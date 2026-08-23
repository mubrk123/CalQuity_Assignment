"""Business-hours arithmetic, driven by the calendar assumption in app.config."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.config import (
    BUSINESS_DAY_END,
    BUSINESS_DAY_START,
    BUSINESS_HOLIDAYS,
    BUSINESS_HOURS_PER_DAY,
    BUSINESS_WEEKDAYS,
    TIMEZONE,
)

Unit = Literal["minutes", "hours", "business_hours", "business_days"]
Coverage = Literal["24x7", "business"]

_STEP = timedelta(minutes=1)


@dataclass(frozen=True)
class ResponseTarget:
    """A first-response target as written in a policy or contract."""

    amount: float
    unit: Unit
    coverage: Coverage
    source_ref: str
    quote: str
    interpretation: str | None = None  # key into config.ASSUMPTION_NOTES

    @property
    def uses_business_calendar(self) -> bool:
        return self.unit in ("business_hours", "business_days")

    @property
    def business_hours(self) -> float:
        if self.unit == "business_hours":
            return float(self.amount)
        if self.unit == "business_days":
            return float(self.amount) * BUSINESS_HOURS_PER_DAY
        raise ValueError(f"{self.unit} is not a business-calendar unit")

    @property
    def clock_delta(self) -> timedelta:
        if self.unit == "minutes":
            return timedelta(minutes=self.amount)
        if self.unit == "hours":
            return timedelta(hours=self.amount)
        raise ValueError(f"{self.unit} is not a clock unit")

    def describe(self) -> str:
        n = int(self.amount) if float(self.amount).is_integer() else self.amount
        word = {
            "minutes": "minute",
            "hours": "hour",
            "business_hours": "business hour",
            "business_days": "business day",
        }[self.unit]
        plural = "" if n == 1 else "s"
        suffix = ", 24x7" if self.coverage == "24x7" and self.unit in ("minutes", "hours") else ""
        return f"{n} {word}{plural}{suffix}"


def is_business_day(dt: datetime) -> bool:
    if dt.strftime("%Y-%m-%d") in BUSINESS_HOLIDAYS:
        return False
    return dt.weekday() in BUSINESS_WEEKDAYS


def _window(dt: datetime) -> tuple[datetime, datetime]:
    return (
        dt.replace(hour=BUSINESS_DAY_START.hour, minute=BUSINESS_DAY_START.minute,
                   second=0, microsecond=0),
        dt.replace(hour=BUSINESS_DAY_END.hour, minute=BUSINESS_DAY_END.minute,
                   second=0, microsecond=0),
    )


def is_business_time(dt: datetime) -> bool:
    if not is_business_day(dt):
        return False
    start, end = _window(dt)
    return start <= dt < end


def next_business_instant(dt: datetime) -> datetime:
    """The first business instant at or after dt."""
    guard = 0
    while guard < 3650:
        if is_business_day(dt):
            start, end = _window(dt)
            if dt < start:
                return start
            if dt < end:
                return dt
        dt = (dt + timedelta(days=1)).replace(
            hour=BUSINESS_DAY_START.hour, minute=BUSINESS_DAY_START.minute,
            second=0, microsecond=0,
        )
        guard += 1
    raise RuntimeError("no business day found within 10 years")


def business_hours_between(start: datetime, end: datetime) -> float:
    """Elapsed business hours between two instants (0 if end <= start)."""
    if end <= start:
        return 0.0
    total = timedelta()
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if is_business_day(day):
            w_start, w_end = _window(day)
            lo, hi = max(start, w_start), min(end, w_end)
            if hi > lo:
                total += hi - lo
        day += timedelta(days=1)
    return total.total_seconds() / 3600.0


def add_business_hours(start: datetime, hours: float) -> datetime:
    """The instant reached after consuming `hours` of business time from start."""
    remaining = timedelta(hours=hours)
    cursor = next_business_instant(start)
    guard = 0
    while remaining > timedelta() and guard < 3650:
        _, w_end = _window(cursor)
        available = w_end - cursor
        if available >= remaining:
            return cursor + remaining
        remaining -= available
        cursor = next_business_instant(
            (cursor + timedelta(days=1)).replace(
                hour=BUSINESS_DAY_START.hour, minute=BUSINESS_DAY_START.minute,
                second=0, microsecond=0,
            )
        )
        guard += 1
    return cursor


@dataclass
class TargetEvaluation:
    target: ResponseTarget
    started_at: datetime
    now: datetime
    deadline: datetime
    elapsed_display: str
    remaining_display: str
    breached: bool
    clock_started: bool
    depends_on_business_calendar: bool

    def to_dict(self) -> dict:
        return {
            "target": self.target.describe(),
            "target_source": self.target.source_ref,
            "target_quote": self.target.quote,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M"),
            "reference_now": self.now.strftime("%Y-%m-%d %H:%M"),
            "deadline": self.deadline.strftime("%Y-%m-%d %H:%M"),
            "elapsed": self.elapsed_display,
            "remaining": self.remaining_display,
            "breached": self.breached,
            "clock_started": self.clock_started,
            "depends_on_business_calendar": self.depends_on_business_calendar,
        }


def _fmt_hours(h: float) -> str:
    if h < 1:
        return f"{h * 60:.0f} minutes"
    if h < 24:
        return f"{h:.2f}".rstrip("0").rstrip(".") + " hours"
    return f"{h / BUSINESS_HOURS_PER_DAY:.2f}".rstrip("0").rstrip(".") + " business days"


def evaluate_target(target: ResponseTarget, started_at: datetime, now: datetime) -> TargetEvaluation:
    """Compute deadline and breach status for a target."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)

    if target.uses_business_calendar:
        deadline = add_business_hours(started_at, target.business_hours)
        elapsed_bh = business_hours_between(started_at, now)
        remaining_bh = max(0.0, target.business_hours - elapsed_bh)
        clock_started = elapsed_bh > 0
        return TargetEvaluation(
            target=target,
            started_at=started_at,
            now=now,
            deadline=deadline,
            elapsed_display=f"{_fmt_hours(elapsed_bh)} of business time",
            remaining_display=(
                f"{_fmt_hours(remaining_bh)} of business time remaining"
                if remaining_bh > 0 else "target exceeded"
            ),
            breached=now > deadline,
            clock_started=clock_started,
            depends_on_business_calendar=True,
        )

    deadline = started_at + target.clock_delta
    elapsed = (now - started_at).total_seconds() / 3600.0
    remaining = (deadline - now).total_seconds() / 3600.0
    return TargetEvaluation(
        target=target,
        started_at=started_at,
        now=now,
        deadline=deadline,
        elapsed_display=f"{_fmt_hours(max(0.0, elapsed))} elapsed",
        remaining_display=(
            f"{_fmt_hours(remaining)} remaining" if remaining > 0 else "target exceeded"
        ),
        breached=now > deadline,
        clock_started=True,
        depends_on_business_calendar=False,
    )
