"""Exact unit conversions and checks shared by domain contracts."""

from datetime import UTC, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext

from job_market.domain.enums import ExperienceUnit

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
MONEY_SCALE = 10_000


def _as_decimal(value: Decimal | str | int) -> Decimal:
    if type(value) not in (Decimal, str, int):
        raise ValueError("a Decimal, decimal string, or integer is required")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal value") from None
    if not number.is_finite() or number < 0:
        raise ValueError("a finite nonnegative value is required")
    return number


def _scaled_integer(value: Decimal | str | int, factor: int, *, min_adjusted: int) -> int:
    number = _as_decimal(value)
    if number.is_zero():
        return 0
    # Bound extreme exponents before multiplying. Size the private Decimal
    # context for the input coefficient so arithmetic cannot round away digits.
    if number.adjusted() > 18:
        raise ValueError("value exceeds signed 64-bit storage")
    if number.adjusted() < min_adjusted:
        raise ValueError("value cannot be represented exactly in the storage unit")
    with localcontext(Context(prec=len(number.as_tuple().digits) + len(str(factor)))):
        scaled = number * factor
        if scaled != scaled.to_integral_value():
            raise ValueError("value cannot be represented exactly in the storage unit")
        if scaled > INT64_MAX:
            raise ValueError("value exceeds signed 64-bit storage")
        return int(scaled)


def scale_money(value: Decimal | str | int) -> int:
    """Convert a decimal amount to units of 0.0001; reject rounding and overflow."""
    return _scaled_integer(value, MONEY_SCALE, min_adjusted=-4)


def unscale_money(value: int) -> Decimal:
    """Return the exact decimal amount from a stored integer."""
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        raise ValueError("a nonnegative signed 64-bit integer is required")
    whole, fraction = divmod(value, MONEY_SCALE)
    return Decimal(f"{whole}.{fraction:04d}")


def duration_to_months(value: Decimal | str | int, unit: ExperienceUnit) -> int:
    """Convert months or years without rounding fractional months."""
    match unit:
        case ExperienceUnit.MONTH:
            return _scaled_integer(value, 1, min_adjusted=0)
        case ExperienceUnit.YEAR:
            return _scaled_integer(value, 12, min_adjusted=-2)
        case _:
            raise ValueError("experience unit must be month or year")


def as_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("a timezone-aware timestamp is required")
    return value.astimezone(UTC)


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


def validate_quote(source: str, quote: str, start: int | None, end: int | None) -> None:
    """Validate a quote in original text, with optional character offsets."""
    if start is None and end is None:
        if quote not in source:
            raise ValueError("evidence quote does not occur in original text")
    elif start is None or end is None or not 0 <= start < end <= len(source):
        raise ValueError("evidence offsets are outside original text")
    elif source[start:end] != quote:
        raise ValueError("evidence does not match the original text at its offsets")
