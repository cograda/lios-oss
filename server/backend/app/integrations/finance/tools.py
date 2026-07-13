"""MCP tool definitions and handlers for Finance integration."""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from sqlalchemy import func, or_

from app.integrations.finance.models import Category, CategorizationRule, Transaction
from app.integrations.finance.services import (
    CategoryService,
    compare_periods,
    get_account_summary,
    get_category_breakdown,
    get_dashboard_stats,
    get_monthly_trend,
    get_top_merchants,
    detect_subscriptions,
    register_fingerprint,
    resolve_period,
)
from app.integrations.finance.sync import import_csv
from app.tools.helpers import iso_or_none, serialize

logger = logging.getLogger(__name__)


def handle_summary(session: Session, arguments: dict[str, Any]) -> str:
    """Dashboard stats for a period with optional comparison."""
    period = arguments.get("period", "this_month")
    account_id = arguments.get("account_id")

    start, end, prev_start, prev_end = resolve_period(period)
    stats = get_dashboard_stats(
        session,
        start_date=start,
        end_date=end,
        account_id=int(account_id) if account_id else None,
        prev_start=prev_start,
        prev_end=prev_end,
    )
    stats["period"] = period

    # Add uncategorised coverage stats
    _not_transfer = or_(
        Transaction.is_internal_transfer == False,  # noqa: E712
        Transaction.is_internal_transfer.is_(None),
    )
    total_q = session.query(func.count(Transaction.id)).filter(_not_transfer)
    uncat_q = session.query(func.count(Transaction.id)).filter(
        _not_transfer, Transaction.category_id.is_(None)
    )
    if start:
        total_q = total_q.filter(Transaction.date >= start)
        uncat_q = uncat_q.filter(Transaction.date >= start)
    if end:
        total_q = total_q.filter(Transaction.date <= end)
        uncat_q = uncat_q.filter(Transaction.date <= end)

    total_count = total_q.scalar() or 0
    uncat_count = uncat_q.scalar() or 0
    stats["uncategorized_count"] = uncat_count
    stats["categorization_coverage"] = (
        round((total_count - uncat_count) / total_count * 100, 1) if total_count else 100.0
    )

    return json.dumps(stats, indent=2)


def handle_transactions(session: Session, arguments: dict[str, Any]) -> str:
    """Search and list transactions."""
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    category = arguments.get("category")
    search = arguments.get("search")
    account_id = arguments.get("account_id")
    limit = min(int(arguments.get("limit", 50)), 200)

    query = session.query(Transaction).order_by(Transaction.date.desc())

    if start_date:
        query = query.filter(Transaction.date >= start_date)
    if end_date:
        query = query.filter(Transaction.date <= end_date)
    if account_id:
        query = query.filter(Transaction.account_id == int(account_id))
    if category:
        if category.lower() == "uncategorized":
            query = query.filter(Transaction.category_id.is_(None))
        else:
            query = query.filter(Transaction.category.has(name=category))
    if search:
        query = query.filter(Transaction.description.ilike(f"%{search}%"))

    transactions = query.limit(limit).all()

    result = [
        serialize(
            tx,
            ["id", "date", "description", "amount", "currency", "category", "account", "is_internal_transfer"],
            renames={"is_internal_transfer": "is_transfer"},
            transforms={
                "date": iso_or_none,
                "amount": float,
                "currency": lambda v: v or "EUR",
                "category": lambda v: v.name if v else None,
                "account": lambda v: v.name if v else None,
            },
        )
        for tx in transactions
    ]
    return json.dumps(result, indent=2)


def handle_categories(session: Session, arguments: dict[str, Any]) -> str:
    """Category spending breakdown for a period."""
    period = arguments.get("period", "this_month")
    account_id = arguments.get("account_id")

    start, end, _, _ = resolve_period(period)
    breakdown = get_category_breakdown(
        session,
        start_date=start,
        end_date=end,
        account_id=int(account_id) if account_id else None,
    )
    return json.dumps({"period": period, "categories": breakdown}, indent=2)


def handle_trends(session: Session, arguments: dict[str, Any]) -> str:
    """Monthly income/expense trend."""
    months = int(arguments.get("months", 12))
    trend = get_monthly_trend(session, months=months)
    return json.dumps({"months": months, "trend": trend}, indent=2)


def handle_subscriptions(session: Session, arguments: dict[str, Any]) -> str:
    """Detect recurring charges."""
    period = arguments.get("period", "last_12_months")
    start, end, _, _ = resolve_period(period)
    subs = detect_subscriptions(session, start_date=start, end_date=end)
    return json.dumps({"period": period, "subscriptions": subs}, indent=2)


def handle_import(session: Session, arguments: dict[str, Any]) -> str:
    """Import transactions from CSV content. Auto-detects account if fingerprint is registered."""
    content = arguments.get("content", "")
    filename = arguments.get("filename", "upload.csv")
    account_name = arguments.get("account_name") or None
    account_type = arguments.get("account_type") or None

    if not content:
        return json.dumps({"error": "content is required"})

    result = import_csv(content, filename, account_name, account_type, session)
    return json.dumps(result, indent=2)


def handle_uncategorized(session: Session, arguments: dict[str, Any]) -> str:
    """Surface uncategorised transactions grouped by description pattern."""
    limit = min(int(arguments.get("limit", 20)), 100)

    _not_transfer = or_(
        Transaction.is_internal_transfer == False,  # noqa: E712
        Transaction.is_internal_transfer.is_(None),
    )

    rows = (
        session.query(
            func.lower(func.trim(Transaction.description)).label("pattern"),
            func.count(Transaction.id).label("count"),
            func.sum(Transaction.amount).label("total"),
            func.min(Transaction.date).label("earliest"),
            func.max(Transaction.date).label("latest"),
        )
        .filter(Transaction.category_id.is_(None), _not_transfer)
        .group_by(func.lower(func.trim(Transaction.description)))
        .order_by(func.count(Transaction.id).desc())
        .limit(limit)
        .all()
    )

    total_uncategorized = (
        session.query(func.count(Transaction.id))
        .filter(Transaction.category_id.is_(None), _not_transfer)
        .scalar()
    ) or 0

    groups = [
        serialize(
            row,
            ["pattern", "count", "total", "earliest", "latest"],
            transforms={
                "total": lambda v: round(float(v), 2),
                "earliest": iso_or_none,
                "latest": iso_or_none,
            },
        )
        for row in rows
    ]

    return json.dumps({
        "total_uncategorized": total_uncategorized,
        "groups_shown": len(groups),
        "groups": groups,
    }, indent=2)


def _create_single_rule(session: Session, pattern: str, category_name: str, priority: int) -> dict:
    """Create a single categorisation rule. Returns status dict."""
    # Find or create category
    category = session.query(Category).filter(
        func.lower(Category.name) == category_name.lower()
    ).first()
    if not category:
        category = Category(name=category_name)
        session.add(category)
        session.flush()

    # Check for existing rule with same pattern and category
    existing = session.query(CategorizationRule).filter_by(
        match_pattern=pattern.lower(), category_id=category.id
    ).first()
    if existing:
        return {"status": "exists", "rule": f"'{pattern}' → {category_name}"}

    # Create the rule
    rule = CategorizationRule(
        match_pattern=pattern.lower(),
        category_id=category.id,
        priority=priority,
    )
    session.add(rule)
    session.flush()
    return {"status": "created", "rule": f"'{pattern}' → {category_name} (priority {priority})"}


def handle_add_rule(session: Session, arguments: dict[str, Any]) -> str:
    """Create categorisation rule(s) and apply to existing transactions.

    Supports single rule (pattern + category) or batch (rules array).
    """
    rules_input = arguments.get("rules")

    if rules_input:
        # Batch mode
        created = 0
        existed = 0
        for r in rules_input:
            p = r.get("pattern", "").strip()
            c = r.get("category", "").strip()
            pri = int(r.get("priority", 0))
            if not p or not c:
                continue
            result = _create_single_rule(session, p, c, pri)
            if result["status"] == "created":
                created += 1
            else:
                existed += 1
    else:
        # Single rule mode
        pattern = arguments.get("pattern", "").strip()
        category_name = arguments.get("category", "").strip()
        priority = int(arguments.get("priority", 0))

        if not pattern:
            return json.dumps({"error": "pattern is required"})
        if not category_name:
            return json.dumps({"error": "category is required"})

        result = _create_single_rule(session, pattern, category_name, priority)
        if result["status"] == "exists":
            return json.dumps({"status": "exists", "message": f"Rule already exists: {result['rule']}"})
        created = 1
        existed = 0

    # Apply all rules to uncategorised transactions
    svc = CategoryService(session)
    svc.load_rules()
    uncategorized = session.query(Transaction).filter(
        Transaction.category_id.is_(None),
        Transaction.is_manual_category == False,  # noqa: E712
    ).all()
    categorized = svc.bulk_categorize(uncategorized)
    session.commit()

    return json.dumps({
        "status": "ok",
        "rules_created": created,
        "rules_existed": existed,
        "newly_categorized": categorized,
    }, indent=2)


def handle_top_merchants(session: Session, arguments: dict[str, Any]) -> str:
    """Top merchants/payees by spend."""
    period = arguments.get("period", "this_month")
    category = arguments.get("category")
    limit = min(int(arguments.get("limit", 20)), 50)
    account_id = arguments.get("account_id")

    merchants = get_top_merchants(
        session,
        period=period,
        category=category,
        limit=limit,
        account_id=int(account_id) if account_id else None,
    )
    return json.dumps({"period": period, "merchants": merchants}, indent=2)


def handle_compare(session: Session, arguments: dict[str, Any]) -> str:
    """Compare spending by category between two periods."""
    period_a = arguments.get("period_a", "this_month")
    period_b = arguments.get("period_b", "last_month")
    account_id = arguments.get("account_id")

    comparison = compare_periods(
        session,
        period_a=period_a,
        period_b=period_b,
        account_id=int(account_id) if account_id else None,
    )
    return json.dumps({
        "period_a": period_a,
        "period_b": period_b,
        "categories": comparison,
    }, indent=2)


def handle_accounts(session: Session, arguments: dict[str, Any]) -> str:
    """Per-account summary."""
    period = arguments.get("period", "all")
    accounts = get_account_summary(session, period=period)
    return json.dumps({"period": period, "accounts": accounts}, indent=2)


def handle_register_fingerprint(session: Session, arguments: dict[str, Any]) -> str:
    """Register a fingerprint → account mapping for auto-detection."""
    account_name = arguments.get("account_name", "").strip()
    account_type = arguments.get("account_type", "").strip()
    fingerprint_type = arguments.get("fingerprint_type", "").strip()
    fingerprint_value = arguments.get("fingerprint_value", "").strip()

    if not account_name:
        return json.dumps({"error": "account_name is required"})
    if not account_type:
        return json.dumps({"error": "account_type is required (AIB, Revolut)"})
    if not fingerprint_type:
        return json.dumps({"error": "fingerprint_type is required"})
    if not fingerprint_value:
        return json.dumps({"error": "fingerprint_value is required"})

    result = register_fingerprint(session, account_name, account_type, fingerprint_type, fingerprint_value)
    return json.dumps(result, indent=2)


def get_mcp_tools() -> list[dict]:
    """Return MCP tool definitions with handler functions."""
    return [
        {
            "name": "finance_summary",
            "description": (
                "Financial dashboard: total income, expenses, net savings, and savings rate "
                "for a period, with comparison to the previous equivalent period. "
                "Also shows categorisation coverage. "
                "Periods: this_month, last_month, last_3_months, last_6_months, ytd, last_12_months, all."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time period preset.",
                        "default": "this_month",
                        "enum": ["this_month", "last_month", "last_3_months", "last_6_months", "ytd", "last_12_months", "all"],
                    },
                    "account_id": {
                        "type": "integer",
                        "description": "Filter to a specific account ID (optional).",
                    },
                },
            },
            "handler": handle_summary,
            "category": "money",
            "examples": [
                "How much did I spend this month?",
                "What's my savings rate?",
                "Financial summary for last quarter",
            ],
        },
        {
            "name": "finance_transactions",
            "description": (
                "Search and list financial transactions. Filter by date range, category, "
                "or text search. Returns amount, description, category, and account for each."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD).",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD).",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category name to filter by, or 'uncategorized'.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Text search in transaction descriptions.",
                    },
                    "account_id": {
                        "type": "integer",
                        "description": "Filter to a specific account ID.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50, max 200).",
                        "default": 50,
                    },
                },
            },
            "handler": handle_transactions,
            "category": "money",
            "examples": [
                "Show me recent transactions",
                "What did I spend at Dunnes?",
                "Uncategorized transactions this month",
            ],
        },
        {
            "name": "finance_categories",
            "description": (
                "Spending breakdown by category for a period — shows where money is going. "
                "Returns each category with total spend and transaction count."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time period preset.",
                        "default": "this_month",
                        "enum": ["this_month", "last_month", "last_3_months", "last_6_months", "ytd", "last_12_months", "all"],
                    },
                    "account_id": {
                        "type": "integer",
                        "description": "Filter to a specific account ID (optional).",
                    },
                },
            },
            "handler": handle_categories,
            "category": "money",
            "examples": [
                "Where am I spending the most?",
                "Category breakdown for last month",
            ],
        },
        {
            "name": "finance_trends",
            "description": (
                "Monthly income vs expense trend over time. Shows each month's income, "
                "expenses, and net for the last N months. Good for spotting spending trends."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months to include (default 12).",
                        "default": 12,
                    },
                },
            },
            "handler": handle_trends,
            "category": "money",
            "examples": [
                "How has spending changed over time?",
                "Show income vs expenses by month",
            ],
        },
        {
            "name": "finance_subscriptions",
            "description": (
                "Detect recurring charges (subscriptions, regular payments) by analysing "
                "transaction patterns. Returns each subscription with frequency, average "
                "amount, and when it was last charged."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time range to analyse.",
                        "default": "last_12_months",
                    },
                },
            },
            "handler": handle_subscriptions,
            "category": "money",
            "examples": [
                "What subscriptions am I paying for?",
                "Show recurring charges",
            ],
        },
        {
            "name": "finance_top_merchants",
            "description": (
                "Top merchants/payees ranked by total spend. Shows where money actually goes "
                "at the individual merchant level (more granular than category breakdown). "
                "Optionally filter by category to drill into a specific spending area."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time period preset.",
                        "default": "this_month",
                        "enum": ["this_month", "last_month", "last_3_months", "last_6_months", "ytd", "last_12_months", "all"],
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter to a specific category (e.g. 'Groceries').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 50).",
                        "default": 20,
                    },
                    "account_id": {
                        "type": "integer",
                        "description": "Filter to a specific account ID.",
                    },
                },
            },
            "handler": handle_top_merchants,
            "category": "money",
            "examples": [
                "Where am I spending the most?",
                "Top merchants for Groceries",
                "Biggest payees this year",
            ],
        },
        {
            "name": "finance_compare",
            "description": (
                "Compare spending by category between two time periods. Shows each category's "
                "total in both periods plus the change amount and percentage. "
                "Great for month-over-month or year-over-year comparisons."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period_a": {
                        "type": "string",
                        "description": "First period (preset like 'this_month' or 'YYYY-MM' for a specific month).",
                        "default": "this_month",
                    },
                    "period_b": {
                        "type": "string",
                        "description": "Second period to compare against.",
                        "default": "last_month",
                    },
                    "account_id": {
                        "type": "integer",
                        "description": "Filter to a specific account ID.",
                    },
                },
            },
            "handler": handle_compare,
            "category": "money",
            "examples": [
                "How has spending changed this month vs last?",
                "Compare Q1 to Q4 spending",
            ],
        },
        {
            "name": "finance_accounts",
            "description": (
                "List all bank accounts with income, expense, net, and transaction count. "
                "Useful for seeing which accounts are most active and for getting account IDs "
                "to filter other finance tools."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Time period preset.",
                        "default": "all",
                        "enum": ["this_month", "last_month", "last_3_months", "last_6_months", "ytd", "last_12_months", "all"],
                    },
                },
            },
            "handler": handle_accounts,
            "category": "money",
            "examples": [
                "Show my accounts",
                "Which account has the most spending?",
            ],
        },
        {
            "name": "finance_import_csv",
            "description": (
                "Import transactions from CSV content. Auto-detects format (AIB, Revolut, Generic) "
                "and account (via registered fingerprints). If account is not registered, returns "
                "status 'unknown_account' with fingerprint details — use finance_register_fingerprint "
                "to map it, then re-import. You can also provide account_name/account_type explicitly."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Raw CSV file content.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Original filename (important for Revolut account detection).",
                    },
                    "account_name": {
                        "type": "string",
                        "description": "Account name (e.g. 'AIB Current', 'Revolut Alex'). Optional if fingerprint is registered.",
                    },
                    "account_type": {
                        "type": "string",
                        "description": "Account type (e.g. 'AIB', 'Revolut'). Optional if fingerprint is registered.",
                    },
                },
                "required": ["content"],
            },
            "handler": handle_import,
            "category": "money",
        },
        {
            "name": "finance_register_fingerprint",
            "description": (
                "Register a CSV fingerprint → account mapping for auto-detection. "
                "Called after finance_import_csv returns 'unknown_account'. "
                "Once registered, future imports of CSVs with the same fingerprint "
                "will auto-detect the account."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_name": {
                        "type": "string",
                        "description": "Account name (e.g. 'AIB Current', 'Revolut Alex').",
                    },
                    "account_type": {
                        "type": "string",
                        "description": "Account type: 'AIB' or 'Revolut'.",
                    },
                    "fingerprint_type": {
                        "type": "string",
                        "description": "Type from the unknown_account response (e.g. 'aib_account_number', 'revolut_hash').",
                    },
                    "fingerprint_value": {
                        "type": "string",
                        "description": "Value from the unknown_account response (e.g. '930156 - 25232034', 'de3b34').",
                    },
                },
                "required": ["account_name", "account_type", "fingerprint_type", "fingerprint_value"],
            },
            "handler": handle_register_fingerprint,
            "category": "money",
        },
        {
            "name": "finance_uncategorized",
            "description": (
                "Show uncategorised transactions grouped by description pattern. "
                "Useful for identifying which merchants need categorisation rules. "
                "Groups are sorted by frequency (most common first)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max groups to return (default 20, max 100).",
                        "default": 20,
                    },
                },
            },
            "handler": handle_uncategorized,
            "category": "money",
            "examples": [
                "What transactions need categorising?",
                "Show uncategorised spending",
            ],
        },
        {
            "name": "finance_add_rule",
            "description": (
                "Create categorisation rule(s) and immediately apply to all uncategorised transactions. "
                "Supports single rule (pattern + category) or batch mode (rules array). "
                "Pattern is a case-insensitive substring match against transaction descriptions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Substring to match (case-insensitive). For single rule mode.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category name to assign. Created if it doesn't exist. For single rule mode.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Rule priority (higher = checked first, default 0).",
                        "default": 0,
                    },
                    "rules": {
                        "type": "array",
                        "description": "Batch mode: array of rules to create at once.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pattern": {"type": "string"},
                                "category": {"type": "string"},
                                "priority": {"type": "integer", "default": 0},
                            },
                            "required": ["pattern", "category"],
                        },
                    },
                },
            },
            "handler": handle_add_rule,
            "category": "money",
            "examples": [
                "Categorise DUNNES as Groceries",
                "Add rules for SPOTIFY→Entertainment, DUNNES→Groceries, TESCO→Groceries",
            ],
        },
    ]
