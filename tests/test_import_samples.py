"""SCRUM-18: valid schema 1.0 workbooks import successfully."""
import datetime as dt

from shiftopt.ingest import read_input


def test_small_input(small_input):
    r = read_input(small_input)
    assert r.ok, [str(e) for e in r.errors]
    d = r.data
    assert d.week_start == dt.date(2026, 11, 1)
    assert len(d.employees) == 10
    assert len(d.demand) == 58
    assert len(d.templates) == 35
    assert sorted(d.break_rules) == [4, 6, 8]
    e2 = d.employees["E002"]
    assert e2.unavailable_days == {2, 4}
    assert e2.preferred_start == 10 * 60  # stored, but ignored by the optimizer for a pair


def test_large_input(large_input):
    r = read_input(large_input)
    assert r.ok, [str(e) for e in r.errors]
    assert len(r.data.employees) == 50


def test_infeasible_input_is_valid_input(infeasible_input):
    """Spec section 6: only three active days is INFEASIBLE, not INVALID_INPUT."""
    r = read_input(infeasible_input)
    assert r.ok, [str(e) for e in r.errors]
    active_days = {d for (d, _) in r.data.demand}
    assert len(active_days) == 3


def test_missing_hour_is_forbidden_but_zero_demand_is_allowed(small_input):
    d = read_input(small_input).data
    sunday = dt.date(2026, 11, 1)
    assert d.required_in_slot(sunday, 9 * 60) == 0  # 09:00 listed with demand 0 -> allowed
    assert d.required_in_slot(sunday, 18 * 60) is None  # 18:00 not listed -> forbidden
    assert d.required_in_slot(sunday, 10 * 60 + 30) == d.required_in_slot(sunday, 10 * 60)  # both halves
