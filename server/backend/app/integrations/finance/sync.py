"""Finance sync — CSV import pipeline.

Finance doesn't pull from an API. Data enters via CSV import.
This module orchestrates: parse → deduplicate → insert → categorise → detect transfers.
"""

import hashlib
import logging

from sqlalchemy.orm import Session

from app.integrations.finance.models import Account, ImportHistory, Transaction
from app.integrations.finance.services import (
    CategoryService,
    detect_transfers,
    extract_fingerprint,
    lookup_fingerprint,
    parse_csv_content,
    update_monthly_summaries,
)

logger = logging.getLogger(__name__)


def import_csv(
    content: str,
    filename: str,
    account_name: str | None,
    account_type: str | None,
    session: Session,
) -> dict:
    """Full CSV import pipeline.

    Args:
        content: Raw CSV text.
        filename: Original filename (for logging and dedup).
        account_name: Account name (e.g. "AIB Current", "Revolut Alex"). Optional if fingerprint is registered.
        account_type: Account type (e.g. "AIB", "Revolut"). Optional if fingerprint is registered.
        session: DB session.

    Returns:
        Dict with status, counts, and any errors.
    """
    # Check for duplicate import
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = session.query(ImportHistory).filter_by(file_hash=file_hash).first()
    if existing:
        return {
            "status": "duplicate",
            "message": f"This file was already imported on {existing.imported_at.isoformat()}",
        }

    # Parse
    result = parse_csv_content(content, filename)
    if result["status"] != "success":
        return result

    parsed_transactions = result["transactions"]
    file_format = result["format"]

    # Auto-detect account via fingerprint if not provided
    fp_type, fp_value = None, None
    if not account_name or not account_type:
        fmt, fp_type, fp_value = extract_fingerprint(content, filename)
        if fp_type and fp_value:
            detected_account = lookup_fingerprint(session, fp_type, fp_value)
            if detected_account:
                account_name = detected_account.name
                account_type = detected_account.type
                logger.info(f"Auto-detected account: {account_name} (via {fp_type}={fp_value})")
            else:
                # Fingerprint found but not registered — return for user to map
                known_accounts = [
                    {"id": a.id, "name": a.name, "type": a.type}
                    for a in session.query(Account).order_by(Account.name).all()
                ]
                return {
                    "status": "unknown_account",
                    "format": file_format,
                    "fingerprint_type": fp_type,
                    "fingerprint_value": fp_value,
                    "known_accounts": known_accounts,
                    "transaction_count": len(parsed_transactions),
                    "sample_transactions": [
                        {"date": t["date"], "description": t["description"], "amount": t["amount"]}
                        for t in parsed_transactions[:5]
                    ],
                    "message": (
                        f"Detected {file_format} format with fingerprint {fp_type}={fp_value}, "
                        f"but no account is registered for it. "
                        f"Use finance_register_fingerprint to map it, then re-import."
                    ),
                }
        else:
            return {
                "status": "error",
                "message": "Could not detect account. Provide account_name and account_type explicitly.",
            }

    # Get or create account
    account = session.query(Account).filter_by(name=account_name).first()
    if not account:
        account = Account(name=account_name, type=account_type)
        session.add(account)
        session.flush()

    # Insert transactions (skip duplicates by date+description+amount)
    inserted = 0
    duplicates = 0
    for tx_data in parsed_transactions:
        exists = (
            session.query(Transaction)
            .filter_by(
                account_id=account.id,
                date=tx_data["date"],
                description=tx_data["description"],
                amount=tx_data["amount"],
            )
            .first()
        )
        if exists:
            duplicates += 1
            continue

        tx = Transaction(
            account_id=account.id,
            date=tx_data["date"],
            description=tx_data["description"],
            amount=tx_data["amount"],
            balance=tx_data.get("balance"),
            currency=tx_data.get("currency", "EUR"),
            transaction_type=tx_data.get("transaction_type"),
            source_file=filename,
        )
        session.add(tx)
        inserted += 1

    session.flush()

    # Categorise new transactions
    cat_service = CategoryService(session)
    uncategorized = (
        session.query(Transaction)
        .filter_by(account_id=account.id, category_id=None)
        .filter(Transaction.is_manual_category == False)  # noqa: E712
        .all()
    )
    categorized = cat_service.bulk_categorize(uncategorized)

    # Detect transfers
    transfer_result = detect_transfers(session)

    # Update monthly summaries
    update_monthly_summaries(session)

    # Record import
    history = ImportHistory(
        filename=filename,
        file_hash=file_hash,
        format_type=file_format,
        account_id=account.id,
        transactions_imported=inserted,
        transactions_duplicates=duplicates,
    )
    session.add(history)
    session.commit()

    logger.info(
        f"Finance import: {inserted} new, {duplicates} dupes, "
        f"{categorized} categorized, {transfer_result['total_flagged']} transfers"
    )

    return {
        "status": "success",
        "imported": inserted,
        "duplicates": duplicates,
        "categorized": categorized,
        "transfers_flagged": transfer_result["total_flagged"],
        "message": f"Imported {inserted} transactions from {filename}",
    }
