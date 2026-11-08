# 17 · Dates and times

A scheduling and billing backend. Timestamps arrive from logs, clients, and
payment providers in mixed formats. Meetings recur in local time across DST.
Bookings span time zones. Invoices bill on month ends. Business-day SLAs skip
weekends and holidays. Every "now" is injected, and nothing in these tests
depends on the machine's local time zone.

Implement everything in `main.py`. Zones come from `zoneinfo` (IANA keys such
as `"Europe/Berlin"`). Invalid input raises `ValueError` unless a Part says
otherwise. Naive datetimes are rejected wherever an absolute instant is needed.

Key rule: compare, subtract, and merge aware datetimes in UTC. Two aware
datetimes that share one `ZoneInfo` object are compared and subtracted on their
wall-clock fields, which is wrong across DST.

## Part 1: aware vs naive, UTC storage, display

- `to_utc(dt: datetime) -> datetime`: aware UTC copy. `ValueError` if `dt` is naive.
- `localize(wall: datetime, zone: str, fold: int = 0) -> datetime`: attach `zone`
  to a naive wall time. `fold` (0 or 1) picks the earlier or later of two
  ambiguous occurrences. `ValueError` if `wall` is aware or `fold` is not 0/1.
  Nonexistent wall times are returned unchanged; policy comes later.
- `in_zone(dt: datetime, zone: str) -> datetime`: the instant as wall time in `zone`.
  `ValueError` if naive. An unknown zone raises `ZoneInfoNotFoundError`.
- `format_in_zone(dt: datetime, zone: str) -> str`: `"%Y-%m-%d %H:%M %Z"` in `zone`,
  e.g. `"2026-03-29 14:00 CEST"`.

## Part 2: timestamp parsing into aware UTC

- `parse_timestamp(raw: str | int | float) -> datetime`: returns aware UTC.
  - Numbers, and numeric strings, are Unix epoch values. Magnitude `>= 100_000_000_000`
    means milliseconds, otherwise seconds. Floats keep fractional seconds.
  - Strings starting with a weekday and comma (`"Sun, 29 Mar 2026 12:00:00 +0200"`)
    are RFC 2822, parsed with `email.utils`. `-0000` (unknown offset) is `ValueError`.
  - Anything else is ISO 8601 via `datetime.fromisoformat`, including a `Z` suffix.
    An offset is mandatory: naive or date-only input is `ValueError`.
  - Empty or whitespace-only strings are `ValueError`. Booleans and other types are `TypeError`.
- `to_epoch_ms(dt: datetime) -> int`: whole milliseconds since 1970-01-01T00:00Z,
  truncated toward the past. `ValueError` if naive.

## Part 3: wall-clock vs elapsed-time arithmetic

- `add_calendar_days(dt: datetime, days: int) -> datetime`: move by calendar days,
  keeping the wall-clock time and the zone. The result uses `fold=0`.
- `add_elapsed(dt: datetime, delta: timedelta) -> datetime`: move by absolute time.
  The result is expressed in `dt`'s zone. `ValueError` if `dt` is naive.
- `elapsed_between(start: datetime, end: datetime) -> timedelta`: signed absolute
  difference `end - start`. `ValueError` if either input is naive.

## Part 4: calendars: month arithmetic, billing dates, business days

Month arithmetic:
- `days_in_month(year: int, month: int) -> int`: leap-year aware. An invalid month
  raises `ValueError`.
- `add_months(d: date, months: int) -> date`: adds calendar months and clamps the day
  to the target month's length. Negative values are allowed.
- `billing_dates(anchor: date, count: int) -> list[date]`: `count` dates, each computed
  from `anchor` (no drift). The first element is `anchor`. `count == 0` gives `[]`.
  Negative `count` is `ValueError`.

Business days, via `BusinessCalendar(holidays: Iterable[date] = (), weekend: frozenset[int] = frozenset({5, 6}))`.
Passing `datetime` objects as holidays is `TypeError`.
- `is_business_day(d: date) -> bool`: not in `weekend` (Monday is 0), not a holiday.
- `add_business_days(d: date, n: int) -> date`: step `n` business days forward, or back
  if negative. Each step lands on a business day, so from a weekend start the first
  forward step goes to the next business day. `n == 0` returns `d` unchanged.
- `business_days_between(start: date, end: date) -> int`: number of business days in
  `(start, end]`. If `end < start`, returns the negated count of `(end, start]`.

## Part 5: recurring meetings with an explicit DST policy

- `class Gap(Enum)`: `SKIP`, `SHIFT_FORWARD`. Policy for a nonexistent wall time.
  `SHIFT_FORWARD` means the instant the wall clock jumps to, so 02:30 becomes 03:30 CEST.
- `class Overlap(Enum)`: `EARLIER`, `LATER`, `SKIP`. Policy for an ambiguous wall time
  (the first or second occurrence, or drop it).
- `weekly_occurrences(*, start: date, weekday: int, at: datetime.time, zone: str, count: int, gap: Gap = Gap.SHIFT_FORWARD, overlap: Overlap = Overlap.EARLIER) -> list[datetime]`:
  - Returns the next `count` occurrences on `weekday` (Monday = 0) on or after `start`,
    at wall time `at` in `zone`, as aware datetimes in ascending order.
  - Dropped occurrences (by `Gap.SKIP` or `Overlap.SKIP`) do not count toward `count`.
  - Validation: `weekday` outside 0..6, aware `at`, or negative `count` raise `ValueError`.

## Part 6: booking slots across time zones

- `@dataclass(frozen=True) class TimeSlot`: half-open `[start, end)` with fields
  `start` and `end`. Both must be aware. `end` must be strictly after `start`. Naive or
  inverted bounds raise `ValueError`.
- `overlaps(a: TimeSlot, b: TimeSlot) -> bool`: true when they share an instant.
  Touching slots (`a.end == b.start`) do not overlap.
- `merge_slots(slots: Iterable[TimeSlot]) -> list[TimeSlot]`: sorted, merged list
  with overlapping or touching slots combined. Results are normalised to UTC.
- `total_booked(slots: Iterable[TimeSlot]) -> timedelta`: total covered time after
  merging, so overlapped time counts once.

## Part 7: humanized relative time and monotonic stopwatch

- `humanize(when: datetime, now: datetime) -> str`: `now` is injected. Both must be aware.
  Uses absolute time, not wall-clock fields. Rules, by absolute `|when - now|`:
  - under 45 seconds: `"just now"`
  - under 1 hour: minutes (at least 1), e.g. `"1 minute ago"`, `"in 59 minutes"`
  - under 24 hours: hours, e.g. `"3 hours ago"`, `"in 1 hour"`
  - under 30 days: days, e.g. `"2 days ago"`, `"in 2 days"`
  - otherwise: `"on YYYY-MM-DD"` using the date of `when` in its own zone
  Use `"in ..."` for future instants and `"... ago"` for past ones. Units are singular for 1.
- `class Stopwatch(clock: Callable[[], float] = time.monotonic)`: elapsed-time meter.
  - `start()`: `RuntimeError` if already running.
  - `stop() -> float`: returns seconds since `start()`. `RuntimeError` if not running.
  - `elapsed() -> float`: live reading while running, the last stopped value after
    `stop()`, and `0.0` before the first `start()`.

## Concepts exercised

- Aware vs naive datetimes; `tzinfo`, `utcoffset()`, and `fold` semantics (PEP 495)
- `zoneinfo`: IANA keys, DST gaps and overlaps, `ZoneInfoNotFoundError`
- Same-zone comparison and subtraction pitfall (wall fields, not instants)
- ISO 8601 via `fromisoformat` (`Z` suffix), RFC 2822 via `email.utils`, epoch seconds vs milliseconds
- `timedelta` vs calendar-day arithmetic across DST
- `calendar.monthrange` and month-end clamping, no-drift billing schedules
- Half-open intervals, interval merging across zones, injected clocks, `time.monotonic`
- `match` statements; dataclasses with `slots=True` and `__post_init__` validation

## Run

```
uv run pytest exercises/17_dates_and_times                       # your implementation
uv run pytest exercises/17_dates_and_times -k part5              # one Part
uv run pytest exercises/17_dates_and_times --solutions           # reference implementation
```

## Stretch (untested)

- Make `weekly_occurrences` accept a `zone` iterable of local rules (for example, a
  second meeting in Tokyo whose wall time sits in a Berlin DST gap).
- Implement `free_windows(slots, day_start, day_end)` over merged bookings.
- Add a `BusinessCalendar.from_ics(text)` loader for holiday calendars.
- Compare `zoneinfo` against `dateutil.tz` behaviour for the same DST edge cases.
