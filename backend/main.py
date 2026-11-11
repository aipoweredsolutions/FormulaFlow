import base64
import hashlib
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
from hmac import compare_digest
import secrets
from typing import Annotated, Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
import requests

from backend.ai_agent import answer_admin_query
from backend.business_logic import (
    MAX_SUPPORT_AGE_MONTHS,
    add_months,
    age_in_months,
    calculate_monthly_cost,
    credit_pool_funds,
    get_available_pool_balance,
    month_start,
    release_pool_funds,
    reserve_pool_funds,
)
from backend.paypal_client import (
    PayPalError,
    capture_donation_order,
    create_donation_order,
    create_supplier_payout,
    get_supplier_payout_status,
    verify_webhook_signature,
)
from backend.database import (
    AdminAccount,
    AdminSession,
    DonationOrder,
    DonationRecord,
    FormulaRequest,
    PaymentTransaction,
    PayPalWebhookEvent,
    RequestAuditEvent,
    RequestLifecycleStatus,
    RequestStatus,
    SessionLocal,
    Vendor,
    VendorPriceHistory,
    create_db_tables,
)

logger = logging.getLogger(__name__)
PICKUP_TOKEN_TTL = timedelta(hours=24)
LLM_RATE_LIMIT = 30
LLM_RATE_WINDOW_SECONDS = 60
STATUS_LOOKUP_RATE_LIMIT = 10
STATUS_LOOKUP_RATE_WINDOW_SECONDS = 60
STATUS_LOOKUP_MAX_CLIENTS = 4096
_llm_request_times: dict[int, deque[float]] = defaultdict(deque)
_llm_rate_limit_lock = threading.Lock()
_status_lookup_times: dict[str, deque[float]] = {}
_status_lookup_rate_limit_lock = threading.Lock()


class PickupTokenConfigurationError(RuntimeError):
    pass


class RequestCreate(BaseModel):
    recipient_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=1, max_length=50)
    location: str = Field(min_length=1, max_length=500)
    county: str = Field(min_length=1, max_length=200)
    infant_age_months: int | None = Field(default=None, ge=0, le=23)
    birth_month: date | None = None
    formula_needs: str = Field(
        default="One-month formula supply", min_length=1, max_length=5000
    )

    @field_validator(
        "recipient_name", "phone_number", "location", "county", "formula_needs"
    )
    @classmethod
    def strip_and_validate(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("birth_month")
    @classmethod
    def require_birth_month_start(cls, value: date | None) -> date | None:
        if value is not None and value.day != 1:
            raise ValueError("birth month must be the first day of its month")
        if value is not None and month_start(value) > month_start(
            datetime.now(timezone.utc).date()
        ):
            raise ValueError("birth month cannot be in the future")
        return value

    @model_validator(mode="after")
    def require_age_or_birth_month(self) -> "RequestCreate":
        if self.birth_month is None and self.infant_age_months is None:
            raise ValueError("birth month and year are required")
        if self.birth_month is not None and self.infant_age_months is not None:
            raise ValueError("provide birth month and year, not a separate age")
        if self.birth_month is not None:
            age = age_in_months(
                self.birth_month,
                month_start(datetime.now(timezone.utc).date()),
            )
            if age >= MAX_SUPPORT_AGE_MONTHS:
                raise ValueError("Formula support is available only before 24 months")
        return self


class RequestUpdate(BaseModel):
    recipient_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone_number: str | None = Field(default=None, min_length=1, max_length=50)
    location: str | None = Field(default=None, min_length=1, max_length=500)
    county: str | None = Field(default=None, min_length=1, max_length=200)
    infant_age_months: int | None = Field(default=None, ge=0, le=23)
    birth_month: date | None = None
    formula_needs: str | None = Field(default=None, min_length=1, max_length=5000)

    @field_validator(
        "recipient_name",
        "phone_number",
        "location",
        "county",
        "formula_needs",
        mode="before",
    )
    @classmethod
    def strip_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("birth_month")
    @classmethod
    def require_birth_month_start(cls, value: date | None) -> date | None:
        if value is not None and value.day != 1:
            raise ValueError("birth month must be the first day of its month")
        if value is not None and month_start(value) > month_start(
            datetime.now(timezone.utc).date()
        ):
            raise ValueError("birth month cannot be in the future")
        return value


class RequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    public_reference_code: str
    recipient_name: str
    phone_number: str
    location: str
    county: str | None
    formula_needs: str
    status: RequestStatus
    matched_vendor: str | None
    infant_age_months: int | None
    birth_month: date | None
    continuity_id: int | None
    support_month: date | None
    containers_per_month: int | None
    total_cost_usd: Decimal | None
    match_reason: str | None
    lifecycle_status: RequestLifecycleStatus
    submitted_at: datetime
    approved_at: datetime | None
    payout_pending_at: datetime | None
    payout_confirmed_at: datetime | None
    ready_for_pickup_at: datetime | None
    collected_at: datetime | None
    rejected_at: datetime | None
    pickup_token_expires_at: datetime | None
    pickup_token_retrieved_at: datetime | None
    payout_status: str | None = None
    payout_batch_id: str | None = None
    payout_mode: str | None = None


class PayoutCreate(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    currency: str = Field(default="USD", pattern="^USD$")
    recipient_email: str = Field(min_length=3, max_length=254)

    @field_validator("recipient_email")
    @classmethod
    def validate_recipient_email(cls, value: str) -> str:
        value = value.strip()
        local_part, separator, domain = value.partition("@")
        if (
            not separator
            or not local_part
            or not domain
            or "." not in domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("must be a valid PayPal recipient email")
        return value


class PayoutResponse(BaseModel):
    request_id: int
    status: RequestStatus
    lifecycle_status: RequestLifecycleStatus
    payout_status: str
    payout_batch_id: str | None
    amount: Decimal
    currency: str
    payout_mode: str
    pickup_token: str | None = None


class PickupTokenResponse(BaseModel):
    token: str
    expires_at: datetime


class DonationCreate(BaseModel):
    amount: Decimal = Field(ge=1, le=10000, max_digits=7, decimal_places=2)


class DonationOrderResponse(BaseModel):
    order_id: str
    amount: Decimal
    currency: str
    status: str


class DonationCaptureResponse(BaseModel):
    order_id: str
    capture_id: str | None
    donation_id: int | None = None
    amount: Decimal
    currency: str
    status: str


class DonationRecordCreate(BaseModel):
    paypal_order_id: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(ge=1, le=10000, max_digits=7, decimal_places=2)


class DonationRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    paypal_order_id: str
    paypal_capture_id: str | None
    amount: Decimal
    currency: str


class VendorUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    paypal_email: str = Field(min_length=3, max_length=254)
    formula_cost: Decimal | None = Field(
        default=None, gt=0, max_digits=10, decimal_places=2
    )
    price_per_container_usd: Decimal | None = Field(
        default=None, gt=0, max_digits=10, decimal_places=2
    )
    location: str = Field(default="", min_length=0, max_length=500)
    county: str | None = Field(default=None, min_length=1, max_length=200)
    currency: str = Field(default="USD", pattern="^USD$")

    @field_validator("name", "paypal_email")
    @classmethod
    def strip_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("paypal_email")
    @classmethod
    def validate_paypal_email(cls, value: str) -> str:
        local_part, separator, domain = value.partition("@")
        if (
            not separator
            or not local_part
            or not domain
            or "." not in domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("must be a valid PayPal recipient email")
        return value

    @field_validator("location")
    @classmethod
    def strip_location(cls, value: str) -> str:
        return value.strip()

    @field_validator("county")
    @classmethod
    def strip_county(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("county must not be blank")
        return value


class VendorResponse(BaseModel):
    id: int
    name: str
    paypal_email: str
    formula_cost: Decimal
    location: str
    county: str | None
    price_per_container_usd: Decimal | None
    currency: str


class StudioDonationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: float
    currency: str
    created_at: datetime


class StudioPayoutResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: int
    amount: float
    containers: int | None
    currency: str
    payout_batch_id: str | None
    status: str
    payout_mode: str
    created_at: datetime


class SupplierPayoutReportItem(BaseModel):
    id: int
    request_id: int
    amount: Decimal
    containers: int | None
    confirmed_at: datetime


class SupplierPayoutReportResponse(BaseModel):
    supplier: str
    payment_count: int
    total_amount_usd: Decimal
    total_containers: int
    payments: list[SupplierPayoutReportItem]


class StudioRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    location: str
    county: str | None
    formula_needs: str
    status: RequestStatus
    lifecycle_status: RequestLifecycleStatus
    submitted_at: datetime
    approved_at: datetime | None
    payout_pending_at: datetime | None
    payout_confirmed_at: datetime | None
    ready_for_pickup_at: datetime | None
    collected_at: datetime | None
    rejected_at: datetime | None
    pickup_token_expires_at: datetime | None
    matched_vendor: str | None
    infant_age_months: int | None
    containers_per_month: int | None
    total_cost_usd: float | None


class StudioMerchantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    formula_cost: float
    location: str
    county: str | None
    price_per_container_usd: float | None
    currency: str


class StudioMerchantPriceHistoryResponse(BaseModel):
    vendor_id: int
    merchant_name: str
    previous_price_per_container_usd: float
    price_per_container_usd: float
    change_percent: float
    observed_at: datetime


class AdminPayoutCreate(BaseModel):
    request_id: int = Field(gt=0)
    containers: int | None = Field(default=None, ge=1)


class PartialPayoutCreate(BaseModel):
    containers: int = Field(ge=1)


class RequestDecisionCreate(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

class MerchantMatchCreate(BaseModel):
    merchant_id: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class RequestAuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: int
    action: str
    actor: str
    previous_status: str | None
    new_status: str | None
    previous_lifecycle_status: str | None
    new_lifecycle_status: str | None
    note: str | None
    merchant_id: int | None
    merchant_name: str | None
    created_at: datetime


class AdminAccountCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        local_part, separator, domain = value.partition("@")
        if (
            not separator
            or not local_part
            or "." not in domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("must be a valid email address")
        return value


class AdminLoginCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class AdminPasswordChangeCreate(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class AdminAccountResponse(BaseModel):
    id: int
    email: str
    role: str


class AdminLoginResponse(AdminAccountResponse):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AdminQueryCreate(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class LlmProxyRequest(BaseModel):
    input: list[dict[str, object]] = Field(min_length=1, max_length=200)
    instructions: str | None = Field(default=None, max_length=50000)
    tools: list[dict[str, object]] = Field(default_factory=list, max_length=100)
    tool_choice: str | dict[str, object] | None = None
    parallel_tool_calls: bool = True
    text: dict[str, object] | None = None
    reasoning: dict[str, object] | None = None


class PickupRedeemCreate(BaseModel):
    token: str = Field(min_length=20, max_length=100)


class PublicRequestStatusLookup(BaseModel):
    reference_code: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]{32}$",
    )


class PublicRequestStatusResponse(BaseModel):
    stage: Literal["submitted", "approved", "ready_for_pickup", "collected"]
    last_updated_at: datetime


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


bearer_scheme = HTTPBearer(auto_error=False)


def hash_admin_password(password: str) -> str:
    salt = os.urandom(16)
    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
        maxmem=128 * 1024 * 1024,
    )
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    key_text = base64.urlsafe_b64encode(derived_key).decode("ascii").rstrip("=")
    return f"scrypt${salt_text}${key_text}"


def verify_admin_password(password: str, password_hash: str) -> bool:
    try:
        scheme, salt_text, expected_text = password_hash.split("$", maxsplit=2)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(
            expected_text + "=" * (-len(expected_text) % 4)
        )
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=len(expected),
            maxmem=128 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return compare_digest(actual, expected)


def get_admin_session(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[Session, Depends(get_db)],
) -> tuple[AdminAccount, AdminSession]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    stored_session = db.scalar(
        select(AdminSession).where(
            AdminSession.token_hash
            == hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest(),
            AdminSession.expires_at > func.current_timestamp(),
            AdminSession.revoked_at.is_(None),
        )
    )
    if stored_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session is invalid or expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    account = db.get(AdminAccount, stored_session.account_id)
    if account is None or account.role != "admin" or not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not permitted to access admin operations",
        )
    return account, stored_session


def require_admin(
    session: Annotated[tuple[AdminAccount, AdminSession], Depends(get_admin_session)],
) -> AdminAccount:
    return session[0]


def enforce_llm_rate_limit(account_id: int) -> None:
    now = time.monotonic()
    cutoff = now - LLM_RATE_WINDOW_SECONDS
    with _llm_rate_limit_lock:
        requests_in_window = _llm_request_times[account_id]
        while requests_in_window and requests_in_window[0] <= cutoff:
            requests_in_window.popleft()
        if len(requests_in_window) >= LLM_RATE_LIMIT:
            retry_after = max(
                1,
                math.ceil(
                    LLM_RATE_WINDOW_SECONDS - (now - requests_in_window[0])
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="AI request limit reached. Try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )
        requests_in_window.append(now)


def enforce_status_lookup_rate_limit(client_ip: str) -> None:
    now = time.monotonic()
    cutoff = now - STATUS_LOOKUP_RATE_WINDOW_SECONDS
    with _status_lookup_rate_limit_lock:
        expired_clients = [
            ip
            for ip, requests_in_window in _status_lookup_times.items()
            if not requests_in_window or requests_in_window[-1] <= cutoff
        ]
        for ip in expired_clients:
            del _status_lookup_times[ip]

        requests_in_window = _status_lookup_times.get(client_ip)
        if requests_in_window is None:
            if len(_status_lookup_times) >= STATUS_LOOKUP_MAX_CLIENTS:
                _status_lookup_times.pop(next(iter(_status_lookup_times)))
            requests_in_window = deque()
            _status_lookup_times[client_ip] = requests_in_window

        while requests_in_window and requests_in_window[0] <= cutoff:
            requests_in_window.popleft()
        if len(requests_in_window) >= STATUS_LOOKUP_RATE_LIMIT:
            retry_after = max(
                1,
                math.ceil(
                    requests_in_window[0]
                    + STATUS_LOOKUP_RATE_WINDOW_SECONDS
                    - now
                ),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many status lookups. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        requests_in_window.append(now)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    create_db_tables()
    yield


app = FastAPI(title="FormulaFlow Requests API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "FRONTEND_ORIGINS", "http://localhost:5173"
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health_check(
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    try:
        db.scalar(select(1))
    except SQLAlchemyError as exc:
        logger.exception("Health check failed because the database is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        ) from exc
    return {"status": "ok"}


@app.get("/api/auth/status")
def admin_registration_status(
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    has_admin = db.scalar(select(AdminAccount.id).limit(1)) is not None
    return {"registration_open": not has_admin}


@app.post(
    "/api/auth/register",
    response_model=AdminAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_admin(
    account_data: AdminAccountCreate,
    db: Annotated[Session, Depends(get_db)],
) -> AdminAccount:
    if db.scalar(select(AdminAccount.id).limit(1)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The initial admin account already exists. Please log in.",
        )
    account = AdminAccount(
        email=account_data.email,
        password_hash=hash_admin_password(account_data.password.get_secret_value()),
        role="admin",
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The initial admin account already exists. Please log in.",
        ) from exc
    db.refresh(account)
    return account


@app.post("/api/auth/login", response_model=AdminLoginResponse)
def login_admin(
    credentials: AdminLoginCreate,
    db: Annotated[Session, Depends(get_db)],
) -> AdminLoginResponse:
    account = db.scalar(
        select(AdminAccount).where(AdminAccount.email == credentials.email)
    )
    if (
        account is None
        or not account.is_active
        or account.role != "admin"
        or not verify_admin_password(
            credentials.password.get_secret_value(), account.password_hash
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = secrets.token_urlsafe(32)
    expires_in = 12 * 60 * 60
    admin_session = AdminSession(
        account_id=account.id,
        token_hash=hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )
    db.add(admin_session)
    db.commit()
    return AdminLoginResponse(
        id=account.id,
        email=account.email,
        role=account.role,
        access_token=access_token,
        expires_in=expires_in,
    )


@app.get("/api/auth/me", response_model=AdminAccountResponse)
def current_admin(
    admin: Annotated[AdminAccount, Depends(require_admin)],
) -> AdminAccount:
    return admin


@app.post("/api/auth/change-password", response_model=AdminLoginResponse)
def change_admin_password(
    password_change: AdminPasswordChangeCreate,
    session: Annotated[
        tuple[AdminAccount, AdminSession], Depends(get_admin_session)
    ],
    db: Annotated[Session, Depends(get_db)],
) -> AdminLoginResponse:
    account, _ = session
    if not verify_admin_password(
        password_change.current_password.get_secret_value(),
        account.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        )

    account.password_hash = hash_admin_password(
        password_change.new_password.get_secret_value()
    )
    db.execute(
        update(AdminSession)
        .where(
            AdminSession.account_id == account.id,
            AdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=func.current_timestamp())
        .execution_options(synchronize_session=False)
    )

    access_token = secrets.token_urlsafe(32)
    expires_in = 12 * 60 * 60
    db.add(
        AdminSession(
            account_id=account.id,
            token_hash=hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )
    )
    db.commit()
    db.refresh(account)
    return AdminLoginResponse(
        id=account.id,
        email=account.email,
        role=account.role,
        access_token=access_token,
        expires_in=expires_in,
    )


@app.post("/api/auth/logout")
def logout_admin(
    session: Annotated[tuple[AdminAccount, AdminSession], Depends(get_admin_session)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    admin_session = session[1]
    admin_session.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return {"detail": "Logged out"}


def record_request_event(
    db: Session,
    formula_request: FormulaRequest,
    *,
    action: str,
    actor: str,
    previous_status: str | None = None,
    previous_lifecycle_status: str | None = None,
    note: str | None = None,
    merchant: Vendor | None = None,
) -> None:
    db.add(
        RequestAuditEvent(
            request_id=formula_request.id,
            action=action,
            actor=actor[:120],
            previous_status=previous_status,
            new_status=formula_request.status,
            previous_lifecycle_status=previous_lifecycle_status,
            new_lifecycle_status=formula_request.lifecycle_status,
            note=note,
            merchant_id=merchant.id if merchant else None,
            merchant_name=merchant.name if merchant else None,
        )
    )


def _pickup_token_fernet() -> Fernet:
    key = os.getenv("PICKUP_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise PickupTokenConfigurationError(
            "PICKUP_TOKEN_ENCRYPTION_KEY is required to issue pickup tokens"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise PickupTokenConfigurationError(
            "PICKUP_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def _issue_pickup_token(formula_request: FormulaRequest) -> None:
    token = secrets.token_urlsafe(24)
    encrypted_token = _pickup_token_fernet().encrypt(token.encode("utf-8"))
    now = datetime.now(timezone.utc)
    formula_request.pickup_token_hash = sha256(token.encode("utf-8")).hexdigest()
    formula_request.pickup_token_encrypted = encrypted_token.decode("ascii")
    formula_request.pickup_token_used_at = None
    formula_request.pickup_token_expires_at = now + PICKUP_TOKEN_TTL
    formula_request.pickup_token_retrieved_at = None


def _simulated_payouts_enabled() -> bool:
    configured = os.getenv("PAYPAL_PAYOUT_SIMULATED", "false").strip().lower()
    if configured not in {"true", "false"}:
        raise RuntimeError("PAYPAL_PAYOUT_SIMULATED must be either true or false")
    return configured == "true"


def _record_captured_donation(
    db: Session, donation_order: DonationOrder
) -> DonationRecord:
    if (
        donation_order.status != "COMPLETED"
        or not donation_order.paypal_order_id
        or not donation_order.paypal_capture_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Donation order has not been successfully captured",
        )

    record = db.scalar(
        select(DonationRecord).where(
            DonationRecord.paypal_order_id == donation_order.paypal_order_id
        )
    )
    if record is not None:
        if record.paypal_capture_id != donation_order.paypal_capture_id:
            record.paypal_capture_id = donation_order.paypal_capture_id
            db.commit()
        return record

    record = DonationRecord(
        paypal_order_id=donation_order.paypal_order_id,
        paypal_capture_id=donation_order.paypal_capture_id,
        amount=donation_order.amount,
        currency=donation_order.currency,
    )
    db.add(record)
    credit_pool_funds(db, donation_order.amount)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        record = db.scalar(
            select(DonationRecord).where(
                DonationRecord.paypal_order_id == donation_order.paypal_order_id
            )
        )
        if record is None:
            capture_record = db.scalar(
                select(DonationRecord).where(
                    DonationRecord.paypal_capture_id
                    == donation_order.paypal_capture_id
                )
            )
            if capture_record is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="PayPal capture is already recorded for another donation",
                )
            raise
        if record.paypal_capture_id != donation_order.paypal_capture_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="PayPal capture is already recorded for another donation",
            )
        return record

    db.refresh(record)
    return record


def _webhook_text(value: object, max_length: int = 100) -> str | None:
    return (
        value
        if isinstance(value, str) and value and len(value) <= max_length
        else None
    )


def _handle_capture_webhook(
    db: Session, event_type: str, resource: dict[str, object]
) -> None:
    capture_id = _webhook_text(resource.get("id"))
    related = resource.get("supplementary_data")
    related_ids = related.get("related_ids") if isinstance(related, dict) else None
    order_id = (
        _webhook_text(related_ids.get("order_id"))
        if isinstance(related_ids, dict)
        else None
    )
    if not capture_id or not order_id:
        logger.warning(
            "Verified PayPal capture webhook is missing IDs",
            extra={"event_type": event_type},
        )
        return

    donation_order = db.scalar(
        select(DonationOrder).where(DonationOrder.paypal_order_id == order_id)
    )
    if donation_order is None:
        logger.warning(
            "Verified PayPal capture webhook did not match a local order",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return

    if event_type == "PAYMENT.CAPTURE.DENIED":
        if donation_order.status not in {"COMPLETED", "REVIEW"}:
            donation_order.status = "FAILED"
            donation_order.error_message = "PayPal reported that the capture was denied"
        return

    amount = resource.get("amount")
    if not isinstance(amount, dict):
        logger.warning(
            "Verified PayPal capture webhook is missing amount",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return
    try:
        captured_amount = Decimal(str(amount.get("value")))
    except (InvalidOperation, ValueError):
        logger.warning(
            "Verified PayPal capture webhook has an invalid amount",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return
    if (
        not captured_amount.is_finite()
        or captured_amount != donation_order.amount
        or amount.get("currency_code") != donation_order.currency
    ):
        donation_order.status = "REVIEW"
        donation_order.error_message = (
            "Verified webhook capture did not match the recorded order amount"
        )
        logger.error(
            "Verified PayPal capture does not match local order",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return
    if (
        donation_order.paypal_capture_id
        and donation_order.paypal_capture_id != capture_id
    ):
        donation_order.status = "REVIEW"
        donation_order.error_message = (
            "Verified webhook capture ID does not match the recorded capture"
        )
        logger.error(
            "Verified PayPal capture ID conflicts with local order",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return

    existing_capture = db.scalar(
        select(DonationRecord).where(
            DonationRecord.paypal_capture_id == capture_id
        )
    )
    if existing_capture is not None and existing_capture.paypal_order_id != order_id:
        donation_order.status = "REVIEW"
        donation_order.error_message = (
            "Verified webhook capture ID is already linked to another order"
        )
        logger.error(
            "Verified PayPal capture ID is already recorded",
            extra={"event_type": event_type, "order_id": order_id},
        )
        return

    donation_order.paypal_capture_id = capture_id
    donation_order.status = "COMPLETED"
    donation_order.error_message = None
    _record_captured_donation(db, donation_order)


def _handle_payout_webhook(
    db: Session, event_type: str, resource: dict[str, object]
) -> bool:
    batch_header = resource.get("batch_header")
    if not isinstance(batch_header, dict):
        batch_header = resource
    batch_id = _webhook_text(batch_header.get("payout_batch_id")) or _webhook_text(
        batch_header.get("id")
    )
    sender_header = batch_header.get("sender_batch_header")
    sender_batch_id = (
        _webhook_text(sender_header.get("sender_batch_id"))
        if isinstance(sender_header, dict)
        else None
    )
    if not batch_id and not sender_batch_id:
        logger.warning(
            "Verified PayPal payout webhook is missing batch identifiers",
            extra={"event_type": event_type},
        )
        return False
    transaction = None
    if batch_id:
        transaction = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.payout_batch_id == batch_id
            )
        )
    if transaction is None and sender_batch_id:
        transaction = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.sender_batch_id == sender_batch_id
            )
        )
    if transaction is None:
        logger.warning(
            "Verified PayPal payout webhook did not match a local batch",
            extra={
                "event_type": event_type,
                "payout_batch_id": batch_id,
                "sender_batch_id": sender_batch_id,
            },
        )
        return False
    event_batch_id = batch_id
    batch_id = batch_id or transaction.payout_batch_id or sender_batch_id
    if not batch_id:
        return
    if event_batch_id and not transaction.payout_batch_id:
        transaction.payout_batch_id = event_batch_id

    formula_request = db.get(FormulaRequest, transaction.request_id)
    if formula_request is None:
        logger.error(
            "PayPal payout batch references a missing request",
            extra={"payout_batch_id": batch_id},
        )
        return False
    if transaction.payout_mode == "SIMULATED":
        logger.warning(
            "Verified PayPal payout webhook matched a simulated payout",
            extra={"payout_batch_id": batch_id},
        )
        return True

    provider_status = _webhook_text(batch_header.get("batch_status"))
    provider_status = (provider_status or "").upper()
    if event_type.endswith(".SUCCESS"):
        next_status = "SUCCESS"
    elif event_type.endswith((".DENIED", ".CANCELED", ".RETURNED")):
        next_status = "FAILED"
    elif event_type.endswith(".PROCESSING"):
        next_status = "PROCESSING"
    elif provider_status in {"SUCCESS"}:
        next_status = "SUCCESS"
    elif provider_status in {"DENIED", "CANCELED", "RETURNED", "FAILED", "BLOCKED"}:
        next_status = "FAILED"
    elif provider_status in {"PROCESSING", "PENDING", "NEW", "ONHOLD"}:
        next_status = "PROCESSING"
    else:
        logger.warning(
            "Verified PayPal payout webhook has an unsupported status",
            extra={"event_type": event_type, "payout_batch_id": batch_id},
        )
        return False

    current_status = transaction.status.upper()
    returned = event_type.endswith(".RETURNED") or provider_status == "RETURNED"
    if current_status == "FAILED":
        return True
    if next_status == "SUCCESS" and formula_request.lifecycle_status in {
        RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
        RequestLifecycleStatus.READY_FOR_PICKUP.value,
        RequestLifecycleStatus.COLLECTED.value,
    }:
        return True
    if returned and formula_request.lifecycle_status == RequestLifecycleStatus.COLLECTED.value:
        logger.warning(
            "Ignoring a payout return event after pickup was collected",
            extra={"payout_batch_id": batch_id},
        )
        return True
    if next_status == "PROCESSING":
        transaction.status = "PROCESSING"
        return True

    transaction.status = next_status
    if next_status == "SUCCESS":
        transaction.error_message = None
        _confirm_payout(
            db,
            formula_request,
            transaction,
            actor="PayPal webhook",
            note=f"Verified PayPal webhook confirmed payout batch {batch_id}.",
        )
        return True

    transaction.error_message = f"PayPal reported payout batch status {provider_status or next_status}"
    release_pool_funds(db, transaction.amount)
    previous_lifecycle_status = formula_request.lifecycle_status
    formula_request.status = RequestStatus.APPROVED.value
    formula_request.lifecycle_status = RequestLifecycleStatus.APPROVED.value
    formula_request.pickup_token_hash = None
    formula_request.pickup_token_encrypted = None
    formula_request.pickup_token_used_at = None
    formula_request.pickup_token_expires_at = None
    formula_request.pickup_token_retrieved_at = None
    record_request_event(
        db,
        formula_request,
        action="PAYOUT_STATUS_UPDATED",
        actor="PayPal webhook",
        previous_status=RequestStatus.APPROVED.value,
        previous_lifecycle_status=previous_lifecycle_status,
        note=f"Verified PayPal webhook reported payout batch {batch_id} as FAILED.",
    )
    return True


def _confirm_payout(
    db: Session,
    formula_request: FormulaRequest,
    transaction: PaymentTransaction,
    *,
    actor: str,
    note: str,
) -> None:
    if formula_request.lifecycle_status in {
        RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
        RequestLifecycleStatus.READY_FOR_PICKUP.value,
        RequestLifecycleStatus.COLLECTED.value,
    }:
        return
    now = datetime.now(timezone.utc)
    previous_lifecycle_status = formula_request.lifecycle_status
    formula_request.status = RequestStatus.PAID.value
    formula_request.lifecycle_status = RequestLifecycleStatus.PAYOUT_CONFIRMED.value
    formula_request.payout_confirmed_at = now
    record_request_event(
        db,
        formula_request,
        action="PAYOUT_CONFIRMED",
        actor=actor,
        previous_status=RequestStatus.APPROVED.value,
        previous_lifecycle_status=previous_lifecycle_status,
        note=note,
    )
    _issue_pickup_token(formula_request)
    formula_request.lifecycle_status = RequestLifecycleStatus.READY_FOR_PICKUP.value
    formula_request.ready_for_pickup_at = now
    record_request_event(
        db,
        formula_request,
        action="PICKUP_READY",
        actor=actor,
        previous_status=RequestStatus.PAID.value,
        previous_lifecycle_status=RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
        note="A single-use pickup token was generated; it expires in 24 hours.",
    )


@app.post("/api/paypal/webhook")
async def paypal_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str | bool]:
    try:
        event = await request.json()
    except ValueError as exc:
        logger.warning("Rejected PayPal webhook with invalid JSON")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        ) from exc
    if not isinstance(event, dict):
        logger.warning("Rejected PayPal webhook with invalid payload type")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        )

    event_id = _webhook_text(event.get("id"))
    event_type = _webhook_text(event.get("event_type"))
    transmission_id = request.headers.get("paypal-transmission-id", "")
    required_headers = {
        "auth_algo": request.headers.get("paypal-auth-algo", ""),
        "cert_url": request.headers.get("paypal-cert-url", ""),
        "transmission_id": transmission_id,
        "transmission_sig": request.headers.get("paypal-transmission-sig", ""),
        "transmission_time": request.headers.get("paypal-transmission-time", ""),
    }
    if not event_id or not event_type or any(not value for value in required_headers.values()):
        logger.warning(
            "Rejected PayPal webhook with missing ID, type, or signature headers",
            extra={"event_id": event_id, "event_type": event_type},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook event or signature headers are incomplete",
        )

    try:
        verified = verify_webhook_signature(
            **required_headers,
            webhook_event=event,
        )
    except PayPalError as exc:
        logger.error(
            "PayPal webhook signature verification unavailable",
            extra={"event_id": event_id, "event_type": event_type},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify PayPal webhook; retry delivery",
        ) from exc

    if not verified:
        logger.warning(
            "Rejected unverified PayPal webhook",
            extra={"event_id": event_id, "event_type": event_type},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PayPal webhook signature verification failed",
        )

    existing_event = db.scalar(
        select(PayPalWebhookEvent).where(PayPalWebhookEvent.event_id == event_id)
    )
    if existing_event is not None:
        return {"received": True, "duplicate": True}

    db.add(PayPalWebhookEvent(event_id=event_id, event_type=event_type))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_event = db.scalar(
            select(PayPalWebhookEvent).where(PayPalWebhookEvent.event_id == event_id)
        )
        if existing_event is not None:
            return {"received": True, "duplicate": True}
        raise

    resource = event.get("resource")
    if isinstance(resource, dict):
        if event_type in {
            "PAYMENT.CAPTURE.COMPLETED",
            "PAYMENT.CAPTURE.DENIED",
        }:
            if (
                event_type == "PAYMENT.CAPTURE.COMPLETED"
                and resource.get("status") != "COMPLETED"
            ):
                logger.warning(
                    "Verified PayPal capture completion event has a non-completed status",
                    extra={"event_id": event_id, "event_type": event_type},
                )
            else:
                _handle_capture_webhook(db, event_type, resource)
        elif event_type.startswith("PAYMENT.PAYOUTSBATCH."):
            try:
                matched = _handle_payout_webhook(db, event_type, resource)
            except PickupTokenConfigurationError as exc:
                db.rollback()
                logger.error(
                    "Could not issue pickup token for verified PayPal payout",
                    extra={"event_id": event_id},
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Pickup token encryption is not configured; retry webhook delivery",
                ) from exc
            if not matched:
                db.rollback()
                logger.warning(
                    "PayPal payout webhook was not acknowledged because no local batch matched",
                    extra={"event_id": event_id, "event_type": event_type},
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Payout batch is not matched locally; retry webhook delivery",
                )
    else:
        logger.warning(
            "Verified PayPal webhook has no resource object",
            extra={"event_id": event_id, "event_type": event_type},
        )

    if db.scalar(
        select(PayPalWebhookEvent.id).where(PayPalWebhookEvent.event_id == event_id)
    ) is None:
        db.add(PayPalWebhookEvent(event_id=event_id, event_type=event_type))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_event = db.scalar(
            select(PayPalWebhookEvent).where(PayPalWebhookEvent.event_id == event_id)
        )
        if existing_event is not None:
            return {"received": True, "duplicate": True}
        raise
    return {"received": True, "duplicate": False}


@app.post(
    "/api/donations/orders",
    response_model=DonationOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_donation(
    donation: DonationCreate,
    db: Annotated[Session, Depends(get_db)],
) -> DonationOrderResponse:
    donation_order = DonationOrder(
        amount=donation.amount,
        currency="USD",
        status="CREATING",
    )
    db.add(donation_order)
    db.commit()
    db.refresh(donation_order)
    try:
        result = create_donation_order(
            donation_id=donation_order.id,
            amount=donation_order.amount,
            currency=donation_order.currency,
        )
    except PayPalError as exc:
        donation_order.status = "UNKNOWN" if exc.outcome_unknown else "FAILED"
        donation_order.error_message = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    paypal_order_id = result["order_id"]
    donation_order.paypal_order_id = paypal_order_id
    donation_order.status = "CREATED"
    db.commit()
    return DonationOrderResponse(
        order_id=paypal_order_id,
        amount=donation_order.amount,
        currency=donation_order.currency,
        status=donation_order.status,
    )


@app.post(
    "/api/donations/orders/{order_id}/capture",
    response_model=DonationCaptureResponse,
)
def capture_donation(
    order_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> DonationCaptureResponse:
    donation_order = db.scalar(
        select(DonationOrder).where(DonationOrder.paypal_order_id == order_id)
    )
    if donation_order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Donation order not found",
        )
    if donation_order.status == "COMPLETED":
        record = _record_captured_donation(db, donation_order)
        return DonationCaptureResponse(
            order_id=order_id,
            capture_id=donation_order.paypal_capture_id,
            donation_id=record.id,
            amount=donation_order.amount,
            currency=donation_order.currency,
            status=donation_order.status,
        )
    if donation_order.status != "CREATED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Donation order is not available for capture",
        )

    donation_order.status = "CAPTURING"
    db.commit()
    try:
        result = capture_donation_order(
            donation_id=donation_order.id,
            order_id=order_id,
        )
    except PayPalError as exc:
        donation_order.status = "UNKNOWN" if exc.outcome_unknown else "FAILED"
        donation_order.error_message = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    donation_order.paypal_capture_id = str(result["capture_id"])
    captured_amount = result["amount"]
    captured_currency = str(result["currency"])
    if (
        captured_amount != donation_order.amount
        or captured_currency != donation_order.currency
    ):
        donation_order.status = "REVIEW"
        donation_order.error_message = "PayPal capture amount did not match the order"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Captured donation does not match the original order; contact support",
        )
    if (
        result["order_status"] == "COMPLETED"
        and result["capture_status"] == "COMPLETED"
    ):
        donation_order.status = "COMPLETED"
        donation_order.error_message = None
        record = _record_captured_donation(db, donation_order)
    else:
        donation_order.status = "PENDING"
        donation_order.error_message = "PayPal has not completed this capture"
        db.commit()
    return DonationCaptureResponse(
        order_id=order_id,
        capture_id=donation_order.paypal_capture_id,
        donation_id=record.id if donation_order.status == "COMPLETED" else None,
        amount=donation_order.amount,
        currency=donation_order.currency,
        status=donation_order.status,
    )


@app.post(
    "/api/donations/record",
    response_model=DonationRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_donation(
    donation: DonationRecordCreate,
    db: Annotated[Session, Depends(get_db)],
) -> DonationRecordResponse:
    donation_order = db.scalar(
        select(DonationOrder).where(
            DonationOrder.paypal_order_id == donation.paypal_order_id
        )
    )
    if donation_order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Donation order not found",
        )
    if donation_order.status != "COMPLETED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Donation order has not been successfully captured",
        )
    if donation.amount != donation_order.amount:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Donation amount does not match the verified PayPal capture",
        )

    record = _record_captured_donation(db, donation_order)
    return DonationRecordResponse.model_validate(record)


@app.put(
    "/api/admin/vendors/{vendor_name}",
    response_model=VendorResponse,
)
def update_vendor(
    vendor_name: str,
    vendor_update: VendorUpdate,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VendorResponse:
    unit_price = (
        vendor_update.price_per_container_usd or vendor_update.formula_cost
    )
    if unit_price is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="price_per_container_usd is required",
        )
    vendor = db.scalar(select(Vendor).where(Vendor.name == vendor_name))
    if vendor is None:
        if not vendor_update.location:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A merchant location is required for local matching",
            )
        vendor = Vendor(
            name=vendor_update.name,
            paypal_email=vendor_update.paypal_email,
            formula_cost=unit_price,
            price_per_container_usd=unit_price,
            location=vendor_update.location,
            county=vendor_update.county,
            currency=vendor_update.currency,
        )
        db.add(vendor)
    else:
        if vendor_update.name != vendor_name:
            duplicate = db.scalar(
                select(Vendor).where(Vendor.name == vendor_update.name)
            )
            if duplicate is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A vendor with that name already exists",
                )
        previous_price = vendor.price_per_container_usd
        vendor.name = vendor_update.name
        vendor.paypal_email = vendor_update.paypal_email
        vendor.formula_cost = unit_price
        vendor.price_per_container_usd = unit_price
        vendor.location = vendor_update.location or vendor.location
        if vendor_update.county is not None:
            vendor.county = vendor_update.county
        vendor.currency = vendor_update.currency
        if previous_price != unit_price:
            db.add(
                VendorPriceHistory(
                    vendor_id=vendor.id,
                    price_per_container_usd=unit_price,
                )
            )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A vendor with that name already exists",
        ) from exc
    db.refresh(vendor)
    if vendor.id and not db.scalar(
        select(VendorPriceHistory.id)
        .where(VendorPriceHistory.vendor_id == vendor.id)
        .limit(1)
    ):
        db.add(
            VendorPriceHistory(
                vendor_id=vendor.id,
                price_per_container_usd=unit_price,
            )
        )
        db.commit()
    return VendorResponse(
        id=vendor.id,
        name=vendor.name,
        paypal_email=vendor.paypal_email,
        formula_cost=vendor.formula_cost,
        location=vendor.location,
        county=vendor.county,
        price_per_container_usd=vendor.price_per_container_usd,
        currency=vendor.currency,
    )


@app.get("/api/admin/vendors/{vendor_name}", response_model=VendorResponse)
def get_vendor(
    vendor_name: str,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> VendorResponse:
    vendor = db.scalar(select(Vendor).where(Vendor.name == vendor_name))
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )
    return VendorResponse(
        id=vendor.id,
        name=vendor.name,
        paypal_email=vendor.paypal_email,
        formula_cost=vendor.formula_cost,
        location=vendor.location,
        county=vendor.county,
        price_per_container_usd=vendor.price_per_container_usd,
        currency=vendor.currency,
    )


@app.delete("/api/admin/vendors/{vendor_name}")
def delete_vendor(
    vendor_name: str,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str | bool]:
    vendor = db.scalar(select(Vendor).where(Vendor.name == vendor_name))
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )
    active_match_count = db.scalar(
        select(func.count(FormulaRequest.id)).where(
            FormulaRequest.matched_vendor == vendor.name,
        )
    )
    if active_match_count and int(active_match_count) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Vendor is still matched to active requests",
        )
    db.execute(
        delete(VendorPriceHistory).where(VendorPriceHistory.vendor_id == vendor.id)
    )
    db.delete(vendor)
    db.commit()
    return {"deleted": True, "name": vendor_name}


@app.get("/api/admin/vendors", response_model=list[VendorResponse])
def list_vendors(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[VendorResponse]:
    vendors = db.scalars(select(Vendor).order_by(Vendor.name)).all()
    return [
        VendorResponse(
            id=vendor.id,
            name=vendor.name,
            paypal_email=vendor.paypal_email,
            formula_cost=vendor.formula_cost,
            location=vendor.location,
            county=vendor.county,
            price_per_container_usd=vendor.price_per_container_usd,
            currency=vendor.currency,
        )
        for vendor in vendors
    ]


@app.get("/api/admin/requests/{request_id}", response_model=RequestResponse)
def get_request(
    request_id: int,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FormulaRequest:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )
    payout = db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.request_id == request_id)
        .order_by(PaymentTransaction.id.desc())
        .limit(1)
    )
    response = RequestResponse.model_validate(formula_request)
    if payout is not None:
        response = response.model_copy(
            update={
                "payout_status": payout.status,
                "payout_batch_id": payout.payout_batch_id,
                "payout_mode": payout.payout_mode,
            }
        )
    return response


@app.patch("/api/admin/requests/{request_id}", response_model=RequestResponse)
def update_request(
    request_id: int,
    update: RequestUpdate,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FormulaRequest:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )

    for field_name, value in update.model_dump(exclude_unset=True).items():
        if field_name == "recipient_name":
            formula_request.recipient_name = value
        elif field_name == "phone_number":
            formula_request.phone_number = value
        elif field_name == "location":
            formula_request.location = value
        elif field_name == "county":
            formula_request.county = value
        elif field_name == "formula_needs":
            formula_request.formula_needs = value
        elif field_name == "infant_age_months":
            formula_request.infant_age_months = value
        elif field_name == "birth_month":
            if value is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A recorded birth month cannot be cleared",
                )
            formula_request.birth_month = value
            if formula_request.support_month is None:
                source_date = formula_request.submitted_at.date()
                formula_request.support_month = month_start(source_date)
            if formula_request.continuity_id is None:
                formula_request.continuity_id = formula_request.id
            formula_request.infant_age_months = age_in_months(
                value, formula_request.support_month
            )

    db.commit()
    db.refresh(formula_request)
    return formula_request


@app.get(
    "/api/admin/payouts",
    response_model=list[StudioPayoutResponse],
)
def list_payouts(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[PaymentTransaction]:
    return db.scalars(
        select(PaymentTransaction)
        .order_by(PaymentTransaction.created_at.desc(), PaymentTransaction.id.desc())
        .limit(500)
    ).all()


@app.get(
    "/api/admin/donations",
    response_model=list[DonationRecordResponse],
)
def list_donations(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[DonationRecordResponse]:
    return db.scalars(
        select(DonationRecord)
        .order_by(DonationRecord.created_at.desc(), DonationRecord.id.desc())
        .limit(500)
    ).all()


@app.get(
    "/api/admin/studio/donations",
    response_model=list[StudioDonationResponse],
)
def list_studio_donations(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[StudioDonationResponse]:
    return db.scalars(
        select(DonationRecord)
        .order_by(DonationRecord.created_at.desc(), DonationRecord.id.desc())
        .limit(500)
    ).all()


@app.get(
    "/api/admin/studio/payouts",
    response_model=list[StudioPayoutResponse],
)
def list_studio_payouts(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[StudioPayoutResponse]:
    return db.scalars(
        select(PaymentTransaction)
        .order_by(PaymentTransaction.created_at.desc(), PaymentTransaction.id.desc())
        .limit(500)
    ).all()


@app.get(
    "/api/admin/reports/supplier-payouts",
    response_model=SupplierPayoutReportResponse,
)
def supplier_payout_report(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    supplier: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> SupplierPayoutReportResponse:
    supplier_name = supplier.strip()
    if not supplier_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A supplier name is required",
        )
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="End date must be on or after the start date",
        )

    filters = [
        FormulaRequest.matched_vendor == supplier_name,
        PaymentTransaction.payout_mode == "PAYPAL",
        PaymentTransaction.status == "SUCCESS",
        FormulaRequest.lifecycle_status.in_(
            [
                RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
                RequestLifecycleStatus.READY_FOR_PICKUP.value,
                RequestLifecycleStatus.COLLECTED.value,
            ]
        ),
        FormulaRequest.payout_confirmed_at.is_not(None),
    ]
    if start_date:
        filters.append(
            FormulaRequest.payout_confirmed_at
            >= datetime.combine(start_date, datetime_time.min, tzinfo=timezone.utc)
        )
    if end_date:
        filters.append(
            FormulaRequest.payout_confirmed_at
            < datetime.combine(
                end_date + timedelta(days=1),
                datetime_time.min,
                tzinfo=timezone.utc,
            )
        )

    aggregate = db.execute(
        select(
            func.count(PaymentTransaction.id),
            func.coalesce(func.sum(PaymentTransaction.amount), 0),
            func.coalesce(func.sum(PaymentTransaction.containers), 0),
        )
        .join(FormulaRequest, PaymentTransaction.request_id == FormulaRequest.id)
        .where(*filters)
    ).one()
    transactions = db.execute(
        select(
            PaymentTransaction.id,
            PaymentTransaction.request_id,
            PaymentTransaction.amount,
            PaymentTransaction.containers,
            FormulaRequest.payout_confirmed_at,
        )
        .join(FormulaRequest, PaymentTransaction.request_id == FormulaRequest.id)
        .where(*filters)
        .order_by(
            FormulaRequest.payout_confirmed_at.desc(),
            PaymentTransaction.id.desc(),
        )
        .limit(500)
    ).all()
    return SupplierPayoutReportResponse(
        supplier=supplier_name,
        payment_count=int(aggregate[0] or 0),
        total_amount_usd=Decimal(aggregate[1] or 0),
        total_containers=int(aggregate[2] or 0),
        payments=[
            SupplierPayoutReportItem(
                id=transaction_id,
                request_id=request_id,
                amount=amount_value,
                containers=containers,
                confirmed_at=confirmed_at,
            )
            for (
                transaction_id,
                request_id,
                amount_value,
                containers,
                confirmed_at,
            ) in transactions
        ],
    )


@app.get(
    "/api/admin/studio/requests",
    response_model=list[StudioRequestResponse],
)
def list_studio_requests(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[StudioRequestResponse]:
    return db.scalars(
        select(FormulaRequest)
        .order_by(FormulaRequest.created_at.desc(), FormulaRequest.id.desc())
        .limit(500)
    ).all()


@app.get(
    "/api/admin/studio/merchants",
    response_model=list[StudioMerchantResponse],
)
def list_studio_merchants(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[StudioMerchantResponse]:
    return db.scalars(
        select(Vendor).order_by(Vendor.name).limit(500)
    ).all()


@app.get(
    "/api/admin/studio/merchant-price-history",
    response_model=list[StudioMerchantPriceHistoryResponse],
)
def list_studio_merchant_price_history(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[StudioMerchantPriceHistoryResponse]:
    rows = db.execute(
        select(VendorPriceHistory, Vendor.name)
        .join(Vendor, Vendor.id == VendorPriceHistory.vendor_id)
        .order_by(
            VendorPriceHistory.vendor_id.asc(),
            VendorPriceHistory.observed_at.desc(),
            VendorPriceHistory.id.desc(),
        )
        .limit(5000)
    ).all()
    latest_by_vendor: dict[int, VendorPriceHistory] = {}
    price_changes: list[StudioMerchantPriceHistoryResponse] = []
    for history, merchant_name in rows:
        latest = latest_by_vendor.get(history.vendor_id)
        if latest is not None:
            previous_price = history.price_per_container_usd
            new_price = latest.price_per_container_usd
            change_percent = (
                float((new_price - previous_price) / previous_price * Decimal("100"))
                if previous_price
                else 0.0
            )
            price_changes.append(
                StudioMerchantPriceHistoryResponse(
                    vendor_id=history.vendor_id,
                    merchant_name=merchant_name,
                    previous_price_per_container_usd=float(previous_price),
                    price_per_container_usd=float(new_price),
                    change_percent=change_percent,
                    observed_at=latest.observed_at,
                )
            )
        latest_by_vendor[history.vendor_id] = history

    return sorted(price_changes, key=lambda change: change.observed_at, reverse=True)


@app.post(
    "/api/admin/payout",
    response_model=PayoutResponse,
)
def payout_matched_vendor(
    payout_request: AdminPayoutCreate,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PayoutResponse:
    return _payout_matched_vendor(
        payout_request.request_id, db, admin.email, payout_request.containers
    )


@app.post(
    "/api/admin/approve/{request_id}",
    response_model=PayoutResponse,
)
def approve_request(
    request_id: int,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    purchase: PartialPayoutCreate | None = None,
) -> PayoutResponse:
    return _payout_matched_vendor(
        request_id,
        db,
        admin.email,
        purchase.containers if purchase is not None else None,
    )


def _payout_matched_vendor(
    request_id: int, db: Session, actor: str, container_count: int | None = None
) -> PayoutResponse:
    try:
        simulated = _simulated_payouts_enabled()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found",
        )
    if formula_request.status == RequestStatus.PAID.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This request has already been paid"
        )
    if formula_request.status != RequestStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request must be approved before it can be paid",
        )
    if not formula_request.matched_vendor:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No vendor has been matched to this request",
        )
    if formula_request.birth_month is not None:
        current_age = age_in_months(
            formula_request.birth_month,
            month_start(datetime.now(timezone.utc).date()),
        )
        if current_age >= MAX_SUPPORT_AGE_MONTHS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Formula support ends at the start of the 24-month birthday month",
            )

    vendor = db.scalar(
        select(Vendor).where(Vendor.name == formula_request.matched_vendor)
    )
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matched vendor is missing from the vendor catalog",
        )
    if (
        formula_request.total_cost_usd is None
        or formula_request.containers_per_month is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request has no calculated formula cost",
        )

    required_containers = formula_request.containers_per_month
    if container_count is None:
        container_count = required_containers
    if container_count > required_containers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Container quantity cannot exceed this request's monthly need "
                f"of {required_containers}"
            ),
        )
    amount = (
        formula_request.total_cost_usd
        if container_count == required_containers
        else (
            formula_request.total_cost_usd
            * Decimal(container_count)
            / Decimal(required_containers)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    available_balance = get_available_pool_balance(db)
    if available_balance < amount:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Insufficient donation pool funds: USD {available_balance:.2f} "
                f"available; USD {amount:.2f} required"
            ),
        )

    sender_batch_id = f"formula-payout-{secrets.token_hex(16)}"
    previous_lifecycle_status = formula_request.lifecycle_status
    formula_request.lifecycle_status = RequestLifecycleStatus.PAYOUT_PENDING.value
    formula_request.payout_pending_at = datetime.now(timezone.utc)
    transaction = PaymentTransaction(
        request_id=formula_request.id,
        amount=amount,
        containers=container_count,
        currency=vendor.currency,
        recipient_email=vendor.paypal_email,
        sender_batch_id=sender_batch_id,
        status="SIMULATED_PENDING" if simulated else "SUBMITTING",
        payout_mode="SIMULATED" if simulated else "PAYPAL",
    )
    db.add(transaction)
    record_request_event(
        db,
        formula_request,
        action="PAYOUT_SUBMITTED",
        actor=actor,
        previous_status=formula_request.status,
        previous_lifecycle_status=previous_lifecycle_status,
        note=(
            f"SIMULATED: no money was sent. USD {amount:.2f} payout for "
            f"{container_count} of {required_containers} containers staged for "
            f"{vendor.name}."
            if simulated
            else f"PayPal payout submitted for USD {amount:.2f} to {vendor.name} "
            f"for {container_count} of {required_containers} containers."
        ),
        merchant=vendor,
    )
    if not reserve_pool_funds(db, amount):
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Insufficient donation pool funds; another payout may have reserved them",
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payout has already been initiated for this request",
        ) from exc

    if simulated:
        transaction.payout_batch_id = f"SIMULATED-{sender_batch_id}"
        db.commit()
        return PayoutResponse(
            request_id=formula_request.id,
            status=RequestStatus(formula_request.status),
            lifecycle_status=RequestLifecycleStatus(formula_request.lifecycle_status),
            payout_status=transaction.status,
            payout_batch_id=transaction.payout_batch_id,
            amount=transaction.amount,
            currency=transaction.currency,
            payout_mode=transaction.payout_mode,
        )

    try:
        result = create_supplier_payout(
            request_id=formula_request.id,
            payout_attempt_id=transaction.id,
            sender_batch_id=sender_batch_id,
            recipient_email=vendor.paypal_email,
            amount=amount,
            currency=vendor.currency,
        )
    except PayPalError as exc:
        transaction.status = "UNKNOWN" if exc.outcome_unknown else "FAILED"
        transaction.error_message = str(exc)
        if not exc.outcome_unknown:
            formula_request.lifecycle_status = RequestLifecycleStatus.APPROVED.value
        record_request_event(
            db,
            formula_request,
            action="PAYOUT_STATUS_UPDATED",
            actor=actor,
            previous_status=formula_request.status,
            previous_lifecycle_status=RequestLifecycleStatus.PAYOUT_PENDING.value,
            note=f"Payout submission ended with {transaction.status}: {exc}",
            merchant=vendor,
        )
        if not exc.outcome_unknown:
            release_pool_funds(db, amount)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    transaction.payout_batch_id = result["payout_batch_id"]
    payout_status = result["status"].upper()
    transaction.status = payout_status
    transaction.error_message = None
    if payout_status in {"BLOCKED", "CANCELED", "DENIED", "FAILED", "RETURNED"}:
        transaction.status = "FAILED"
        release_pool_funds(db, amount)
        transaction.error_message = "PayPal denied the payout"
        formula_request.lifecycle_status = RequestLifecycleStatus.APPROVED.value
    record_request_event(
        db,
        formula_request,
        action="PAYOUT_STATUS_UPDATED",
        actor=actor,
        previous_status=RequestStatus.APPROVED.value,
        previous_lifecycle_status=RequestLifecycleStatus.PAYOUT_PENDING.value,
        note=f"PayPal payout status: {transaction.status}.",
        merchant=vendor,
    )
    db.commit()
    return PayoutResponse(
        request_id=formula_request.id,
        status=RequestStatus(formula_request.status),
        lifecycle_status=RequestLifecycleStatus(formula_request.lifecycle_status),
        payout_status=transaction.status,
        payout_batch_id=transaction.payout_batch_id,
        amount=transaction.amount,
        currency=transaction.currency,
        payout_mode=transaction.payout_mode,
    )


@app.post(
    "/api/admin/payout/{request_id}/simulate-confirmation",
    response_model=PayoutResponse,
)
def confirm_simulated_payout(
    request_id: int,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PayoutResponse:
    try:
        simulated_enabled = _simulated_payouts_enabled()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if not simulated_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Simulated payouts are disabled",
        )
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    transaction = db.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.request_id == request_id,
            PaymentTransaction.payout_mode == "SIMULATED",
        )
        .order_by(PaymentTransaction.id.desc())
        .limit(1)
    )
    if transaction is None or transaction.status != "SIMULATED_PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending simulated payout exists for this request",
        )
    transaction.status = "SUCCESS"
    try:
        _confirm_payout(
            db,
            formula_request,
            transaction,
            actor=admin.email,
            note="SIMULATED: an admin confirmed the demo payout; no money was sent.",
        )
    except PickupTokenConfigurationError as exc:
        db.rollback()
        logger.error(
            "Could not issue pickup token for simulated payout",
            extra={"request_id": request_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pickup token encryption is not configured",
        ) from exc
    db.commit()
    return PayoutResponse(
        request_id=formula_request.id,
        status=RequestStatus(formula_request.status),
        lifecycle_status=RequestLifecycleStatus(formula_request.lifecycle_status),
        payout_status=transaction.status,
        payout_batch_id=transaction.payout_batch_id,
        amount=transaction.amount,
        currency=transaction.currency,
        payout_mode=transaction.payout_mode,
    )


@app.post(
    "/api/admin/payout/{request_id}/refresh",
    response_model=PayoutResponse,
)
def refresh_payout_status(
    request_id: int,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PayoutResponse:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if formula_request.status == RequestStatus.PAID.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request has already been paid",
        )
    transaction = db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.request_id == request_id)
        .order_by(PaymentTransaction.id.desc())
        .limit(1)
    )
    if transaction is None or not transaction.payout_batch_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No PayPal payout batch is available to check",
        )
    if transaction.status == "FAILED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payout is already in a final state",
        )

    try:
        result = get_supplier_payout_status(transaction.payout_batch_id)
    except PayPalError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    payout_status = result["status"].upper()
    transaction.status = payout_status
    previous_lifecycle_status = formula_request.lifecycle_status
    if payout_status == "SUCCESS":
        transaction.error_message = None
        try:
            _confirm_payout(
                db,
                formula_request,
                transaction,
                actor=admin.email,
                note=(
                    "PayPal status API confirmed payout batch "
                    f"{transaction.payout_batch_id}."
                ),
            )
        except PickupTokenConfigurationError as exc:
            db.rollback()
            logger.error(
                "Could not issue pickup token for confirmed PayPal payout",
                extra={"request_id": request_id},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pickup token encryption is not configured",
            ) from exc
    else:
        if payout_status in {"BLOCKED", "CANCELED", "DENIED", "FAILED", "RETURNED"}:
            transaction.status = "FAILED"
            transaction.error_message = f"PayPal payout ended with {payout_status}"
            release_pool_funds(db, transaction.amount)
            formula_request.lifecycle_status = RequestLifecycleStatus.APPROVED.value
        record_request_event(
            db,
            formula_request,
            action="PAYOUT_STATUS_UPDATED",
            actor=admin.email,
            previous_status=RequestStatus.APPROVED.value,
            previous_lifecycle_status=previous_lifecycle_status,
            note=f"PayPal payout status refreshed to {transaction.status}.",
        )
    db.commit()
    return PayoutResponse(
        request_id=formula_request.id,
        status=RequestStatus(formula_request.status),
        lifecycle_status=RequestLifecycleStatus(formula_request.lifecycle_status),
        payout_status=transaction.status,
        payout_batch_id=transaction.payout_batch_id,
        amount=transaction.amount,
        currency=transaction.currency,
        payout_mode=transaction.payout_mode,
    )


@app.post(
    "/api/requests",
    response_model=RequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_request(
    request: RequestCreate,
    db: Annotated[Session, Depends(get_db)],
) -> FormulaRequest:
    current_month = month_start(datetime.now(timezone.utc).date())
    infant_age_months = (
        age_in_months(request.birth_month, current_month)
        if request.birth_month is not None
        else request.infant_age_months
    )
    formula_request = FormulaRequest(
        recipient_name=request.recipient_name,
        phone_number=request.phone_number,
        location=request.location,
        county=request.county,
        formula_needs=request.formula_needs,
        infant_age_months=infant_age_months,
        birth_month=request.birth_month,
        support_month=current_month if request.birth_month is not None else None,
        status=RequestStatus.PENDING_VERIFICATION.value,
        lifecycle_status=RequestLifecycleStatus.SUBMITTED.value,
        matched_vendor=None,
    )
    db.add(formula_request)
    db.flush()
    if formula_request.birth_month is not None:
        formula_request.continuity_id = formula_request.id
    record_request_event(
        db,
        formula_request,
        action="REQUEST_SUBMITTED",
        actor="Recipient",
        note="Request submitted for admin review.",
    )
    db.commit()
    db.refresh(formula_request)
    return formula_request


@app.post(
    "/api/public/request-status",
    response_model=PublicRequestStatusResponse,
)
def public_request_status(
    lookup: PublicRequestStatusLookup,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> PublicRequestStatusResponse:
    client_ip = request.client.host if request.client else "unknown"
    enforce_status_lookup_rate_limit(client_ip)
    response.headers["Cache-Control"] = "no-store"

    formula_request = db.scalar(
        select(FormulaRequest).where(
            FormulaRequest.public_reference_code == lookup.reference_code
        )
    )
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request status is unavailable for this reference code.",
        )

    lifecycle = formula_request.lifecycle_status
    if lifecycle == RequestLifecycleStatus.COLLECTED.value:
        stage = "collected"
        last_updated_at = formula_request.collected_at
    elif lifecycle == RequestLifecycleStatus.READY_FOR_PICKUP.value:
        stage = "ready_for_pickup"
        last_updated_at = formula_request.ready_for_pickup_at
    elif lifecycle in {
        RequestLifecycleStatus.APPROVED.value,
        RequestLifecycleStatus.PAYOUT_PENDING.value,
        RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
    }:
        stage = "approved"
        last_updated_at = formula_request.approved_at
    elif lifecycle == RequestLifecycleStatus.SUBMITTED.value:
        stage = "submitted"
        last_updated_at = formula_request.submitted_at
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request status is unavailable for this reference code.",
        )

    if last_updated_at is None:
        last_updated_at = formula_request.submitted_at
    return PublicRequestStatusResponse(
        stage=stage,
        last_updated_at=last_updated_at,
    )


@app.post(
    "/api/admin/requests/{request_id}/pickup-token/retrieve",
    response_model=PickupTokenResponse,
)
def retrieve_pickup_token(
    request_id: int,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PickupTokenResponse:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if formula_request.lifecycle_status != RequestLifecycleStatus.READY_FOR_PICKUP.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pickup token is ready for this request",
        )
    if formula_request.pickup_token_retrieved_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token has already been retrieved",
        )
    if formula_request.pickup_token_expires_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token expiry is missing",
        )
    if formula_request.pickup_token_encrypted is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token is no longer available for retrieval",
        )
    if db.scalar(
        select(FormulaRequest.id).where(
            FormulaRequest.id == request_id,
            FormulaRequest.pickup_token_expires_at <= func.current_timestamp(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Pickup token has expired",
        )

    try:
        cipher = _pickup_token_fernet()
    except PickupTokenConfigurationError as exc:
        logger.error(
            "Pickup token encryption key is unavailable",
            extra={"request_id": request_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pickup token encryption is not configured",
        ) from exc
    try:
        token = cipher.decrypt(
            formula_request.pickup_token_encrypted.encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError) as exc:
        logger.error(
            "Stored pickup token could not be decrypted",
            extra={"request_id": request_id},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored pickup token could not be decrypted; check the encryption key",
        ) from exc

    retrieval_time = datetime.now(timezone.utc)
    claimed = db.execute(
        update(FormulaRequest)
        .where(
            FormulaRequest.id == request_id,
            FormulaRequest.pickup_token_retrieved_at.is_(None),
            FormulaRequest.pickup_token_expires_at > func.current_timestamp(),
        )
        .values(
            pickup_token_retrieved_at=retrieval_time,
            pickup_token_encrypted=None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token was already retrieved or has expired",
        )
    formula_request.pickup_token_encrypted = None
    formula_request.pickup_token_retrieved_at = retrieval_time
    record_request_event(
        db,
        formula_request,
        action="PICKUP_TOKEN_RETRIEVED",
        actor=admin.email,
        previous_status=formula_request.status,
        previous_lifecycle_status=formula_request.lifecycle_status,
        note="Admin retrieved the single-use token for delivery.",
    )
    db.commit()
    return PickupTokenResponse(
        token=token,
        expires_at=formula_request.pickup_token_expires_at,
    )


@app.post(
    "/api/admin/requests/{request_id}/decision",
    response_model=RequestResponse,
)
def decide_request(
    request_id: int,
    decision: RequestDecisionCreate,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FormulaRequest:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if formula_request.status != RequestStatus.PENDING_VERIFICATION.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only requests awaiting verification can be approved or rejected",
        )
    if (
        decision.decision == "approve"
        and formula_request.birth_month is not None
        and age_in_months(
            formula_request.birth_month,
            month_start(datetime.now(timezone.utc).date()),
        )
        >= MAX_SUPPORT_AGE_MONTHS
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This child has reached the 24-month support cutoff",
        )
    if decision.decision == "reject" and decision.note is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reason is required when rejecting a request",
        )

    previous_status = formula_request.status
    previous_lifecycle_status = formula_request.lifecycle_status
    formula_request.status = (
        RequestStatus.APPROVED.value
        if decision.decision == "approve"
        else RequestStatus.REJECTED.value
    )
    if decision.decision == "approve":
        formula_request.lifecycle_status = RequestLifecycleStatus.APPROVED.value
        formula_request.approved_at = datetime.now(timezone.utc)
        formula_request.matched_vendor = None
        formula_request.containers_per_month = None
        formula_request.total_cost_usd = None
        formula_request.match_reason = None
    else:
        formula_request.lifecycle_status = RequestLifecycleStatus.REJECTED.value
        formula_request.rejected_at = datetime.now(timezone.utc)
    record_request_event(
        db,
        formula_request,
        action="REQUEST_APPROVED" if decision.decision == "approve" else "REQUEST_REJECTED",
        actor=admin.email,
        previous_status=previous_status,
        previous_lifecycle_status=previous_lifecycle_status,
        note=decision.note,
    )
    db.commit()
    db.refresh(formula_request)
    return formula_request


@app.get(
    "/api/admin/requests/{request_id}/history",
    response_model=list[RequestAuditEventResponse],
)
def request_history(
    request_id: int,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[RequestAuditEvent]:
    if db.get(FormulaRequest, request_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    return list(
        db.scalars(
            select(RequestAuditEvent)
            .where(RequestAuditEvent.request_id == request_id)
            .order_by(RequestAuditEvent.created_at, RequestAuditEvent.id)
        ).all()
    )


@app.post(
    "/api/admin/requests/{request_id}/match",
    response_model=RequestResponse,
)
def rematch_request(
    request_id: int,
    match: MerchantMatchCreate,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> FormulaRequest:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if formula_request.status != RequestStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only approved requests can be matched to a merchant",
        )
    open_payout = db.scalar(
        select(PaymentTransaction.id).where(
            PaymentTransaction.request_id == request_id,
            PaymentTransaction.status != "FAILED",
        )
    )
    if open_payout is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payout has already been initiated; this merchant cannot be changed",
        )
    merchant = db.get(Vendor, match.merchant_id)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Merchant not found"
        )
    if merchant.currency != "USD" or merchant.price_per_container_usd is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The selected merchant must have a USD container price",
        )
    if formula_request.infant_age_months is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This request has no recorded infant age",
        )
    if formula_request.infant_age_months >= MAX_SUPPORT_AGE_MONTHS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Formula support ends when the child reaches 24 months",
        )
    previous_merchant = formula_request.matched_vendor
    quantity, total = calculate_monthly_cost(
        formula_request.infant_age_months, merchant.price_per_container_usd
    )
    formula_request.matched_vendor = merchant.name
    formula_request.containers_per_month = quantity
    formula_request.total_cost_usd = total
    formula_request.match_reason = (
        f"Manually assigned by {admin.email} to {merchant.name}."
        if not previous_merchant
        else f"Reassigned by {admin.email} from {previous_merchant} to {merchant.name}."
    )
    record_request_event(
        db,
        formula_request,
        action="MERCHANT_MATCHED",
        actor=admin.email,
        previous_status=formula_request.status,
        note=match.note or formula_request.match_reason,
        merchant=merchant,
    )
    db.commit()
    db.refresh(formula_request)
    return formula_request


@app.get("/api/admin/requests", response_model=list[RequestResponse])
def list_active_requests(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[RequestResponse]:
    _ensure_current_monthly_support_reviews(db)
    query = (
        select(FormulaRequest)
        .where(
            FormulaRequest.status.in_(
                [
                    RequestStatus.PENDING_VERIFICATION.value,
                    RequestStatus.REJECTED.value,
                    RequestStatus.PAID.value,
                    RequestStatus.PENDING.value,
                    RequestStatus.APPROVED.value,
                ]
            )
        )
        .order_by(FormulaRequest.created_at.desc(), FormulaRequest.id.desc())
        .limit(500)
    )
    requests = list(db.scalars(query).all())
    request_ids = [request.id for request in requests]
    payouts = (
        db.scalars(
            select(PaymentTransaction).where(
                PaymentTransaction.request_id.in_(request_ids)
            ).order_by(PaymentTransaction.id.desc())
        ).all()
        if request_ids
        else []
    )
    payouts_by_request = {}
    for payout in payouts:
        payouts_by_request.setdefault(payout.request_id, payout)
    return [
        RequestResponse.model_validate(request).model_copy(
            update={
                "payout_status": (
                    payouts_by_request[request.id].status
                    if request.id in payouts_by_request
                    else None
                ),
                "payout_batch_id": (
                    payouts_by_request[request.id].payout_batch_id
                    if request.id in payouts_by_request
                    else None
                ),
                "payout_mode": (
                    payouts_by_request[request.id].payout_mode
                    if request.id in payouts_by_request
                    else None
                ),
            }
        )
        for request in requests
    ]


@app.get("/api/admin/dashboard")
def admin_dashboard(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    _ensure_current_monthly_support_reviews(db)
    try:
        payout_mode = (
            "SIMULATED" if _simulated_payouts_enabled() else "PAYPAL"
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    lifecycle_counts = dict(
        db.execute(
            select(FormulaRequest.lifecycle_status, func.count(FormulaRequest.id))
            .group_by(FormulaRequest.lifecycle_status)
        ).all()
    )
    request_status_counts = dict(
        db.execute(
            select(FormulaRequest.status, func.count(FormulaRequest.id)).group_by(
                FormulaRequest.status
            )
        ).all()
    )
    payout_status_counts = [
        {
            "status": payout_status,
            "payout_mode": payout_mode_value,
            "count": count,
        }
        for payout_status, payout_mode_value, count in db.execute(
            select(
                PaymentTransaction.status,
                PaymentTransaction.payout_mode,
                func.count(PaymentTransaction.id),
            ).group_by(
                PaymentTransaction.status,
                PaymentTransaction.payout_mode,
            ).order_by(
                PaymentTransaction.status,
                PaymentTransaction.payout_mode,
            )
        ).all()
    ]
    donations_total = db.scalar(
        select(func.coalesce(func.sum(DonationRecord.amount), 0))
    )
    paypal_payouts_sent = db.scalar(
        select(func.coalesce(func.sum(PaymentTransaction.amount), 0))
        .join(FormulaRequest, PaymentTransaction.request_id == FormulaRequest.id)
        .where(
            PaymentTransaction.payout_mode == "PAYPAL",
            PaymentTransaction.status == "SUCCESS",
            FormulaRequest.lifecycle_status.in_(
                [
                    RequestLifecycleStatus.PAYOUT_CONFIRMED.value,
                    RequestLifecycleStatus.READY_FOR_PICKUP.value,
                    RequestLifecycleStatus.COLLECTED.value,
                ]
            ),
        )
    )
    paypal_payouts_pending = db.scalar(
        select(func.coalesce(func.sum(PaymentTransaction.amount), 0))
        .join(FormulaRequest, PaymentTransaction.request_id == FormulaRequest.id)
        .where(
            PaymentTransaction.payout_mode == "PAYPAL",
            PaymentTransaction.status.not_in(
                ["FAILED", "CANCELED", "DENIED", "RETURNED", "BLOCKED"]
            ),
            FormulaRequest.lifecycle_status
            == RequestLifecycleStatus.PAYOUT_PENDING.value,
        )
    )
    families_supported_count = _confirmed_paypal_family_count(db)
    requests_by_location = [
        {"location": location, "count": count}
        for location, count in db.execute(
            select(FormulaRequest.location, func.count(FormulaRequest.id))
            .group_by(FormulaRequest.location)
            .order_by(func.count(FormulaRequest.id).desc(), FormulaRequest.location)
        ).all()
    ]
    return {
        "payout_mode": payout_mode,
        "available_pool_usd": get_available_pool_balance(db),
        "donations_total_usd": Decimal(donations_total),
        "paypal_payouts_sent_usd": Decimal(paypal_payouts_sent or 0),
        "paypal_payouts_pending_usd": Decimal(paypal_payouts_pending or 0),
        "total_requests_count": sum(lifecycle_counts.values()),
        "submitted_count": lifecycle_counts.get(
            RequestLifecycleStatus.SUBMITTED.value, 0
        ),
        "pending_verification_count": request_status_counts.get(
            RequestStatus.PENDING_VERIFICATION.value, 0
        ),
        "approved_count": lifecycle_counts.get(
            RequestLifecycleStatus.APPROVED.value, 0
        ),
        "payout_pending_count": lifecycle_counts.get(
            RequestLifecycleStatus.PAYOUT_PENDING.value, 0
        ),
        "paid_count": request_status_counts.get(RequestStatus.PAID.value, 0),
        "families_supported_count": families_supported_count,
        "ready_for_pickup_count": lifecycle_counts.get(
            RequestLifecycleStatus.READY_FOR_PICKUP.value, 0
        ),
        "collected_count": lifecycle_counts.get(
            RequestLifecycleStatus.COLLECTED.value, 0
        ),
        "rejected_count": request_status_counts.get(
            RequestStatus.REJECTED.value, 0
        ),
        "payout_status_counts": payout_status_counts,
        "requests_by_location": requests_by_location,
        **_monthly_support_age_overview(db),
    }


def _ensure_current_monthly_support_reviews(
    db: Session, current_month: date | None = None
) -> None:
    current_month = month_start(
        current_month or datetime.now(timezone.utc).date()
    )
    requests = list(
        db.scalars(
            select(FormulaRequest)
            .where(
                FormulaRequest.continuity_id.is_not(None),
                FormulaRequest.birth_month.is_not(None),
            )
            .order_by(
                FormulaRequest.continuity_id,
                FormulaRequest.support_month,
                FormulaRequest.id,
            )
        ).all()
    )
    latest_by_family: dict[int, FormulaRequest] = {}
    for request in requests:
        family_id = request.continuity_id
        if family_id is not None:
            latest_by_family[family_id] = request

    latest_request_ids = [request.id for request in latest_by_family.values()]
    payouts = (
        db.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.request_id.in_(latest_request_ids))
            .order_by(PaymentTransaction.request_id, PaymentTransaction.id)
        ).all()
        if latest_request_ids
        else []
    )
    latest_payout_by_request: dict[int, PaymentTransaction] = {}
    for payout in payouts:
        latest_payout_by_request[payout.request_id] = payout

    for family_id, previous in latest_by_family.items():
        if (
            previous.support_month is None
            or previous.support_month >= current_month
            or previous.birth_month is None
        ):
            continue
        payout = latest_payout_by_request.get(previous.id)
        age = age_in_months(previous.birth_month, current_month)
        if (
            age >= MAX_SUPPORT_AGE_MONTHS
            and previous.lifecycle_status
            in {
                RequestLifecycleStatus.SUBMITTED.value,
                RequestLifecycleStatus.APPROVED.value,
            }
            and (payout is None or payout.status == "FAILED")
        ):
            previous_status = previous.status
            previous_lifecycle_status = previous.lifecycle_status
            previous.status = RequestStatus.REJECTED.value
            previous.lifecycle_status = RequestLifecycleStatus.REJECTED.value
            previous.rejected_at = datetime.now(timezone.utc)
            record_request_event(
                db,
                previous,
                action="SUPPORT_AGE_CUTOFF",
                actor="System",
                previous_status=previous_status,
                previous_lifecycle_status=previous_lifecycle_status,
                note=(
                    "Monthly support review closed because the child reached "
                    "24 months. No payout was submitted for this cycle."
                ),
            )
            db.commit()
            continue
        if (
            age < 0
            or age >= MAX_SUPPORT_AGE_MONTHS
            or previous.status == RequestStatus.REJECTED.value
            or payout is None
            or payout.status != "SUCCESS"
            or payout.payout_mode != "PAYPAL"
            or previous.lifecycle_status
            not in {
                RequestLifecycleStatus.READY_FOR_PICKUP.value,
                RequestLifecycleStatus.COLLECTED.value,
            }
        ):
            continue

        renewal = FormulaRequest(
            recipient_name=previous.recipient_name,
            phone_number=previous.phone_number,
            location=previous.location,
            county=previous.county,
            formula_needs=previous.formula_needs,
            infant_age_months=age,
            birth_month=previous.birth_month,
            continuity_id=family_id,
            support_month=current_month,
            status=RequestStatus.PENDING_VERIFICATION.value,
            lifecycle_status=RequestLifecycleStatus.SUBMITTED.value,
        )
        db.add(renewal)
        try:
            db.flush()
            record_request_event(
                db,
                renewal,
                action="MONTHLY_REVIEW_CREATED",
                actor="System",
                note=(
                    f"Monthly continuity review created for {current_month:%Y-%m} "
                    f"at age {age} months. Staff must verify need and approve; "
                    "no payout has been submitted."
                ),
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(FormulaRequest.id).where(
                    FormulaRequest.continuity_id == family_id,
                    FormulaRequest.support_month == current_month,
                )
            )
            if existing is None:
                raise


def _monthly_support_age_overview(
    db: Session, current_month: date | None = None
) -> dict[str, object]:
    current_month = month_start(
        current_month or datetime.now(timezone.utc).date()
    )
    family_requests = list(
        db.scalars(
            select(FormulaRequest)
            .where(FormulaRequest.continuity_id.is_not(None))
            .order_by(
                FormulaRequest.continuity_id,
                FormulaRequest.support_month,
                FormulaRequest.id,
            )
        ).all()
    )
    latest_by_family: dict[int, FormulaRequest] = {}
    for request in family_requests:
        if request.continuity_id is not None:
            latest_by_family[request.continuity_id] = request

    cohort_labels = (
        "0–2 months",
        "3–5 months",
        "6–11 months",
        "12–17 months",
        "18–23 months",
        "24+ months · ended",
        "Birth month needed",
    )
    cohort_counts = {label: 0 for label in cohort_labels}
    approaching_cutoff: list[dict[str, object]] = []
    supported_families = set(
        db.scalars(
            select(
                func.coalesce(
                    FormulaRequest.continuity_id,
                    FormulaRequest.id,
                )
            )
            .join(
                PaymentTransaction,
                PaymentTransaction.request_id == FormulaRequest.id,
            )
            .where(
                PaymentTransaction.status == "SUCCESS",
                PaymentTransaction.payout_mode == "PAYPAL",
            )
            .distinct()
        ).all()
    )
    for family_id, request in latest_by_family.items():
        if request.birth_month is None:
            if request.status != RequestStatus.REJECTED.value:
                cohort_counts["Birth month needed"] += 1
            continue
        age = age_in_months(request.birth_month, current_month)
        if age < 0:
            if request.status != RequestStatus.REJECTED.value:
                cohort_counts["Birth month needed"] += 1
            continue
        if age >= MAX_SUPPORT_AGE_MONTHS:
            if (
                request.status != RequestStatus.REJECTED.value
                or family_id in supported_families
            ):
                cohort_counts["24+ months · ended"] += 1
            continue
        if request.status == RequestStatus.REJECTED.value:
            continue
        cohort = (
            "0–2 months"
            if age <= 2
            else "3–5 months"
            if age <= 5
            else "6–11 months"
            if age <= 11
            else "12–17 months"
            if age <= 17
            else "18–23 months"
        )
        cohort_counts[cohort] += 1
        if age >= 18:
            approaching_cutoff.append(
                {
                    "request_id": request.id,
                    "recipient_name": request.recipient_name,
                    "location": request.location,
                    "age_months": age,
                    "months_remaining": MAX_SUPPORT_AGE_MONTHS - age,
                    "final_eligible_month": add_months(
                        request.birth_month, MAX_SUPPORT_AGE_MONTHS - 1
                    ),
                }
            )

    legacy_supported_families = db.scalars(
        select(
            func.coalesce(
                FormulaRequest.continuity_id,
                FormulaRequest.id,
            )
        )
        .join(
            PaymentTransaction,
            PaymentTransaction.request_id == FormulaRequest.id,
        )
        .where(
            FormulaRequest.birth_month.is_(None),
            PaymentTransaction.status == "SUCCESS",
            PaymentTransaction.payout_mode == "PAYPAL",
        )
        .distinct()
    ).all()
    cohort_counts["Birth month needed"] += sum(
        1
        for family_id in legacy_supported_families
        if family_id not in latest_by_family
    )

    return {
        "age_cohorts": [
            {"label": label, "count": cohort_counts[label]}
            for label in cohort_labels
        ],
        "approaching_age_limit": sorted(
            approaching_cutoff,
            key=lambda row: (
                row["months_remaining"],
                row["recipient_name"],
            ),
        ),
    }


def _confirmed_paypal_family_count(db: Session) -> int:
    count = db.scalar(
        select(
            func.count(
                func.distinct(
                    func.coalesce(
                        FormulaRequest.continuity_id,
                        FormulaRequest.id,
                    )
                )
            )
        )
        .join(
            PaymentTransaction,
            PaymentTransaction.request_id == FormulaRequest.id,
        )
        .where(
            FormulaRequest.status == RequestStatus.PAID.value,
            PaymentTransaction.status == "SUCCESS",
            PaymentTransaction.payout_mode == "PAYPAL",
        )
    )
    return int(count or 0)


@app.get("/api/public/impact")
def public_impact(
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, int | Decimal]:
    donations_total = db.scalar(
        select(func.coalesce(func.sum(DonationRecord.amount), 0))
    )
    infants_sustained = db.scalar(
        select(
            func.count(
                func.distinct(
                    func.coalesce(
                        FormulaRequest.continuity_id,
                        FormulaRequest.id,
                    )
                )
            )
        )
        .join(
            PaymentTransaction,
            PaymentTransaction.request_id == FormulaRequest.id,
        )
        .where(
            FormulaRequest.status == RequestStatus.PAID.value,
            FormulaRequest.infant_age_months <= 24,
            PaymentTransaction.status == "SUCCESS",
            PaymentTransaction.payout_mode == "PAYPAL",
        )
    )
    verified_merchants = db.scalar(
        select(func.count(Vendor.id)).where(
            Vendor.currency == "USD",
            Vendor.location != "",
            Vendor.price_per_container_usd.is_not(None),
        )
    )
    return {
        "total_donations_usd": Decimal(donations_total),
        "families_supported_count": _confirmed_paypal_family_count(db),
        "infants_sustained": int(infants_sustained or 0),
        "verified_merchants": int(verified_merchants or 0),
    }


@app.get("/api/admin/anomalies")
def admin_anomalies(
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, list[dict[str, object]]]:
    payout_issues = db.execute(
        select(
            PaymentTransaction.id,
            PaymentTransaction.request_id,
            PaymentTransaction.status,
            PaymentTransaction.amount,
            PaymentTransaction.error_message,
        )
        .where(PaymentTransaction.status.in_(["FAILED", "UNKNOWN", "SUBMITTING"]))
        .order_by(PaymentTransaction.created_at.desc())
        .limit(50)
    ).all()
    price_alerts: list[dict[str, object]] = []
    vendors = db.scalars(select(Vendor).order_by(Vendor.name)).all()
    for vendor in vendors:
        observations = db.scalars(
            select(VendorPriceHistory)
            .where(VendorPriceHistory.vendor_id == vendor.id)
            .order_by(VendorPriceHistory.observed_at.desc(), VendorPriceHistory.id.desc())
            .limit(2)
        ).all()
        if len(observations) < 2:
            continue
        previous, current = observations[1], observations[0]
        if current.price_per_container_usd >= previous.price_per_container_usd * Decimal(
            "1.20"
        ):
            price_alerts.append(
                {
                    "vendor": vendor.name,
                    "location": vendor.location,
                    "previous_price_usd": previous.price_per_container_usd,
                    "current_price_usd": current.price_per_container_usd,
                    "increase_percent": (
                        (current.price_per_container_usd / previous.price_per_container_usd)
                        - Decimal("1")
                    )
                    * Decimal("100"),
                }
            )
    return {
        "payout_issues": [
            {
                "transaction_id": row.id,
                "request_id": row.request_id,
                "status": row.status,
                "amount_usd": row.amount,
                "message": row.error_message,
            }
            for row in payout_issues
        ],
        "price_alerts": price_alerts,
    }


@app.post("/api/admin/agent/query")
def query_admin_data(
    question: AdminQueryCreate,
    _: Annotated[None, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    requests = db.scalars(select(FormulaRequest).order_by(FormulaRequest.id)).all()
    safe_request_data = [
        {
            "id": request.id,
            "location": request.location,
            "status": request.status,
            "total_cost_usd": request.total_cost_usd,
        }
        for request in requests
    ]
    return answer_admin_query(
        question.question,
        safe_request_data,
        get_available_pool_balance(db),
    )


@app.post("/api/llm")
def proxy_studio_llm_request(
    llm_request: LlmProxyRequest,
    session: Annotated[
        tuple[AdminAccount, AdminSession], Depends(get_admin_session)
    ],
) -> StreamingResponse:
    account, _ = session
    enforce_llm_rate_limit(account.id)

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The AI provider is not configured on the server.",
        )

    payload = llm_request.model_dump(exclude_none=True)
    payload.update({"model": model, "stream": True, "store": False})
    try:
        upstream = requests.post(
            "https://api.openai.com/v1/responses",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=(10, 120),
            stream=True,
        )
    except requests.RequestException as exc:
        logger.warning("OpenAI request failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the AI provider.",
        ) from exc

    if not upstream.ok:
        logger.warning("OpenAI returned HTTP %s", upstream.status_code)
        upstream.close()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI provider rejected the request.",
        )

    def stream_response() -> Generator[bytes, None, None]:
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/pickup/redeem")
def redeem_pickup_token(
    pickup: PickupRedeemCreate,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    token_hash = sha256(pickup.token.encode("utf-8")).hexdigest()
    formula_request = db.scalar(
        select(FormulaRequest).where(
            FormulaRequest.pickup_token_hash == token_hash,
        )
    )
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pickup token is invalid",
        )
    if formula_request.pickup_token_used_at is not None or (
        formula_request.lifecycle_status == RequestLifecycleStatus.COLLECTED.value
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token has already been redeemed",
        )
    if (
        formula_request.status != RequestStatus.PAID.value
        or formula_request.lifecycle_status
        != RequestLifecycleStatus.READY_FOR_PICKUP.value
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pickup token is invalid",
        )
    if formula_request.pickup_token_expires_at is None or db.scalar(
        select(FormulaRequest.id).where(
            FormulaRequest.id == formula_request.id,
            FormulaRequest.pickup_token_expires_at <= func.current_timestamp(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Pickup token has expired",
        )
    previous_lifecycle_status = formula_request.lifecycle_status
    redeemed = db.execute(
        update(FormulaRequest)
        .where(
            FormulaRequest.id == formula_request.id,
            FormulaRequest.pickup_token_used_at.is_(None),
            FormulaRequest.pickup_token_expires_at > func.current_timestamp(),
            FormulaRequest.lifecycle_status
            == RequestLifecycleStatus.READY_FOR_PICKUP.value,
        )
        .values(
            pickup_token_used_at=func.current_timestamp(),
            lifecycle_status=RequestLifecycleStatus.COLLECTED.value,
            collected_at=func.current_timestamp(),
        )
        .execution_options(synchronize_session=False)
    )
    if redeemed.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pickup token has already been redeemed or has expired",
        )
    formula_request.lifecycle_status = RequestLifecycleStatus.COLLECTED.value
    formula_request.pickup_token_used_at = datetime.now(timezone.utc)
    formula_request.collected_at = datetime.now(timezone.utc)
    record_request_event(
        db,
        formula_request,
        action="PICKUP_REDEEMED",
        actor="Merchant",
        previous_status=formula_request.status,
        previous_lifecycle_status=previous_lifecycle_status,
        note="One-time pickup token was redeemed.",
    )
    db.commit()
    return {
        "status": "redeemed",
        "recipient_name": formula_request.recipient_name,
        "location": formula_request.location,
        "merchant": formula_request.matched_vendor,
        "containers_per_month": formula_request.containers_per_month,
    }


@app.post(
    "/api/admin/requests/{request_id}/confirm-and-pay",
    response_model=PayoutResponse,
)
def confirm_and_pay(
    request_id: int,
    payout: PayoutCreate,
    admin: Annotated[AdminAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> PayoutResponse:
    formula_request = db.get(FormulaRequest, request_id)
    if formula_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    vendor = (
        db.scalar(select(Vendor).where(Vendor.name == formula_request.matched_vendor))
        if formula_request.matched_vendor
        else None
    )
    if (
        vendor is None
        or formula_request.total_cost_usd is None
        or payout.amount != formula_request.total_cost_usd
        or payout.recipient_email != vendor.paypal_email
        or payout.currency != vendor.currency
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payout amount and destination must match the verified request and merchant",
        )
    return _payout_matched_vendor(request_id, db, admin.email)