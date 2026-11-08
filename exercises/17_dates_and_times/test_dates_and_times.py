"""Dates and times: scheduling and billing backend scenario. See README.md."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from types import ModuleType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

BERLIN = "Europe/Berlin"
TOKYO = "Asia/Tokyo"
MAR_29_NOON_UTC = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)


def berlin(y: int, mo: int, d: int, h: int, mi: int = 0, fold: int = 0) -> datetime:
    """Aware Berlin datetime built directly, independent of the code under test."""
    return datetime(y, mo, d, h, mi, fold=fold, tzinfo=ZoneInfo(BERLIN))


def utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0, us: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


class ManualClock:
    """Injected monotonic clock whose reading the test controls."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestPart1AwareAndZones:
    def test_to_utc_converts_fixed_offset_to_utc(self, main: ModuleType) -> None:
        dt = datetime(2026, 3, 1, 10, 0, tzinfo=timezone(timedelta(hours=1)))
        result = main.to_utc(dt)
        assert result == utc(2026, 3, 1, 9), "10:00+01:00 should be 09:00 UTC"
        assert result.utcoffset() == timedelta(0), "result must carry a zero UTC offset"

    def test_to_utc_rejects_naive_datetime(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.to_utc(datetime(2026, 3, 1, 10, 0))

    def test_localize_attaches_zone_and_fold_selects_occurrence(self, main: ModuleType) -> None:
        wall = datetime(2026, 10, 25, 2, 30)
        first = main.localize(wall, BERLIN)
        second = main.localize(wall, BERLIN, fold=1)
        assert first.utcoffset() == timedelta(hours=2), "fold=0 is the earlier CEST occurrence"
        assert second.utcoffset() == timedelta(hours=1), "fold=1 is the later CET occurrence"
        assert main.to_utc(first) == utc(2026, 10, 25, 0, 30)
        assert main.to_utc(second) == utc(2026, 10, 25, 1, 30)

    def test_localize_rejects_aware_input_and_bad_fold(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.localize(berlin(2026, 3, 1, 9), BERLIN)
        with pytest.raises(ValueError):
            main.localize(datetime(2026, 3, 1, 9), BERLIN, fold=2)

    def test_in_zone_displays_wall_clock_in_target_zone(self, main: ModuleType) -> None:
        result = main.in_zone(MAR_29_NOON_UTC, BERLIN)
        assert result.hour == 14, "12:00Z is 14:00 in Berlin after the March DST switch"
        assert result.utcoffset() == timedelta(hours=2)
        assert result == MAR_29_NOON_UTC, "display conversion must not change the instant"

    def test_in_zone_rejects_naive_and_unknown_zone(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.in_zone(datetime(2026, 3, 29, 12, 0), BERLIN)
        with pytest.raises(ZoneInfoNotFoundError):
            main.in_zone(MAR_29_NOON_UTC, "Mars/Olympus_Mons")

    def test_format_in_zone_uses_local_abbreviation(self, main: ModuleType) -> None:
        assert main.format_in_zone(MAR_29_NOON_UTC, BERLIN) == "2026-03-29 14:00 CEST"
        winter = utc(2026, 1, 15, 12)
        assert main.format_in_zone(winter, BERLIN) == "2026-01-15 13:00 CET"


class TestPart2TimestampParsing:
    def test_iso_offset_is_normalised_to_utc(self, main: ModuleType) -> None:
        result = main.parse_timestamp("2026-03-29T02:30:00+01:00")
        assert result == utc(2026, 3, 29, 1, 30)
        assert result.utcoffset() == timedelta(0), "parsed value must be aware UTC"

    def test_iso_z_suffix_is_accepted(self, main: ModuleType) -> None:
        assert main.parse_timestamp("2026-03-29T12:00:00Z") == MAR_29_NOON_UTC

    def test_iso_fractional_seconds_survive_conversion(self, main: ModuleType) -> None:
        result = main.parse_timestamp("2026-03-29T14:00:00.123456+02:00")
        assert result == utc(2026, 3, 29, 12, 0, 0, 123456), "microseconds must be preserved"

    def test_iso_without_offset_is_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.parse_timestamp("2026-03-29T12:00:00")
        with pytest.raises(ValueError):
            main.parse_timestamp("2026-03-29")

    def test_rfc2822_is_parsed_to_utc(self, main: ModuleType) -> None:
        assert main.parse_timestamp("Sun, 29 Mar 2026 12:00:00 +0200") == utc(2026, 3, 29, 10)

    def test_rfc2822_unknown_offset_is_rejected(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.parse_timestamp("Sun, 29 Mar 2026 12:00:00 -0000")

    def test_epoch_seconds_as_int_string_and_float(self, main: ModuleType) -> None:
        assert main.parse_timestamp(1774785600) == MAR_29_NOON_UTC
        assert main.parse_timestamp("1774785600") == MAR_29_NOON_UTC
        assert main.parse_timestamp(1774785600.5) == utc(2026, 3, 29, 12, 0, 0, 500000)

    def test_epoch_millis_detected_by_magnitude(self, main: ModuleType) -> None:
        assert main.parse_timestamp("1774785600123") == utc(2026, 3, 29, 12, 0, 0, 123000)
        assert main.parse_timestamp(1774785600123) == utc(2026, 3, 29, 12, 0, 0, 123000)

    def test_epoch_threshold_boundary(self, main: ModuleType) -> None:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        assert main.parse_timestamp(99_999_999_999) == epoch + timedelta(seconds=99_999_999_999)
        assert main.parse_timestamp(100_000_000_000) == utc(1973, 3, 3, 9, 46, 40)

    def test_bool_empty_and_garbage_inputs(self, main: ModuleType) -> None:
        with pytest.raises(TypeError):
            main.parse_timestamp(True)
        with pytest.raises(ValueError):
            main.parse_timestamp("   ")
        with pytest.raises(ValueError):
            main.parse_timestamp("not a timestamp")

    def test_to_epoch_ms_round_trips_and_rejects_naive(self, main: ModuleType) -> None:
        assert main.to_epoch_ms(utc(2026, 3, 29, 12, 0, 0, 123000)) == 1774785600123
        assert main.to_epoch_ms(berlin(2026, 3, 29, 14)) == 1774785600000
        with pytest.raises(ValueError):
            main.to_epoch_ms(datetime(2026, 3, 29, 12))


class TestPart3WallVsElapsed:
    def test_calendar_days_keep_wall_clock_across_spring_forward(self, main: ModuleType) -> None:
        start = berlin(2026, 3, 28, 9)
        result = main.add_calendar_days(start, 1)
        assert (result.hour, result.minute) == (9, 0), "wall clock must stay at 09:00"
        assert result.utcoffset() == timedelta(hours=2), "Mar 29 09:00 is already CEST"
        assert main.to_utc(result) == utc(2026, 3, 29, 7)
        assert result.tzinfo is not None and result.tzinfo.key == BERLIN, "zone must be kept"

    def test_elapsed_24h_across_spring_forward_is_not_a_calendar_day(self, main: ModuleType) -> None:
        start = berlin(2026, 3, 28, 9)
        result = main.add_elapsed(start, timedelta(hours=24))
        assert result.hour == 10, "24 absolute hours after 09:00 CET is 10:00 CEST"
        assert main.to_utc(result) == utc(2026, 3, 29, 8)
        assert main.add_calendar_days(start, 1).hour == 9, "calendar day stays at 09:00"

    def test_elapsed_between_same_zone_is_absolute_across_dst(self, main: ModuleType) -> None:
        # Same ZoneInfo object: naive subtraction would report 24 hours here.
        result = main.elapsed_between(berlin(2026, 3, 28, 9), berlin(2026, 3, 29, 9))
        assert result == timedelta(hours=23), "the spring-forward day has only 23 hours"

    def test_elapsed_between_across_zones_and_sign(self, main: ModuleType) -> None:
        tokyo = datetime(2026, 3, 29, 9, 0, tzinfo=ZoneInfo(TOKYO))  # 00:00Z
        berlin_9 = berlin(2026, 3, 29, 9)  # 07:00Z
        assert main.elapsed_between(tokyo, berlin_9) == timedelta(hours=7)
        assert main.elapsed_between(berlin_9, tokyo) == timedelta(hours=-7)


class TestPart4Calendars:
    def test_add_months_clamps_to_month_end(self, main: ModuleType) -> None:
        assert main.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert main.add_months(date(2028, 1, 31), 1) == date(2028, 2, 29), "2028 is a leap year"
        assert main.add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)

    def test_add_months_crosses_year_boundaries(self, main: ModuleType) -> None:
        assert main.add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
        assert main.add_months(date(2026, 1, 15), -1) == date(2025, 12, 15)
        assert main.add_months(date(2026, 5, 10), 0) == date(2026, 5, 10)

    def test_days_in_month_follows_gregorian_leap_rules(self, main: ModuleType) -> None:
        assert main.days_in_month(2024, 2) == 29
        assert main.days_in_month(1900, 2) == 28, "1900 is not a leap year"
        assert main.days_in_month(2000, 2) == 29, "2000 is a leap year"
        with pytest.raises(ValueError):
            main.days_in_month(2026, 13)

    def test_billing_dates_are_anchored_not_chained(self, main: ModuleType) -> None:
        dates = main.billing_dates(date(2026, 1, 31), 4)
        assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]

    def test_billing_dates_zero_and_negative_count(self, main: ModuleType) -> None:
        assert main.billing_dates(date(2026, 1, 31), 0) == []
        with pytest.raises(ValueError):
            main.billing_dates(date(2026, 1, 31), -1)

    def test_business_day_predicate_respects_weekend_and_holidays(self, main: ModuleType) -> None:
        cal = main.BusinessCalendar(holidays=[date(2026, 12, 25)])
        assert not cal.is_business_day(date(2026, 3, 28)), "Saturday"
        assert cal.is_business_day(date(2026, 3, 30)), "Monday"
        assert not cal.is_business_day(date(2026, 12, 25)), "Christmas is a holiday"

    def test_add_business_days_skips_weekend_and_holiday(self, main: ModuleType) -> None:
        cal = main.BusinessCalendar(holidays=[date(2026, 12, 25)])
        # Fri 25 is a holiday; Sat, Sun skipped; lands on Mon 28.
        assert cal.add_business_days(date(2026, 12, 24), 1) == date(2026, 12, 28)
        assert cal.add_business_days(date(2026, 3, 2), 5) == date(2026, 3, 9)

    def test_add_business_days_from_weekend_and_zero_step(self, main: ModuleType) -> None:
        cal = main.BusinessCalendar()
        assert cal.add_business_days(date(2026, 3, 28), 1) == date(2026, 3, 30), "Sat +1 is Mon"
        assert cal.add_business_days(date(2026, 3, 28), -1) == date(2026, 3, 27), "Sat -1 is Fri"
        assert cal.add_business_days(date(2026, 3, 28), 0) == date(2026, 3, 28), "n=0 is identity"

    def test_business_days_between_counts_half_open_range(self, main: ModuleType) -> None:
        cal = main.BusinessCalendar(holidays=[date(2026, 12, 25)])
        assert cal.business_days_between(date(2026, 3, 2), date(2026, 3, 9)) == 5
        assert cal.business_days_between(date(2026, 3, 6), date(2026, 3, 9)) == 1
        assert cal.business_days_between(date(2026, 3, 9), date(2026, 3, 2)) == -5
        assert cal.business_days_between(date(2026, 12, 24), date(2026, 12, 28)) == 1

    @settings(
        derandomize=True,
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        start=st.dates(date(2026, 1, 1), date(2026, 12, 31)),
        end=st.dates(date(2026, 1, 1), date(2026, 12, 31)),
    )
    def test_between_and_add_are_inverse_for_business_days(
        self, main: ModuleType, start: date, end: date
    ) -> None:
        cal = main.BusinessCalendar(holidays=[date(2026, 12, 25), date(2026, 7, 3)])
        if end > start and cal.is_business_day(end):
            steps = cal.business_days_between(start, end)
            assert cal.add_business_days(start, steps) == end, (
                f"adding {steps} business days to {start} must land on {end}"
            )


class TestPart5RecurringMeetings:
    def test_weekly_meeting_keeps_local_time_across_spring_forward(self, main: ModuleType) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 3, 16), weekday=0, at=time(9, 0), zone=BERLIN, count=4
        )
        assert [(o.hour, o.minute) for o in occ] == [(9, 0)] * 4, "wall clock must stay 09:00"
        assert [o.utcoffset() for o in occ] == [timedelta(hours=h) for h in (1, 1, 2, 2)]
        assert [main.to_utc(o).hour for o in occ] == [8, 8, 7, 7]

    def test_first_occurrence_is_next_matching_weekday_on_or_after_start(
        self, main: ModuleType
    ) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 3, 18), weekday=0, at=time(9, 0), zone=BERLIN, count=1
        )
        assert occ[0].date() == date(2026, 3, 23), "Wed 18 Mar -> next Monday is 23 Mar"

    def test_nonexistent_time_shifts_forward_by_default(self, main: ModuleType) -> None:
        # Sun 29 Mar 2026: 02:30 does not exist in Berlin; it becomes 03:30 CEST.
        occ = main.weekly_occurrences(
            start=date(2026, 3, 22), weekday=6, at=time(2, 30), zone=BERLIN, count=2
        )
        assert [(o.hour, o.minute) for o in occ] == [(2, 30), (3, 30)]
        assert main.to_utc(occ[1]) == utc(2026, 3, 29, 1, 30), "shifted instant is 01:30Z"
        assert occ[1].utcoffset() == timedelta(hours=2)

    def test_nonexistent_time_skip_policy_drops_occurrence(self, main: ModuleType) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 3, 22),
            weekday=6,
            at=time(2, 30),
            zone=BERLIN,
            count=2,
            gap=main.Gap.SKIP,
        )
        assert [o.date() for o in occ] == [date(2026, 3, 22), date(2026, 4, 5)]

    def test_ambiguous_time_defaults_to_earlier_occurrence(self, main: ModuleType) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 10, 18), weekday=6, at=time(2, 30), zone=BERLIN, count=2
        )
        assert [o.utcoffset() for o in occ] == [timedelta(hours=2)] * 2, "both CEST"

    def test_ambiguous_time_later_policy_picks_second_occurrence(self, main: ModuleType) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 10, 18),
            weekday=6,
            at=time(2, 30),
            zone=BERLIN,
            count=2,
            overlap=main.Overlap.LATER,
        )
        assert occ[0].utcoffset() == timedelta(hours=2)
        assert occ[1].utcoffset() == timedelta(hours=1), "Oct 25 LATER is CET"
        assert main.to_utc(occ[1]) == utc(2026, 10, 25, 1, 30)

    def test_ambiguous_time_skip_policy_drops_occurrence(self, main: ModuleType) -> None:
        occ = main.weekly_occurrences(
            start=date(2026, 10, 18),
            weekday=6,
            at=time(2, 30),
            zone=BERLIN,
            count=2,
            overlap=main.Overlap.SKIP,
        )
        assert [o.date() for o in occ] == [date(2026, 10, 18), date(2026, 11, 1)]

    def test_validation_and_zero_count(self, main: ModuleType) -> None:
        base = {"start": date(2026, 3, 16), "at": time(9, 0), "zone": BERLIN}
        assert main.weekly_occurrences(weekday=0, count=0, **base) == []
        with pytest.raises(ValueError):
            main.weekly_occurrences(weekday=7, count=1, **base)
        with pytest.raises(ValueError):
            main.weekly_occurrences(weekday=0, count=-1, **base)
        with pytest.raises(ValueError):
            main.weekly_occurrences(weekday=0, count=1, **{**base, "at": time(9, 0, tzinfo=UTC)})


class TestPart6BookingSlots:
    def test_slot_requires_aware_bounds_and_positive_length(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.TimeSlot(datetime(2026, 3, 30, 9), datetime(2026, 3, 30, 10))
        with pytest.raises(ValueError):
            main.TimeSlot(berlin(2026, 3, 30, 9), berlin(2026, 3, 30, 9))
        with pytest.raises(ValueError):
            main.TimeSlot(berlin(2026, 3, 30, 10), berlin(2026, 3, 30, 9))

    def test_overlap_detected_across_time_zones(self, main: ModuleType) -> None:
        berlin_slot = main.TimeSlot(berlin(2026, 3, 30, 9), berlin(2026, 3, 30, 10))  # 07:00-08:00Z
        tokyo_slot = main.TimeSlot(
            datetime(2026, 3, 30, 16, 30, tzinfo=ZoneInfo(TOKYO)),  # 07:30-08:30Z
            datetime(2026, 3, 30, 17, 30, tzinfo=ZoneInfo(TOKYO)),
        )
        assert main.overlaps(berlin_slot, tokyo_slot)
        assert main.overlaps(tokyo_slot, berlin_slot)

    def test_touching_slots_do_not_overlap(self, main: ModuleType) -> None:
        first = main.TimeSlot(berlin(2026, 3, 30, 9), berlin(2026, 3, 30, 10))
        second = main.TimeSlot(berlin(2026, 3, 30, 10), berlin(2026, 3, 30, 11))
        assert not main.overlaps(first, second), "half-open intervals: end is exclusive"

    def test_overlap_in_ambiguous_hour_uses_absolute_time(self, main: ModuleType) -> None:
        # Same ZoneInfo object: naive wall comparison would report an overlap.
        a = main.TimeSlot(berlin(2026, 10, 25, 2, 0, fold=0), berlin(2026, 10, 25, 2, 10, fold=0))
        b = main.TimeSlot(berlin(2026, 10, 25, 2, 5, fold=1), berlin(2026, 10, 25, 2, 30, fold=1))
        assert not main.overlaps(a, b), "a ends 00:10Z, b starts 01:05Z"

    def test_merge_combines_overlapping_and_touching_across_zones(self, main: ModuleType) -> None:
        slots = [
            main.TimeSlot(berlin(2026, 3, 30, 13), berlin(2026, 3, 30, 14)),  # 11-12Z
            main.TimeSlot(
                datetime(2026, 3, 30, 16, 30, tzinfo=ZoneInfo(TOKYO)),  # 07:30-08:30Z
                datetime(2026, 3, 30, 17, 30, tzinfo=ZoneInfo(TOKYO)),
            ),
            main.TimeSlot(berlin(2026, 3, 30, 12), berlin(2026, 3, 30, 13)),  # 10-11Z
            main.TimeSlot(berlin(2026, 3, 30, 9), berlin(2026, 3, 30, 10)),  # 07-08Z
        ]
        merged = main.merge_slots(slots)
        spans = [(main.to_utc(m.start), main.to_utc(m.end)) for m in merged]
        assert spans == [
            (utc(2026, 3, 30, 7), utc(2026, 3, 30, 8, 30)),
            (utc(2026, 3, 30, 10), utc(2026, 3, 30, 12)),
        ], "overlapping slots fuse; touching slots fuse; the rest stay separate"

    def test_total_booked_counts_overlap_once(self, main: ModuleType) -> None:
        slots = [
            main.TimeSlot(berlin(2026, 3, 30, 9), berlin(2026, 3, 30, 10)),
            main.TimeSlot(
                datetime(2026, 3, 30, 16, 30, tzinfo=ZoneInfo(TOKYO)),
                datetime(2026, 3, 30, 17, 30, tzinfo=ZoneInfo(TOKYO)),
            ),
            main.TimeSlot(berlin(2026, 3, 30, 12), berlin(2026, 3, 30, 14)),
        ]
        assert main.total_booked(slots) == timedelta(hours=3, minutes=30)
        assert main.total_booked([]) == timedelta(0)


class TestPart7HumanizeAndStopwatch:
    def test_within_45_seconds_is_just_now(self, main: ModuleType) -> None:
        assert main.humanize(MAR_29_NOON_UTC + timedelta(seconds=30), MAR_29_NOON_UTC) == "just now"
        assert main.humanize(MAR_29_NOON_UTC - timedelta(seconds=44), MAR_29_NOON_UTC) == "just now"

    def test_past_units_are_singular_or_plural(self, main: ModuleType) -> None:
        now = MAR_29_NOON_UTC
        assert main.humanize(now - timedelta(minutes=1), now) == "1 minute ago"
        assert main.humanize(now - timedelta(hours=3), now) == "3 hours ago"
        assert main.humanize(now - timedelta(days=2), now) == "2 days ago"

    def test_future_units_read_in(self, main: ModuleType) -> None:
        now = MAR_29_NOON_UTC
        assert main.humanize(now + timedelta(days=2), now) == "in 2 days"
        assert main.humanize(now + timedelta(minutes=90), now) == "in 1 hour"
        assert main.humanize(now + timedelta(minutes=59), now) == "in 59 minutes"

    def test_old_instants_fall_back_to_calendar_date(self, main: ModuleType) -> None:
        assert main.humanize(utc(2026, 2, 1, 12), MAR_29_NOON_UTC) == "on 2026-02-01"

    def test_dst_day_is_23_absolute_hours(self, main: ModuleType) -> None:
        now = berlin(2026, 3, 29, 12)
        when = berlin(2026, 3, 28, 12)
        assert main.humanize(when, now) == "23 hours ago", "absolute difference, not '1 day'"

    def test_humanize_uses_only_injected_now(self, main: ModuleType) -> None:
        when = MAR_29_NOON_UTC + timedelta(hours=1)
        assert main.humanize(when, MAR_29_NOON_UTC) == "in 1 hour"
        assert main.humanize(when, MAR_29_NOON_UTC + timedelta(hours=3)) == "2 hours ago"

    def test_humanize_rejects_naive_inputs(self, main: ModuleType) -> None:
        with pytest.raises(ValueError):
            main.humanize(datetime(2026, 3, 29, 12), MAR_29_NOON_UTC)

    def test_stopwatch_measures_with_injected_clock(self, main: ModuleType) -> None:
        clock = ManualClock(100.0)
        watch = main.Stopwatch(clock)
        assert watch.elapsed() == 0.0, "no reading before start"
        watch.start()
        clock.now = 102.5
        assert watch.elapsed() == 2.5, "live reading while running"
        clock.now = 103.0
        assert watch.stop() == 3.0
        clock.now = 500.0
        assert watch.elapsed() == 3.0, "reading freezes after stop"

    def test_stopwatch_state_errors(self, main: ModuleType) -> None:
        watch = main.Stopwatch(ManualClock())
        with pytest.raises(RuntimeError):
            watch.stop()
        watch.start()
        with pytest.raises(RuntimeError):
            watch.start()
        watch.stop()
        with pytest.raises(RuntimeError):
            watch.stop()

    def test_default_clock_never_reports_negative(self, main: ModuleType) -> None:
        watch = main.Stopwatch()
        watch.start()
        assert watch.stop() >= 0.0
