import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.database import PoolBalance, Vendor


CENT = Decimal("0.01")
_LOCATION_WORDS = re.compile(r"[a-z0-9]+")
MAX_SUPPORT_AGE_MONTHS = 24


def month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def age_in_months(birth_month: date, as_of_month: date) -> int:
    birth_month = month_start(birth_month)
    as_of_month = month_start(as_of_month)
    return (as_of_month.year - birth_month.year) * 12 + (
        as_of_month.month - birth_month.month
    )


def add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def containers_for_age(age_months: int) -> int:
    if age_months < 0:
        raise ValueError("Infant age cannot be negative")
    return 3 if age_months <= 2 else 4


def calculate_monthly_cost(
    age_months: int, price_per_container_usd: Decimal
) -> tuple[int, Decimal]:
    if not price_per_container_usd.is_finite() or price_per_container_usd <= 0:
        raise ValueError("Price per container must be greater than zero")
    containers = containers_for_age(age_months)
    total = (price_per_container_usd * containers).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    return containers, total


def _location_tokens(location: str) -> set[str]:
    return set(_LOCATION_WORDS.findall(location.casefold()))


def match_local_vendor(db: Session, location: str) -> tuple[Vendor | None, str]:
    requested_tokens = _location_tokens(location)
    if not requested_tokens:
        return None, "A recognizable neighborhood is required for local matching"

    vendors = list(
        db.scalars(
            select(Vendor)
            .where(
                Vendor.currency == "USD",
                Vendor.location != "",
            )
            .order_by(Vendor.id)
        ).all()
    )
    matches: list[tuple[int, Vendor]] = []
    for vendor in vendors:
        vendor_tokens = _location_tokens(vendor.location)
        overlap = requested_tokens & vendor_tokens
        if overlap and vendor.price_per_container_usd is not None:
            matches.append((len(overlap), vendor))

    if not matches:
        return None, "No USD merchant with a per-container price matches this neighborhood"

    score, selected = max(matches, key=lambda match: (match[0], -match[1].id))
    matched_tokens = sorted(requested_tokens & _location_tokens(selected.location))
    return selected, (
        f"Matched {selected.name} on neighborhood/city terms "
        f"({', '.join(matched_tokens)}; location score {score})"
    )


def get_available_pool_balance(db: Session) -> Decimal:
    pool = db.get(PoolBalance, 1)
    return pool.available_usd if pool is not None else Decimal("0.00")


def reserve_pool_funds(db: Session, amount: Decimal) -> bool:
    result = db.execute(
        update(PoolBalance)
        .where(
            PoolBalance.id == 1,
            PoolBalance.available_usd >= amount,
        )
        .values(available_usd=PoolBalance.available_usd - amount)
    )
    return result.rowcount == 1


def release_pool_funds(db: Session, amount: Decimal) -> None:
    db.execute(
        update(PoolBalance)
        .where(PoolBalance.id == 1)
        .values(available_usd=PoolBalance.available_usd + amount)
    )


def credit_pool_funds(db: Session, amount: Decimal) -> None:
    result = db.execute(
        update(PoolBalance)
        .where(PoolBalance.id == 1)
        .values(available_usd=PoolBalance.available_usd + amount)
    )
    if result.rowcount != 1:
        raise RuntimeError("The platform pool balance has not been initialized")