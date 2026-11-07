"""SCRUM-25 (every hard rule) and the first mutation cases of SCRUM-27.

Each test breaks the valid sample schedule in one way and checks that the
validator rejects it with the right rule.
"""
import copy
import datetime as dt

import pytest

from shiftopt.domain import Assignment, Break
from shiftopt.ingest import read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.validator import validate


@pytest.fixture
def data(small_input):
    return read_input(small_input).data


@pytest.fixture
def sched(small_output):
    return read_schedule(small_output)


def _first(sched, eid):
    return next(a for a in sched.assignments if a.employee_id == eid)


def _rules(data, sched):
    return validate(data, sched).rules_broken()


def test_too_few_shifts(data, sched):
    sched.assignments.remove(_first(sched, "E001"))
    assert "TOO_FEW_SHIFTS" in _rules(data, sched)


def test_absence_exemption_allows_three_shifts(data, sched):
    """Spec section 3: only the absent employee may drop below four shifts."""
    sched.assignments.remove(_first(sched, "E001"))
    sched.min_shifts_exempt.add("E001")
    assert validate(data, sched).passed


def test_work_on_unavailable_day(data, sched):
    a = _first(sched, "E001")  # E001 is unavailable on day 1 (Sunday) and 3 (Tuesday)
    sunday = data.templates["D1_08_8"]
    sched.assignments.append(Assignment(dt.date(2026, 11, 1), "E001", "D1_08_8", sunday.start, sunday.end,
                                        (Break(540, 570), Break(660, 690)), 8, sunday.cost))
    assert "UNAVAILABLE_DAY" in _rules(data, sched)


def test_two_shifts_same_day(data, sched):
    a = _first(sched, "E002")
    sched.assignments.append(copy.deepcopy(a))
    rules = _rules(data, sched)
    assert "MORE_THAN_ONE_SHIFT_PER_DAY" in rules


def test_six_shifts_and_over_40_hours(data):
    from shiftopt.domain import Schedule
    s = Schedule()
    # Six 8 h shifts for one employee with no unavailable days would break both the 5-shift and 40 h limits.
    emp = next(e for e in data.employees.values() if not e.unavailable_days) if any(
        not e.unavailable_days for e in data.employees.values()) else None
    eid = emp.employee_id if emp else "E001"
    for day in range(1, 7):
        t = next((t for t in data.templates_on(day) if t.duration_hours == 8), None)
        if t is None:
            continue
        s.assignments.append(Assignment(data.date_for_day(day), eid, t.shift_id, t.start, t.end,
                                        (Break(t.start + 60, t.start + 90), Break(t.start + 180, t.start + 210)), 8, t.cost))
    rules = _rules(data, s)
    assert "TOO_MANY_SHIFTS" in rules
    assert "OVER_40_HOURS" in rules


def test_break_too_early(data, sched):
    a = _first(sched, "E001")
    a.breaks = (Break(a.start + 30, a.start + 60),) + a.breaks[1:]
    assert "BREAK_TOO_EARLY" in _rules(data, sched)


def test_break_too_late(data, sched):
    a = next(x for x in sched.assignments if len(x.breaks) == 1)
    a.breaks = (Break(a.end - 60, a.end - 30),)
    assert "BREAK_TOO_LATE" in _rules(data, sched)


def test_breaks_too_close(data, sched):
    a = next(x for x in sched.assignments if len(x.breaks) == 2)
    b1 = a.breaks[0]
    a.breaks = (b1, Break(b1.end + 30, b1.end + 60))
    assert "BREAKS_TOO_CLOSE" in _rules(data, sched)


def test_wrong_break_count(data, sched):
    a = next(x for x in sched.assignments if len(x.breaks) == 2)
    a.breaks = a.breaks[:1]
    assert "BREAK_COUNT" in _rules(data, sched)


def test_shift_not_matching_template(data, sched):
    a = _first(sched, "E001")
    a.start += 30
    assert "TEMPLATE_MISMATCH" in _rules(data, sched)


def test_shift_in_missing_hour(data, sched):
    a = _first(sched, "E001")
    a.end = 19 * 60  # 18:00 is not listed in Demand
    assert "OUTSIDE_ACTIVE_HOURS" in _rules(data, sched)


def test_break_reduces_coverage(data, sched):
    """A break removes the employee from that slot's AssignedEmployees."""
    before = validate(data, sched).metrics
    a = _first(sched, "E001")
    b = a.breaks[0]
    a.breaks = (Break(b.start + 30, b.end + 30),) + a.breaks[1:]  # shift the break by one slot
    after = validate(data, sched).metrics
    cov_b = {(r.date, r.slot_start): r.assigned for r in before.coverage}
    cov_a = {(r.date, r.slot_start): r.assigned for r in after.coverage}
    assert cov_a[(a.date, b.start)] == cov_b[(a.date, b.start)] + 1
    assert cov_a[(a.date, b.start + 30)] == cov_b[(a.date, b.start + 30)] - 1
