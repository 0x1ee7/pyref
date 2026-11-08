"""Dates and times: aware datetimes, zones, DST, ISO 8601, calendars, and clocks.

Rules this module follows throughout:

* Every instant is normalised to UTC before it is compared, subtracted, or
  merged. Two aware datetimes sharing one ``ZoneInfo`` object are compared and
  subtracted on their *wall-clock* fields, which is wrong across DST.
* "Now" is never read inside the library; callers inject it.
* Naive datetimes are rejected wherever an absolute instant is required.
"""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from email.utils import parsedate_to_datetime
from enum import Enum
from zoneinfo import ZoneInfo

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Epoch magnitudes at or above this are milliseconds, below are seconds.
# 1e11 seconds is year 5138; 1e11 milliseconds is 1973-03-03.
_MS_THRESHOLD = 100_000_000_000
_NUMERIC = re.compile(r"-?\d+(?:\.\d+)?")
_RFC2822_START = re.compile(r"(?i)^(?:mon|tue|wed|thu|fri|sat|sun),")


# --------------------------------------------------------------------------- #
# Part 1: aware vs naive, UTC storage, display in a zone
# --------------------------------------------------------------------------- #


def to_utc(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime. Naive input is an error."""
    if dt.utcoffset() is None:
        raise ValueError(f"naive datetime {dt.isoformat()} has no zone; attach one first")
    return dt.astimezone(UTC)


def localize(wall: datetime, zone: str, fold: int = 0) -> datetime:
    """Attach ``zone`` to a naive wall-clock time.

    ``fold`` selects which of two occurrences an ambiguous wall time means.
    Nonexistent wall times are returned as-is; policy is the caller's concern.
    """
    if wall.tzinfo is not None:
        raise ValueError("localize expects a naive wall-clock datetime")
    if fold not in (0, 1):
        raise ValueError(f"fold must be 0 or 1, not {fold!r}")
    return wall.replace(tzinfo=ZoneInfo(zone), fold=fold)


def in_zone(dt: datetime, zone: str) -> datetime:
    """Express an aware instant as wall-clock time in ``zone``."""
    return to_utc(dt).astimezone(ZoneInfo(zone))


def format_in_zone(dt: datetime, zone: str) -> str:
    """Render as ``YYYY-MM-DD HH:MM ABBR`` in ``zone``, e.g. ``2026-03-29 14:00 CEST``."""
    return in_zone(dt, zone).strftime("%Y-%m-%d %H:%M %Z")


# --------------------------------------------------------------------------- #
# Part 2: heterogeneous timestamp parsing into aware UTC
# --------------------------------------------------------------------------- #


def to_epoch_ms(dt: datetime) -> int:
    """Whole milliseconds since the Unix epoch, truncated toward the past."""
    return (to_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def _from_epoch(value: float) -> datetime:
    if abs(value) >= _MS_THRESHOLD:
        return EPOCH + timedelta(milliseconds=value)
    return EPOCH + timedelta(seconds=value)


def parse_timestamp(raw: str | int | float) -> datetime:
    """Parse a log timestamp of any common shape into an aware UTC datetime.

    Accepted forms:

    * ``int``/``float`` or a numeric string: epoch seconds, or epoch
      milliseconds when the magnitude is at least 1e11.
    * RFC 2822 (``Sun, 29 Mar 2026 12:00:00 +0200``), detected by a weekday
      prefix and parsed with :mod:`email.utils`.
    * ISO 8601 with a mandatory offset (``Z`` included).

    Raises ``TypeError`` for unsupported types (``bool`` included) and
    ``ValueError`` for empty, malformed, or offset-less input.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise TypeError(f"unsupported timestamp type: {type(raw).__name__}")
    if isinstance(raw, (int, float)):
        return _from_epoch(raw)

    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    if _NUMERIC.fullmatch(text):
        return _from_epoch(float(text))

    if _RFC2822_START.match(text):
        parsed = parsedate_to_datetime(text)
    else:
        parsed = datetime.fromisoformat(text)
    if parsed.utcoffset() is None:
        raise ValueError(f"timestamp {raw!r} carries no UTC offset")
    return to_utc(parsed)


# --------------------------------------------------------------------------- #
# Part 3: wall-clock vs elapsed-time arithmetic
# --------------------------------------------------------------------------- #


def add_calendar_days(dt: datetime, days: int) -> datetime:
    """Move by whole calendar days, keeping the wall-clock time.

    The result is not normalised: if the wall time falls in a DST gap, the
    returned value carries the pre-transition offset (``fold=0``).
    """
    return (dt.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=dt.tzinfo, fold=0)


def add_elapsed(dt: datetime, delta: timedelta) -> datetime:
    """Move by an absolute duration; the result is expressed in ``dt``'s zone."""
    return (to_utc(dt) + delta).astimezone(dt.tzinfo)


def elapsed_between(start: datetime, end: datetime) -> timedelta:
    """Signed absolute duration from ``start`` to ``end``."""
    return to_utc(end) - to_utc(start)


# --------------------------------------------------------------------------- #
# Part 4: calendar arithmetic and business days
# --------------------------------------------------------------------------- #


def days_in_month(year: int, month: int) -> int:
    """Number of days in the given month, leap years included."""
    return calendar.monthrange(year, month)[1]


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's length."""
    index = d.year * 12 + (d.month - 1) + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(d.day, days_in_month(year, month)))


def billing_dates(anchor: date, count: int) -> list[date]:
    """``count`` billing dates, each computed from ``anchor`` (no drift).

    Stepping one month at a time from the previous result would turn Jan 31
    into Feb 28 and then Mar 28. Computing from the anchor keeps Mar 31.
    """
    if count < 0:
        raise ValueError(f"count must be non-negative, not {count}")
    return [add_months(anchor, k) for k in range(count)]


class BusinessCalendar:
    """Weekday and holiday aware day arithmetic over :class:`datetime.date`."""

    def __init__(
        self,
        holidays: Iterable[date] = (),
        weekend: frozenset[int] = frozenset({5, 6}),
    ) -> None:
        self._holidays = frozenset(holidays)
        if any(isinstance(h, datetime) for h in self._holidays):
            raise TypeError("holidays must be date objects, not datetime")
        self._weekend = weekend

    def is_business_day(self, d: date) -> bool:
        return d.weekday() not in self._weekend and d not in self._holidays

    def add_business_days(self, d: date, n: int) -> date:
        """Step ``n`` business days forward (or back if negative).

        Each step lands on a business day, so from a non-business start the
        first step goes to the next business day. ``n == 0`` returns ``d``.
        """
        step = timedelta(days=1 if n >= 0 else -1)
        remaining = abs(n)
        current = d
        while remaining:
            current += step
            if self.is_business_day(current):
                remaining -= 1
        return current

    def business_days_between(self, start: date, end: date) -> int:
        """Count business days in the half-open range ``(start, end]``.

        Reversed arguments return the negated count, so
        ``add_business_days(start, between(start, end)) == end`` whenever
        ``end`` is a business day after ``start``.
        """
        if end < start:
            return -self.business_days_between(end, start)
        count = 0
        current = start
        while current < end:
            current += timedelta(days=1)
            if self.is_business_day(current):
                count += 1
        return count


# --------------------------------------------------------------------------- #
# Part 5: recurring meetings with an explicit DST policy
# --------------------------------------------------------------------------- #


class Gap(Enum):
    """What to do when a wall time does not exist (spring-forward gap)."""

    SKIP = "skip"
    SHIFT_FORWARD = "shift_forward"


class Overlap(Enum):
    """Which occurrence to use when a wall time happens twice (fall-back)."""

    EARLIER = "earlier"
    LATER = "later"
    SKIP = "skip"


def _resolve(wall: datetime, zone: ZoneInfo, gap: Gap, overlap: Overlap) -> datetime | None:
    earlier = wall.replace(tzinfo=zone, fold=0)
    later = wall.replace(tzinfo=zone, fold=1)
    if earlier.utcoffset() == later.utcoffset():
        return earlier.astimezone(UTC).astimezone(zone)

    # The two folds disagree on offset, so the wall time is a gap or an overlap.
    # Round-tripping the earlier fold through UTC reproduces the wall time only
    # for an overlap.
    roundtrip = earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip != wall:
        if gap is Gap.SKIP:
            return None
        # Interpreting the wall time with the pre-transition offset lands
        # after the gap, which is the "shift forward" result.
        return earlier.astimezone(UTC).astimezone(zone)

    match overlap:
        case Overlap.EARLIER:
            chosen = earlier
        case Overlap.LATER:
            chosen = later
        case Overlap.SKIP:
            return None
    return chosen.astimezone(UTC).astimezone(zone)


def weekly_occurrences(
    *,
    start: date,
    weekday: int,
    at: dtime,
    zone: str,
    count: int,
    gap: Gap = Gap.SHIFT_FORWARD,
    overlap: Overlap = Overlap.EARLIER,
) -> list[datetime]:
    """Next ``count`` occurrences of a weekly local-time event.

    Occurrences fall on ``weekday`` (Monday is 0) on or after ``start`` and
    keep the wall-clock time ``at`` in ``zone``. Occurrences dropped by the
    policy do not count toward ``count``.
    """
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be 0 (Monday) to 6 (Sunday), not {weekday}")
    if at.tzinfo is not None:
        raise ValueError("at must be a naive wall-clock time")
    if count < 0:
        raise ValueError(f"count must be non-negative, not {count}")

    tz = ZoneInfo(zone)
    day = start + timedelta(days=(weekday - start.weekday()) % 7)
    results: list[datetime] = []
    while len(results) < count:
        resolved = _resolve(datetime.combine(day, at), tz, gap, overlap)
        if resolved is not None:
            results.append(resolved)
        day += timedelta(days=7)
    return results


# --------------------------------------------------------------------------- #
# Part 6: booking slots across time zones
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """Half-open interval ``[start, end)`` with aware bounds."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if to_utc(self.end) <= to_utc(self.start):
            raise ValueError("a slot must end after it starts")


def overlaps(a: TimeSlot, b: TimeSlot) -> bool:
    """True when the half-open intervals share any instant. Touching is not overlap."""
    return to_utc(a.start) < to_utc(b.end) and to_utc(b.start) < to_utc(a.end)


def merge_slots(slots: Iterable[TimeSlot]) -> list[TimeSlot]:
    """Merge overlapping or touching slots. Results are normalised to UTC."""
    ordered = sorted(
        (TimeSlot(to_utc(s.start), to_utc(s.end)) for s in slots),
        key=lambda s: s.start,
    )
    merged: list[TimeSlot] = []
    for slot in ordered:
        if merged and slot.start <= merged[-1].end:
            last = merged[-1]
            if slot.end > last.end:
                merged[-1] = TimeSlot(last.start, slot.end)
        else:
            merged.append(slot)
    return merged


def total_booked(slots: Iterable[TimeSlot]) -> timedelta:
    """Total time covered by ``slots``, counting overlapped time once."""
    return sum((s.end - s.start for s in merge_slots(slots)), timedelta())


# --------------------------------------------------------------------------- #
# Part 7: humanized relative time and monotonic stopwatch
# --------------------------------------------------------------------------- #


def humanize(when: datetime, now: datetime) -> str:
    """Relative description of ``when`` as seen from the injected ``now``.

    Differences use absolute time, so a 23-hour span across a DST change reads
    as "23 hours", not "1 day".
    """
    seconds = (to_utc(when) - to_utc(now)).total_seconds()
    magnitude = abs(seconds)
    if magnitude < 45:
        return "just now"
    if magnitude < 3600:
        unit, size = "minute", 60
    elif magnitude < 86400:
        unit, size = "hour", 3600
    elif magnitude < 30 * 86400:
        unit, size = "day", 86400
    else:
        return f"on {when.date().isoformat()}"

    n = max(1, int(magnitude // size))
    text = f"{n} {unit}{'' if n == 1 else 's'}"
    return f"in {text}" if seconds > 0 else f"{text} ago"


class Stopwatch:
    """Elapsed-time meter over a monotonic clock.

    The default clock is :func:`time.monotonic`, which never jumps backwards
    when the wall clock is adjusted. Inject a different callable in tests.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started: float | None = None
        self._last = 0.0

    def start(self) -> None:
        if self._started is not None:
            raise RuntimeError("stopwatch is already running")
        self._started = self._clock()

    def stop(self) -> float:
        """Stop and return the seconds elapsed since :meth:`start`."""
        if self._started is None:
            raise RuntimeError("stopwatch is not running")
        self._last = self._clock() - self._started
        self._started = None
        return self._last

    def elapsed(self) -> float:
        """Live reading while running, the final reading once stopped, else 0.0."""
        if self._started is not None:
            return self._clock() - self._started
        return self._last
