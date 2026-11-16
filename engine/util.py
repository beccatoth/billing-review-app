"""Shared helpers: exact money math, date parsing, month arithmetic.

All money is Decimal and rounds half up, per START_HERE ("Use exact decimal
arithmetic, round half up").
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    DENVER = ZoneInfo("America/Denver")
except Exception:  # pragma: no cover
    DENVER = None

ZERO = Decimal("0")
CENT = Decimal("0.01")


# ---------------------------------------------------------------- money
def D(value) -> Decimal:
    """Parse a value to Decimal. Blank means zero only where the caller says so."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "" or s.lower() in ("nan", "none"):
        return ZERO
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Not a number: {value!r}")


def is_blank(value) -> bool:
    return value is None or str(value).strip() == "" or str(value).strip().lower() in ("nan", "none", "nat")


def money(x: Decimal) -> Decimal:
    """Round once to cents, half up."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def whole(x: Decimal) -> int:
    """Round once to a whole unit, half up."""
    return int(D(x).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_usd(cents) -> Decimal:
    return D(cents) / Decimal(100)


def fmt_usd(x, signed: bool = False) -> str:
    x = D(x)
    s = f"${abs(x):,.2f}"
    if x < 0:
        return f"({s})" if not signed else f"-{s}"
    return f"+{s}" if signed and x > 0 else s


def fmt_int(x) -> str:
    return f"{int(x):,}"


# ---------------------------------------------------------------- dates
_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")


def parse_date(value) -> Optional[date]:
    """Accept ISO (2026-10-01), ISO timestamps, or M/D/YY spreadsheets re-saves."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return to_denver(value).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = _MDY.match(s.split(" ")[0])
    if m:
        mo, dd, yy = (int(g) for g in m.groups())
        if yy < 100:
            yy += 2000
        return date(yy, mo, dd)
    if "T" in s or len(s) > 10:
        return to_denver(parse_ts(s)).date()
    return date.fromisoformat(s[:10])


def parse_ts(value) -> Optional[datetime]:
    """ISO timestamp with offset. Naive values are treated as America/Denver."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        m = _MDY.match(s.split(" ")[0])
        if m:
            d = parse_date(s.split(" ")[0])
            rest = s.split(" ")[1] if " " in s else "00:00"
            hh, mm = (int(p) for p in rest.split(":")[:2])
            dt = datetime(d.year, d.month, d.day, hh, mm)
        else:
            dt = datetime.fromisoformat(s)
    if dt.tzinfo is None and DENVER is not None:
        dt = dt.replace(tzinfo=DENVER)
    return dt


def to_denver(dt: datetime) -> datetime:
    return dt.astimezone(DENVER) if (DENVER is not None and dt.tzinfo) else dt


def ts_to_service_date(value) -> Optional[date]:
    """Stripe period boundaries are midnight Denver; convert to the service date."""
    dt = parse_ts(value)
    return to_denver(dt).date() if dt else None


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_period(value) -> Optional[tuple]:
    """'Oct 2026', 'Oct-26', '2026-10' -> (2026, 10)."""
    if is_blank(value):
        return None
    s = str(value).strip()
    m = re.match(r"^([A-Za-z]{3})[a-z]*[\s\-]+(\d{2,4})$", s)
    if m:
        y = int(m.group(2))
        return (y + 2000 if y < 100 else y, MONTHS[m.group(1).lower()])
    m = re.match(r"^(\d{4})-(\d{1,2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    raise ValueError(f"Unrecognised posting period: {value!r}")


def month_bounds(year: int, month: int) -> tuple:
    start = date(year, month, 1)
    return start, add_months(start, 1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def days_between(start: date, end_excl: date) -> int:
    return max(0, (end_excl - start).days)


def overlap_days(a0: date, a1: date, b0: date, b1: date) -> int:
    return days_between(max(a0, b0), min(a1, b1))


def period_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def long_date(d: date) -> str:
    return f"{calendar.month_name[d.month]} {d.day}, {d.year}" if d else ""


def daterange(start: date, end_excl: date):
    d = start
    while d < end_excl:
        yield d
        d += timedelta(days=1)
