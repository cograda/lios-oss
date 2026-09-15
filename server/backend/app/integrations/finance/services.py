"""Finance business logic — consolidated from finance-dashboard services.

Contains: CSV parsing, category matching, analytics, and transfer detection.
"""

import csv
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

# Tines expense reimbursements: 16+ hex chars followed by AIB ref separator.
_TINES_EXPENSE_PATTERN = re.compile(r"^[0-9A-Fa-f]{16,}\s*\|")

# Family member names that appear in Revolut family transfer descriptions.
# A "Transfer to/from <NAME>" between two known Revolut accounts is internal.
_FAMILY_NAMES = (
    "ALEX CATHAL O RIVERS",
    "SAM VIRGINIA RIVERS",
    "ALEX CATHAL O RIVERS & SAM VIRGINIA RIVERS",
)

# AIB MOBI internal transfer prefixes (between own AIB accounts, not payments).
_AIB_MOBI_INTERNAL_PREFIXES = (
    "*MOBI JOINT SAVING",
    "*MOBI JOINT-034",
    "*MOBI SAVING CLUB",
    "*MOBI CURRENT",
    "*MOBI BUILD",
    "*MOBI BUILDING",
    "*MOBI BOBBLES",
    "*MOBI BATHROOM",
)


def _is_family_transfer_debit(desc: str) -> bool:
    up = (desc or "").upper()
    return any(f"TRANSFER TO {name}" in up for name in _FAMILY_NAMES)


def _is_family_transfer_credit(desc: str) -> bool:
    up = (desc or "").upper()
    return any(f"TRANSFER FROM {name}" in up for name in _FAMILY_NAMES)


def _is_revolut_topup(desc: str) -> bool:
    up = (desc or "").upper()
    return (
        up.startswith("TOP-UP BY")
        or "APPLE PAY TOP-UP" in up
        or up == "OPEN BANKING TOP-UP"
    )


def _is_aib_mobi_internal(desc: str) -> bool:
    up = (desc or "").upper()
    if not up.startswith("*MOBI"):
        return False
    return any(up.startswith(p) for p in _AIB_MOBI_INTERNAL_PREFIXES)

from dateutil import parser as dateparser
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.integrations.finance.models import (
    Account,
    AccountFingerprint,
    Category,
    CategorizationRule,
    ImportHistory,
    MonthlySummary,
    Transaction,
)
from app.services.text import ILIKE_ESCAPE_CHAR, escape_ilike

# ─── Transfer filter (shared across analytics queries) ───

_not_transfer = or_(
    Transaction.is_internal_transfer == False,  # noqa: E712
    Transaction.is_internal_transfer.is_(None),
)


# ═══════════════════════════════════════════════════════════
# CSV Parsing
# ═══════════════════════════════════════════════════════════

FORMAT_SIGNATURES = {
    "AIB": ["debit amount", "credit amount"],
    "REVOLUT": ["type", "state", "product"],
}


def _parse_date(date_str: str, formats: list[str] | None = None) -> str | None:
    if not date_str or str(date_str).strip() == "":
        return None
    date_str = str(date_str).strip()
    if formats:
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    try:
        return dateparser.parse(date_str).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_amount(value: str) -> float:
    if not value or str(value).strip() == "":
        return 0.0
    cleaned = str(value).strip().replace(",", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _get_val(row: dict, keys: list[str]) -> str:
    for k in keys:
        if k in row:
            return row[k]
    return ""


def _detect_format(headers: list[str]) -> str:
    headers_lower = [h.strip().lower() for h in headers]
    for fmt_name, required in FORMAT_SIGNATURES.items():
        if all(h in headers_lower for h in required):
            return fmt_name
    has_date = any(k in h for h in headers_lower for k in ["date", "posted", "time"])
    has_amount = any(k in h for h in headers_lower for k in ["amount", "debit", "credit", "value", "sum"])
    if has_date and has_amount:
        return "GENERIC"
    return "UNKNOWN"


def _process_aib(row: dict, filename: str) -> dict:
    date_str = _get_val(row, ["Posted Transactions Date", "Posted Transactions Date "])
    parsed_date = _parse_date(date_str, ["%d/%m/%Y", "%d/%m/%y"])

    desc_parts = []
    for i in range(1, 4):
        val = _get_val(row, [f"Description{i}", f"Description{i} "]).strip()
        if val:
            desc_parts.append(val)
    if not desc_parts:
        val = _get_val(row, ["Description", "Description "]).strip()
        if val:
            desc_parts.append(val)
    description = " | ".join(desc_parts)

    debit = _parse_amount(_get_val(row, ["Debit Amount", "Debit Amount "]))
    credit = _parse_amount(_get_val(row, ["Credit Amount", "Credit Amount "]))
    amount = -debit if debit > 0 else credit if credit > 0 else 0.0

    return {
        "date": parsed_date,
        "description": description,
        "amount": amount,
        "balance": _parse_amount(_get_val(row, ["Balance", "Balance "])),
        "currency": (_get_val(row, ["Posted Currency", "Posted Currency "]) or "EUR").strip(),
        "transaction_type": _get_val(row, ["Transaction Type", "Transaction Type "]).strip(),
    }


def _process_revolut(row: dict, filename: str) -> dict:
    date_str = row.get("Completed Date", "") or row.get("Started Date", "")
    return {
        "date": _parse_date(date_str, ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
        "description": row.get("Description", "").strip(),
        "amount": _parse_amount(row.get("Amount", "")),
        "balance": _parse_amount(row.get("Balance", "")),
        "currency": row.get("Currency", "EUR").strip(),
        "transaction_type": row.get("Type", "").strip(),
    }


def _process_generic(row: dict, headers: list[str], filename: str) -> dict:
    parsed_date = None
    for h in headers:
        if any(k in h.lower() for k in ["date", "posted", "time"]):
            parsed_date = _parse_date(row.get(h, ""))
            if parsed_date:
                break

    amount = 0.0
    for h in headers:
        if any(k in h.lower() for k in ["amount", "value", "sum"]):
            amount = _parse_amount(row.get(h, ""))
            if amount != 0.0:
                break
    if amount == 0.0:
        for h in headers:
            if "debit" in h.lower():
                d = _parse_amount(row.get(h, ""))
                if d > 0:
                    amount = -d
                    break
            elif "credit" in h.lower():
                c = _parse_amount(row.get(h, ""))
                if c > 0:
                    amount = c
                    break

    description = ""
    for h in headers:
        if any(k in h.lower() for k in ["description", "memo", "narrative", "details", "payee", "name"]):
            description = row.get(h, "").strip()
            if description:
                break

    balance = 0.0
    for h in headers:
        if "balance" in h.lower():
            balance = _parse_amount(row.get(h, ""))
            break

    currency = "EUR"
    for h in headers:
        if "currency" in h.lower():
            val = row.get(h, "").strip()
            if val:
                currency = val
            break

    return {
        "date": parsed_date,
        "description": description,
        "amount": amount,
        "balance": balance,
        "currency": currency,
        "transaction_type": "",
    }


def parse_csv_content(content: str, filename: str) -> dict:
    """Parse CSV content (string). Returns status, format, transactions, skipped_rows, message."""
    transactions = []
    skipped_rows = []

    try:
        reader = csv.reader(io.StringIO(content))
        try:
            headers = next(reader)
        except StopIteration:
            return {"status": "error", "message": "Empty file"}

        if not headers:
            return {"status": "error", "message": "No headers found"}

        file_format = _detect_format(headers)
        if file_format == "UNKNOWN":
            return {
                "status": "error",
                "message": "Could not detect file format.",
                "format": "UNKNOWN",
                "headers_found": [h.strip() for h in headers],
            }

        cleaned_headers = [h.strip() for h in headers]
        dict_reader = csv.DictReader(io.StringIO(content))
        # Skip the header row we already read
        next(csv.reader(io.StringIO(content)))

        dict_reader = csv.DictReader(io.StringIO(content))

        for row_num, row in enumerate(dict_reader, start=2):
            # Strip whitespace from keys (AIB exports have leading spaces)
            row = {k.strip(): v for k, v in row.items()}
            if not any(row.values()):
                continue
            try:
                if file_format == "AIB":
                    processed = _process_aib(row, filename)
                elif file_format == "REVOLUT":
                    processed = _process_revolut(row, filename)
                elif file_format == "GENERIC":
                    processed = _process_generic(row, cleaned_headers, filename)

                if processed["date"] is None:
                    skipped_rows.append({"row": row_num, "reason": "Could not parse date"})
                    continue
                if processed["amount"] == 0.0 and not processed["description"]:
                    skipped_rows.append({"row": row_num, "reason": "No amount or description"})
                    continue
                transactions.append(processed)
            except Exception as e:
                skipped_rows.append({"row": row_num, "reason": str(e)})

        msg = f"Parsed {len(transactions)} transactions from {filename}"
        if skipped_rows:
            msg += f", {len(skipped_rows)} rows skipped"
        return {
            "status": "success",
            "format": file_format,
            "transactions": transactions,
            "skipped_rows": skipped_rows,
            "message": msg,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════
# Account Fingerprinting
# ═══════════════════════════════════════════════════════════


def extract_fingerprint(content: str, filename: str) -> tuple[str, str | None, str | None]:
    """Extract an account fingerprint from CSV content + filename.

    Returns (format, fingerprint_type, fingerprint_value).
    If no fingerprint can be extracted, type and value are None.
    """
    try:
        reader = csv.reader(io.StringIO(content))
        headers = next(reader)
    except StopIteration:
        return ("UNKNOWN", None, None)

    file_format = _detect_format([h.strip() for h in headers])

    if file_format == "AIB":
        headers_stripped = [h.strip() for h in headers]
        # Try to read first data row
        try:
            first_row = next(reader)
            row_dict = {h.strip(): v.strip() for h, v in zip(headers, first_row)}
        except StopIteration:
            return (file_format, None, None)

        # AIB Current Account format has "Posted Account" column
        posted_account = row_dict.get("Posted Account", "").strip().strip('"')
        if posted_account:
            return (file_format, "aib_account_number", posted_account)

        # AIB Card format has "Masked Card Number" column
        masked_card = row_dict.get("Masked Card Number", "").strip().strip('"')
        if masked_card:
            return (file_format, "aib_card_number", masked_card)

        return (file_format, None, None)

    elif file_format == "REVOLUT":
        # Revolut filenames contain a per-account hash: account-statement_..._<hash>.csv
        match = re.search(r"_([a-f0-9]{6})\.csv$", filename, re.IGNORECASE)
        if match:
            return (file_format, "revolut_hash", match.group(1).lower())
        return (file_format, None, None)

    return (file_format, None, None)


def lookup_fingerprint(
    session: Session, fingerprint_type: str, fingerprint_value: str
) -> Account | None:
    """Look up an account by its fingerprint. Returns None if not registered."""
    fp = (
        session.query(AccountFingerprint)
        .filter_by(fingerprint_type=fingerprint_type, fingerprint_value=fingerprint_value)
        .first()
    )
    if fp:
        return session.query(Account).get(fp.account_id)
    return None


def register_fingerprint(
    session: Session,
    account_name: str,
    account_type: str,
    fingerprint_type: str,
    fingerprint_value: str,
) -> dict:
    """Register a fingerprint → account mapping. Creates the account if needed."""
    # Find or create account
    account = session.query(Account).filter_by(name=account_name).first()
    if not account:
        account = Account(name=account_name, type=account_type)
        session.add(account)
        session.flush()

    # Check for existing fingerprint
    existing = (
        session.query(AccountFingerprint)
        .filter_by(fingerprint_type=fingerprint_type, fingerprint_value=fingerprint_value)
        .first()
    )
    if existing:
        if existing.account_id == account.id:
            return {"status": "exists", "message": f"Already registered: {fingerprint_value} → {account_name}"}
        # Update to point to new account
        existing.account_id = account.id
        session.commit()
        return {"status": "updated", "message": f"Updated: {fingerprint_value} → {account_name}"}

    fp = AccountFingerprint(
        account_id=account.id,
        fingerprint_type=fingerprint_type,
        fingerprint_value=fingerprint_value,
    )
    session.add(fp)
    session.commit()
    return {"status": "ok", "message": f"Registered: {fingerprint_value} → {account_name}"}


# ═══════════════════════════════════════════════════════════
# Category Matching
# ═══════════════════════════════════════════════════════════


class CategoryService:
    def __init__(self, session: Session):
        self.session = session
        self._rules_cache: list[tuple[str, int]] | None = None

    def load_rules(self):
        rules = (
            self.session.query(CategorizationRule)
            .order_by(CategorizationRule.priority.desc())
            .all()
        )
        self._rules_cache = [(r.match_pattern.lower(), r.category_id) for r in rules]

    def match_category(self, description: str) -> int | None:
        """Longest-keyword-wins match.

        When multiple rules match, the longest pattern takes priority —
        prevents broad keywords like "COFFEE" shadowing specific ones
        like "CLOUD PICKER COFFEE".
        """
        if not description:
            return None
        if self._rules_cache is None:
            self.load_rules()
        desc_lower = description.lower()
        best_id: int | None = None
        best_len = 0
        for pattern, category_id in self._rules_cache:
            if pattern in desc_lower and len(pattern) > best_len:
                best_id = category_id
                best_len = len(pattern)
        return best_id

    def _tines_reimbursement_category_id(self) -> int | None:
        cat = (
            self.session.query(Category)
            .filter(Category.name == "Income - Expense Reimbursement")
            .first()
        )
        return cat.id if cat else None

    def apply_rules_to_transaction(self, transaction: Transaction) -> bool:
        if transaction.is_manual_category:
            return True
        # Pattern-based: Tines expense reimbursements (hex ref + AIB ref).
        if transaction.description and _TINES_EXPENSE_PATTERN.match(transaction.description):
            cat_id = self._tines_reimbursement_category_id()
            if cat_id:
                transaction.category_id = cat_id
                return True
        cat_id = self.match_category(transaction.description)
        if cat_id:
            transaction.category_id = cat_id
            return True
        return False

    def bulk_categorize(self, transactions: list[Transaction]) -> int:
        count = 0
        for t in transactions:
            if self.apply_rules_to_transaction(t):
                count += 1
        return count


# ═══════════════════════════════════════════════════════════
# Analytics
# ═══════════════════════════════════════════════════════════


def resolve_period(period: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Map preset name to (start, end, prev_start, prev_end) date strings."""
    today = date.today()
    first_of_month = today.replace(day=1)

    if period == "this_month":
        start, end = first_of_month, today
    elif period == "last_month":
        end = first_of_month - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "last_3_months":
        start = (first_of_month - timedelta(days=90)).replace(day=1)
        end = today
    elif period == "last_6_months":
        start = (first_of_month - timedelta(days=180)).replace(day=1)
        end = today
    elif period == "ytd":
        start = today.replace(month=1, day=1)
        end = today
    elif period == "last_12_months":
        start = (first_of_month - timedelta(days=365)).replace(day=1)
        end = today
    elif len(period) == 7 and period[4] == "-":
        # YYYY-MM format — specific month
        try:
            year, month = int(period[:4]), int(period[5:7])
            start = date(year, month, 1)
            if month == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, month + 1, 1) - timedelta(days=1)
        except (ValueError, IndexError):
            return (None, None, None, None)
    else:  # "all"
        return (None, None, None, None)

    duration = (end - start).days
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=duration)
    return (start.isoformat(), end.isoformat(), prev_start.isoformat(), prev_end.isoformat())


def _query_period_stats(
    session: Session,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: int | None = None,
) -> dict:
    query = session.query(
        func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)).label("income"),
        func.sum(case((Transaction.amount < 0, func.abs(Transaction.amount)), else_=0)).label("expense"),
        func.count(Transaction.id).label("count"),
    ).filter(_not_transfer)

    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    result = query.first()
    income = float(result.income or 0)
    expense = float(result.expense or 0)
    return {
        "total_income": income,
        "total_expense": expense,
        "net_savings": income - expense,
        "transaction_count": result.count or 0,
    }


def get_dashboard_stats(
    session: Session,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: int | None = None,
    prev_start: str | None = None,
    prev_end: str | None = None,
) -> dict:
    current = _query_period_stats(session, start_date, end_date, account_id)
    if prev_start and prev_end:
        current["previous"] = _query_period_stats(session, prev_start, prev_end, account_id)
    return current


def get_category_breakdown(
    session: Session,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: int | None = None,
) -> list[dict]:
    query = (
        session.query(
            func.coalesce(Category.name, "Uncategorized").label("name"),
            func.sum(func.abs(Transaction.amount)).label("total"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .filter(Transaction.amount < 0, _not_transfer)
    )
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    results = (
        query.group_by("name")
        .order_by(func.sum(func.abs(Transaction.amount)).desc())
        .all()
    )
    return [{"name": r.name, "value": float(r.total)} for r in results if r.total and r.total > 0]


def get_monthly_trend(
    session: Session,
    months: int = 12,
    start_date: str | None = None,
    end_date: str | None = None,
    account_id: int | None = None,
) -> list[dict]:
    month_expr = func.to_char(Transaction.date, "YYYY-MM")
    query = session.query(
        month_expr.label("month"),
        func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)).label("income"),
        func.sum(case((Transaction.amount < 0, func.abs(Transaction.amount)), else_=0)).label("expense"),
    ).filter(_not_transfer)

    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    results = query.group_by(month_expr).order_by(month_expr.desc()).limit(months).all()
    return [
        {
            "month": r.month,
            "income": float(r.income or 0),
            "expense": float(r.expense or 0),
            "savings": float((r.income or 0) - (r.expense or 0)),
        }
        for r in reversed(results)
    ]


def get_top_merchants(
    session: Session,
    period: str = "this_month",
    category: str | None = None,
    limit: int = 20,
    account_id: int | None = None,
) -> list[dict]:
    """Top merchants/payees by total spend."""
    start, end, _, _ = resolve_period(period)
    query = (
        session.query(
            func.lower(func.trim(Transaction.description)).label("merchant"),
            func.count(Transaction.id).label("count"),
            func.sum(func.abs(Transaction.amount)).label("total_spend"),
            func.avg(func.abs(Transaction.amount)).label("avg_amount"),
            func.coalesce(Category.name, "Uncategorized").label("category"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .filter(Transaction.amount < 0, _not_transfer)
    )
    if start:
        query = query.filter(Transaction.date >= start)
    if end:
        query = query.filter(Transaction.date <= end)
    if category:
        # No `%` wildcards here — this wants an exact (case-insensitive) name
        # match, so a category name that itself contains a literal `%`/`_`
        # must not be treated as a wildcard.
        query = query.filter(
            Category.name.ilike(escape_ilike(category), escape=ILIKE_ESCAPE_CHAR)
        )
    if account_id:
        query = query.filter(Transaction.account_id == account_id)

    results = (
        query.group_by(
            func.lower(func.trim(Transaction.description)),
            func.coalesce(Category.name, "Uncategorized"),
        )
        .order_by(func.sum(func.abs(Transaction.amount)).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "merchant": r.merchant,
            "count": r.count,
            "total_spend": round(float(r.total_spend), 2),
            "avg_amount": round(float(r.avg_amount), 2),
            "category": r.category,
        }
        for r in results
    ]


def compare_periods(
    session: Session,
    period_a: str,
    period_b: str,
    account_id: int | None = None,
) -> list[dict]:
    """Compare category spending between two periods."""
    a_breakdown = {
        r["name"]: r["value"]
        for r in get_category_breakdown(session, *resolve_period(period_a)[:2], account_id=account_id)
    }
    b_breakdown = {
        r["name"]: r["value"]
        for r in get_category_breakdown(session, *resolve_period(period_b)[:2], account_id=account_id)
    }

    all_categories = sorted(set(a_breakdown.keys()) | set(b_breakdown.keys()))
    result = []
    for cat in all_categories:
        a_total = a_breakdown.get(cat, 0)
        b_total = b_breakdown.get(cat, 0)
        change = a_total - b_total
        change_pct = round((change / b_total) * 100, 1) if b_total > 0 else None
        result.append({
            "category": cat,
            "period_a_total": round(a_total, 2),
            "period_b_total": round(b_total, 2),
            "change_amount": round(change, 2),
            "change_pct": change_pct,
        })
    result.sort(key=lambda r: abs(r["change_amount"]), reverse=True)
    return result


def get_account_summary(
    session: Session,
    period: str = "all",
) -> list[dict]:
    """Per-account income/expense/net summary."""
    start, end, _, _ = resolve_period(period)
    query = (
        session.query(
            Account.id,
            Account.name,
            Account.type,
            func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)).label("income"),
            func.sum(case((Transaction.amount < 0, func.abs(Transaction.amount)), else_=0)).label("expense"),
            func.count(Transaction.id).label("count"),
        )
        .join(Transaction, Transaction.account_id == Account.id)
        .filter(_not_transfer)
    )
    if start:
        query = query.filter(Transaction.date >= start)
    if end:
        query = query.filter(Transaction.date <= end)

    results = query.group_by(Account.id, Account.name, Account.type).order_by(Account.name).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "type": r.type,
            "income": round(float(r.income or 0), 2),
            "expense": round(float(r.expense or 0), 2),
            "net": round(float((r.income or 0) - (r.expense or 0)), 2),
            "transaction_count": r.count,
        }
        for r in results
    ]


def detect_subscriptions(
    session: Session,
    min_occurrences: int = 3,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    query = session.query(Transaction).filter(Transaction.amount < 0, _not_transfer)
    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)

    transactions = query.order_by(Transaction.description, Transaction.date).all()
    groups: dict[tuple, list] = defaultdict(list)
    for tx in transactions:
        key = (tx.description.lower().strip(), round(float(tx.amount), 2))
        groups[key].append(tx)

    subscriptions = []
    for (desc, amount), txs in groups.items():
        if len(txs) < min_occurrences:
            continue
        dates = sorted([tx.date for tx in txs])
        if len(dates) < 2:
            continue
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_interval = sum(intervals) / len(intervals)

        if 25 <= avg_interval <= 35:
            frequency = "monthly"
        elif 12 <= avg_interval <= 16:
            frequency = "fortnightly"
        elif 355 <= avg_interval <= 375:
            frequency = "annual"
        elif 85 <= avg_interval <= 100:
            frequency = "quarterly"
        else:
            frequency = f"~{int(avg_interval)}d"

        variance = sum((i - avg_interval) ** 2 for i in intervals) / len(intervals)
        std_dev = variance**0.5
        confidence = max(0, min(1, 1 - (std_dev / avg_interval))) if avg_interval > 0 else 0
        if confidence < 0.3:
            continue

        subscriptions.append({
            "description": txs[0].description,
            "amount": abs(float(amount)),
            "currency": txs[0].currency or "EUR",
            "frequency": frequency,
            "occurrences": len(txs),
            "last_charged": dates[-1].isoformat(),
            "confidence": round(confidence, 2),
        })

    subscriptions.sort(key=lambda s: s["amount"], reverse=True)
    return subscriptions


def update_monthly_summaries(session: Session) -> int:
    session.query(MonthlySummary).delete()
    month_expr = func.to_char(Transaction.date, "YYYY-MM")
    results = (
        session.query(
            month_expr.label("month"),
            Transaction.category_id,
            Transaction.account_id,
            func.sum(case((Transaction.amount > 0, Transaction.amount), else_=0)).label("income"),
            func.sum(case((Transaction.amount < 0, func.abs(Transaction.amount)), else_=0)).label("expense"),
            func.count(Transaction.id).label("count"),
        )
        .filter(_not_transfer)
        .group_by(month_expr, Transaction.category_id, Transaction.account_id)
        .all()
    )
    summaries = [
        MonthlySummary(
            month=r.month,
            category_id=r.category_id,
            account_id=r.account_id,
            total_income=r.income or 0,
            total_expense=r.expense or 0,
            transaction_count=r.count,
        )
        for r in results
    ]
    session.add_all(summaries)
    session.flush()
    return len(summaries)


# ═══════════════════════════════════════════════════════════
# Transfer Detection
# ═══════════════════════════════════════════════════════════


def detect_transfers(session: Session) -> dict:
    """Detect and flag internal transfers between accounts."""
    counts = {"revolut_internal": 0, "aib_to_revolut": 0, "aib_internal": 0}

    # Reset existing flags (idempotent)
    session.query(Transaction).filter(
        Transaction.is_internal_transfer == True  # noqa: E712
    ).update({Transaction.is_internal_transfer: False})
    session.flush()

    # Pattern 3: AIB internal — INET JOINT SAVING and *MOBI internal transfers.
    aib_internal_candidates = (
        session.query(Transaction)
        .join(Account)
        .filter(
            Account.type == "AIB",
            or_(
                Transaction.description.ilike("%INET JOINT SAVING%"),
                Transaction.description.ilike("*MOBI%"),
            ),
        )
        .all()
    )
    for tx in aib_internal_candidates:
        desc_up = (tx.description or "").upper()
        if "INET JOINT SAVING" in desc_up or _is_aib_mobi_internal(desc_up):
            tx.is_internal_transfer = True
            counts["aib_internal"] += 1

    # Pattern 1: Revolut <-> Revolut family transfers.
    # Matches "Transfer to/from <FAMILY_NAME>" across different Revolut
    # accounts, same amount, within ±1 day.
    revolut_pool = (
        session.query(Transaction)
        .join(Account)
        .filter(
            Account.type == "Revolut",
            Transaction.is_internal_transfer == False,  # noqa: E712
        )
        .order_by(Transaction.date)
        .all()
    )
    rev_debits = [
        t for t in revolut_pool
        if float(t.amount) < 0 and _is_family_transfer_debit(t.description or "")
    ]
    rev_credits = [
        t for t in revolut_pool
        if float(t.amount) > 0 and _is_family_transfer_credit(t.description or "")
    ]
    used_credit_ids: set[int] = set()
    for debit in rev_debits:
        debit_amt = abs(float(debit.amount))
        for credit in rev_credits:
            if credit.id in used_credit_ids:
                continue
            if credit.account_id == debit.account_id:
                continue
            if abs((credit.date - debit.date).days) > 1:
                continue
            if abs(float(credit.amount) - debit_amt) > 0.01:
                continue
            debit.is_internal_transfer = True
            credit.is_internal_transfer = True
            used_credit_ids.add(credit.id)
            counts["revolut_internal"] += 1
            break

    # Pattern 2: AIB -> Revolut top-up.
    aib_debits = (
        session.query(Transaction)
        .filter(
            Transaction.amount < 0,
            Transaction.is_internal_transfer == False,  # noqa: E712
            Transaction.description.ilike("%VDP-Revolut%"),
        )
        .join(Account)
        .filter(Account.type == "AIB")
        .order_by(Transaction.date)
        .all()
    )
    rev_topup_pool = (
        session.query(Transaction)
        .filter(
            Transaction.amount > 0,
            Transaction.is_internal_transfer == False,  # noqa: E712
        )
        .join(Account)
        .filter(Account.type == "Revolut")
        .order_by(Transaction.date)
        .all()
    )
    rev_topups = [t for t in rev_topup_pool if _is_revolut_topup(t.description or "")]
    used_topup_ids: set[int] = set()
    for debit in aib_debits:
        debit_amt = abs(float(debit.amount))
        for topup in rev_topups:
            if topup.id in used_topup_ids:
                continue
            if abs((topup.date - debit.date).days) > 2:
                continue
            if abs(float(topup.amount) - debit_amt) > 0.01:
                continue
            debit.is_internal_transfer = True
            topup.is_internal_transfer = True
            used_topup_ids.add(topup.id)
            counts["aib_to_revolut"] += 1
            break

    session.flush()
    total = sum(counts.values())
    return {"total_flagged": total, "by_pattern": counts}
