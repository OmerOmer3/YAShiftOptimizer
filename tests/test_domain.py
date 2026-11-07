"""SCRUM-16: shared domain model with 30-minute slots and half-open intervals."""
import datetime as dt

from shiftopt.domain import Assignment, Break, day_of_week, minutes_to_time, slots_in, time_to_minutes


def test_time_conversion_round_trip():
    assert time_to_minutes(dt.time(9, 30)) == 570
    assert minutes_to_time(570) == dt.time(9, 30)


def test_half_open_interval_excludes_end():
    assert list(slots_in(600, 660)) == [600, 630]  # 10:00-11:00 -> slots 10:00 and 10:30


def test_break_removes_only_its_own_slot():
    # Spec section 1 example: a break 10:00-10:30 removes the employee from coverage at 10:00 only.
    a = Assignment(dt.date(2026, 11, 2), "E001", "X", 480, 720, (Break(600, 630),), 4, 200)
    working = a.working_slots()
    assert 600 not in working
    assert 630 in working
    assert 570 in working
    assert len(working) == 7  # 8 slots in a 4 h shift minus one break slot


def test_day_numbering_matches_spec():
    assert day_of_week(dt.date(2026, 11, 1)) == 1  # Sunday
    assert day_of_week(dt.date(2026, 11, 6)) == 6  # Friday
    assert day_of_week(dt.date(2026, 11, 7)) == 7  # Saturday, never valid
