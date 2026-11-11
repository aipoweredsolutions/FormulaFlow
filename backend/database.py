import os
import secrets
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class RequestStatus(str, Enum):
    PENDING_VERIFICATION = "Pending Verification"
    REJECTED = "Rejected"
    PAID = "Paid"
    PENDING = "Pending"
    APPROVED = "Approved"
    FULFILLED = "Fulfilled"


class RequestLifecycleStatus(str, Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAYOUT_PENDING = "payout_pending"
    PAYOUT_CONFIRMED = "payout_confirmed"
    READY_FOR_PICKUP = "ready_for_pickup"
    COLLECTED = "collected"


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./formula_requests.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    __table_args__ = (
        Index(
            "uq_admin_accounts_single_admin",
            "role",
            unique=True,
            sqlite_where=text("role = 'admin'"),
            postgresql_where=text("role = 'admin'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("admin_accounts.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class FormulaRequest(Base):
    __tablename__ = "formula_requests"
    __table_args__ = (
        Index(
            "uq_formula_requests_public_reference_code",
            "public_reference_code",
            unique=True,
        ),
        Index(
            "uq_formula_requests_continuity_month",
            "continuity_id",
            "support_month",
            unique=True,
            sqlite_where=text(
                "continuity_id IS NOT NULL AND support_month IS NOT NULL"
            ),
            postgresql_where=text(
                "continuity_id IS NOT NULL AND support_month IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    public_reference_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=lambda: secrets.token_urlsafe(24),
    )
    recipient_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    county: Mapped[str | None] = mapped_column(String(200), nullable=True)
    formula_needs: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RequestStatus.PENDING_VERIFICATION.value
    )
    matched_vendor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    infant_age_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    continuity_id: Mapped[int | None] = mapped_column(
        ForeignKey("formula_requests.id"), nullable=True, index=True
    )
    support_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    containers_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pickup_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pickup_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    pickup_token_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pickup_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pickup_token_retrieved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=RequestLifecycleStatus.SUBMITTED.value
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payout_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payout_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_for_pickup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class RequestAuditEvent(Base):
    __tablename__ = "request_audit_events"
    __table_args__ = (
        Index("ix_request_audit_events_request_created", "request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("formula_requests.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    previous_lifecycle_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_lifecycle_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id"), nullable=True
    )
    merchant_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (
        Index(
            "uq_payment_transactions_open_request",
            "request_id",
            unique=True,
            sqlite_where=text("status != 'FAILED'"),
            postgresql_where=text("status != 'FAILED'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("formula_requests.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    containers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(254), nullable=False)
    sender_batch_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )
    payout_batch_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    payout_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PAYPAL"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class DonationOrder(Base):
    __tablename__ = "donation_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    paypal_order_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )
    paypal_capture_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class DonationRecord(Base):
    __tablename__ = "donation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    paypal_order_id: Mapped[str] = mapped_column(
        ForeignKey("donation_orders.paypal_order_id"), nullable=False, unique=True
    )
    paypal_capture_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class PayPalWebhookEvent(Base):
    __tablename__ = "paypal_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    paypal_email: Mapped[str] = mapped_column(String(254), nullable=False)
    formula_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    county: Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_per_container_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PoolBalance(Base):
    __tablename__ = "pool_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    available_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class VendorPriceHistory(Base):
    __tablename__ = "vendor_price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vendor_id: Mapped[int] = mapped_column(
        ForeignKey("vendors.id"), nullable=False, index=True
    )
    price_per_container_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


def create_db_tables() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_existing_columns()
    _initialize_pool_balance()


def _migrate_existing_columns() -> None:
    migrations = {
        "formula_requests": {
            "county": "VARCHAR(200)",
            "public_reference_code": "VARCHAR(32)",
            "infant_age_months": "INTEGER",
            "birth_month": "DATE",
            "continuity_id": "INTEGER",
            "support_month": "DATE",
            "containers_per_month": "INTEGER",
            "total_cost_usd": "NUMERIC(10, 2)",
            "match_reason": "TEXT",
            "pickup_token_hash": "VARCHAR(64)",
            "pickup_token_encrypted": "TEXT",
            "pickup_token_used_at": "DATETIME",
            "pickup_token_expires_at": "DATETIME",
            "pickup_token_retrieved_at": "DATETIME",
            "lifecycle_status": "VARCHAR(40) NOT NULL DEFAULT 'submitted'",
            "submitted_at": "DATETIME",
            "approved_at": "DATETIME",
            "payout_pending_at": "DATETIME",
            "payout_confirmed_at": "DATETIME",
            "ready_for_pickup_at": "DATETIME",
            "collected_at": "DATETIME",
            "rejected_at": "DATETIME",
        },
        "request_audit_events": {
            "previous_lifecycle_status": "VARCHAR(40)",
            "new_lifecycle_status": "VARCHAR(40)",
        },
        "vendors": {
            "location": "VARCHAR(500) NOT NULL DEFAULT ''",
            "county": "VARCHAR(200)",
            "price_per_container_usd": "NUMERIC(10, 2)",
        },
        "donation_records": {
            "paypal_capture_id": "VARCHAR(100)",
        },
        "payment_transactions": {
            "sender_batch_id": "VARCHAR(100)",
            "payout_mode": "VARCHAR(20) NOT NULL DEFAULT 'PAYPAL'",
            "containers": "INTEGER",
        },
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        for table_name, columns in migrations.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            for column_name, column_type in columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE {table_name} ADD COLUMN "
                            f"{column_name} {column_type}"
                        )
                    )
        if "formula_requests" in existing_tables:
            request_columns = {
                column["name"]
                for column in inspect(connection).get_columns("formula_requests")
            }
            if "created_at" in request_columns:
                connection.execute(
                    text(
                        "UPDATE formula_requests SET submitted_at = created_at "
                        "WHERE submitted_at IS NULL"
                    )
                )
            connection.execute(
                text(
                    "UPDATE formula_requests SET submitted_at = CURRENT_TIMESTAMP "
                    "WHERE submitted_at IS NULL"
                )
            )
            rows_without_reference = connection.execute(
                text(
                    "SELECT id FROM formula_requests "
                    "WHERE public_reference_code IS NULL"
                )
            ).all()
            for row in rows_without_reference:
                connection.execute(
                    text(
                        "UPDATE formula_requests SET public_reference_code = :code "
                        "WHERE id = :request_id"
                    ),
                    {
                        "code": secrets.token_urlsafe(24),
                        "request_id": row.id,
                    },
                )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_formula_requests_public_reference_code "
                    "ON formula_requests (public_reference_code)"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_formula_requests_continuity_month "
                    "ON formula_requests (continuity_id, support_month) "
                    "WHERE continuity_id IS NOT NULL "
                    "AND support_month IS NOT NULL"
                )
            )
            if {"status", "pickup_token_hash", "lifecycle_status"} <= request_columns:
                connection.execute(
                    text(
                        "UPDATE formula_requests SET lifecycle_status = CASE "
                        "WHEN status = 'Approved' THEN 'approved' "
                        "WHEN status = 'Rejected' THEN 'rejected' "
                        "WHEN status = 'Paid' AND pickup_token_hash IS NOT NULL "
                        "THEN 'ready_for_pickup' "
                        "WHEN status = 'Paid' THEN 'payout_confirmed' "
                        "ELSE 'submitted' END "
                        "WHERE lifecycle_status = 'submitted' "
                        "AND status != 'Pending Verification'"
                    )
                )
        connection.execute(
            text(
                "UPDATE donation_records "
                "SET paypal_capture_id = ("
                "SELECT donation_orders.paypal_capture_id "
                "FROM donation_orders "
                "WHERE donation_orders.paypal_order_id = "
                "donation_records.paypal_order_id"
                ") "
                "WHERE paypal_capture_id IS NULL"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_donation_records_paypal_capture_id "
                "ON donation_records (paypal_capture_id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_payment_transactions_sender_batch_id "
                "ON payment_transactions (sender_batch_id)"
            )
        )


def _initialize_pool_balance() -> None:
    from sqlalchemy import func

    with SessionLocal() as db:
        if db.get(PoolBalance, 1) is not None:
            return
        donations = db.scalar(select(func.coalesce(func.sum(DonationRecord.amount), 0)))
        payouts = db.scalar(
            select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                PaymentTransaction.status != "FAILED"
            )
        )
        db.add(
            PoolBalance(
                id=1,
                available_usd=Decimal(donations) - Decimal(payouts),
            )
        )
        db.commit()