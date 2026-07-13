"""SQLAlchemy models for the Finance integration.

Ported from finance-dashboard — modernised to Mapped[T] + mapped_column() style.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coglib import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String)  # "AIB", "Revolut"

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account", lazy=True)
    monthly_summaries: Mapped[list["MonthlySummary"]] = relationship(back_populates="account", lazy=True)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)

    rules: Mapped[list["CategorizationRule"]] = relationship(back_populates="category", lazy=True)
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="category", lazy=True)
    monthly_summaries: Mapped[list["MonthlySummary"]] = relationship(back_populates="category", lazy=True)


class CategorizationRule(Base):
    __tablename__ = "categorization_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"))
    match_pattern: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped["Category"] = relationship(back_populates="rules")

    __table_args__ = (
        UniqueConstraint("match_pattern", "category_id", name="unique_rule"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    date: Mapped[datetime] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String)
    merchant: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    balance: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    matched_rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categorization_rules.id"), nullable=True
    )
    is_manual_category: Mapped[bool] = mapped_column(Boolean, default=False)
    is_internal_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transaction_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_file: Mapped[str | None] = mapped_column(String, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="transactions")
    category: Mapped["Category | None"] = relationship(back_populates="transactions")

    __table_args__ = (
        Index("idx_transactions_date", "date"),
        Index("idx_transactions_merchant", "merchant"),
    )


class ImportHistory(Base):
    __tablename__ = "import_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String, unique=True)
    format_type: Mapped[str] = mapped_column(String)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    transactions_imported: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transactions_duplicates: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AccountFingerprint(Base):
    """Maps content fingerprints to accounts for auto-detection during CSV import.

    Fingerprint types:
    - aib_account_number: Posted Account value (e.g., "930156 - 25232034")
    - aib_card_number: Masked Card Number value (e.g., "**** ****")
    - revolut_hash: Hash suffix from Revolut filename (e.g., "de3b34")
    """

    __tablename__ = "account_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"))
    fingerprint_type: Mapped[str] = mapped_column(String)
    fingerprint_value: Mapped[str] = mapped_column(String)

    account: Mapped["Account"] = relationship()

    __table_args__ = (
        UniqueConstraint("fingerprint_type", "fingerprint_value", name="unique_fingerprint"),
    )


class MonthlySummary(Base):
    __tablename__ = "monthly_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[str] = mapped_column(String)  # YYYY-MM
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=True)
    total_income: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total_expense: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0)

    account: Mapped["Account | None"] = relationship(back_populates="monthly_summaries")
    category: Mapped["Category | None"] = relationship(back_populates="monthly_summaries")

    __table_args__ = (
        UniqueConstraint("month", "category_id", "account_id", name="unique_monthly_summary"),
    )
