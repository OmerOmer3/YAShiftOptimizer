"""Optimization engine (SCRUM-28 shortage, SCRUM-29 cost, SCRUM-30 budget and statuses,
SCRUM-31 infeasibility).

Every schedule the engine returns is re-checked by the independent validator, and the
engine's own shortage and cost must equal what the validator recomputes.
"""
import pytest

from shiftopt.engine import solve
from shiftopt.ingest import read_input
from shiftopt.validator import validate
from tests.builders import flat_demand, h, make_input

WORKERS = 2  # CI runners have two cores


def _checked(data, result):
    """Validator passes and agrees with the engine's numbers."""
    rep = validate(data, result.schedule)
    assert rep.passed, [str(v) for v in rep.violations]
    assert rep.metrics.total_shortage_slots == result.total_shortage_slots
    assert rep.metrics.total_cost == pytest.approx(result.total_cost)
    return rep


# ---------------------------------------------------------------- course samples
def test_small_sample_is_solved_to_proven_optimum(small_input):
    data = read_input(small_input).data
    r = solve(data, time_budget_s=30, workers=WORKERS)
    assert r.status == "OPTIMAL"
    assert r.coverage_proven and r.cost_proven
    _checked(data, r)
    assert r.total_shortage_slots == 0
    # The course's hand-made sample costs 11,540 and is not claimed optimal (spec section 10).
    assert r.total_cost <= 11540
    assert r.first_feasible_seconds is not None and r.first_feasible_seconds <= r.optimization_seconds


def test_infeasible_sample_is_infeasible_with_reasons(infeasible_input):
    data = read_input(infeasible_input).data
    r = solve(data, time_budget_s=10, workers=WORKERS)
    assert r.status == "INFEASIBLE"
    assert r.schedule is None
    assert r.first_feasible_seconds is None
    assert r.total_shortage_slots is None and r.total_cost is None  # empty, not zero (spec section 9)
    assert any("needs at least 4 shifts" in m for m in r.diagnostics)


def test_large_sample_gives_a_valid_schedule(large_input):
    data = read_input(large_input).data
    r = solve(data, time_budget_s=20, workers=WORKERS)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    _checked(data, r)
    if r.status == "FEASIBLE":
        assert not (r.coverage_proven and r.cost_proven)


def test_tiny_budget_is_never_reported_infeasible(large_input):
    """Spec section 9: running out of time is not INFEASIBLE."""
    data = read_input(large_input).data
    r = solve(data, time_budget_s=0.001, workers=WORKERS)
    assert r.status in ("NO_SOLUTION_FOUND", "FEASIBLE", "OPTIMAL")
    if r.status == "NO_SOLUTION_FOUND":
        assert r.schedule is None and r.first_feasible_seconds is None
    else:
        _checked(data, r)


def test_solver_settings_are_recorded(small_input):
    r = solve(read_input(small_input).data, time_budget_s=10, workers=1, seed=7)
    for key in ("solver_version", "num_workers", "random_seed", "platform", "cpu_count"):
        assert key in r.solver_info
    assert r.solver_info["num_workers"] == 1 and r.solver_info["random_seed"] == 7


# ---------------------------------------------------------------- objective order
def test_cost_never_buys_shortage():
    """One employee, four days, demand 1 from 08:00 to 16:00.
    A cheap 4-hour shift covers 7 of 16 half-hours (one break); an expensive 8-hour shift
    covers 14 (two breaks). Lexicographic order must take the 8-hour shift every day."""
    data = make_input(
        employees=[("E1", ())],
        demand=flat_demand([1, 2, 3, 4], 8, 16),
        templates=[t for d in (1, 2, 3, 4) for t in ((f"C{d}", d, h(8), 4, 10.0), (f"X{d}", d, h(8), 8, 100.0))],
    )
    r = solve(data, time_budget_s=10, workers=WORKERS)
    assert r.status == "OPTIMAL"
    _checked(data, r)
    assert r.total_shortage_slots == 4 * 2
    assert r.total_cost == 400
    assert {a.shift_id[0] for a in r.schedule.assignments} == {"X"}


def test_cheaper_template_wins_when_shortage_is_equal():
    data = make_input(
        employees=[("E1", ())],
        demand=flat_demand([1, 2, 3, 4], 8, 12),
        templates=[t for d in (1, 2, 3, 4) for t in ((f"A{d}", d, h(8), 4, 30.0), (f"B{d}", d, h(8), 4, 20.0))],
    )
    r = solve(data, time_budget_s=10, workers=WORKERS)
    _checked(data, r)
    assert r.total_cost == 80
    assert {a.shift_id[0] for a in r.schedule.assignments} == {"B"}


def test_breaks_are_placed_where_they_cost_no_coverage():
    """Demand is 1 all day except 0 during 10:00-11:00 and 13:00-14:00. The engine decides
    break positions (spec section 2), so both breaks of the 8-hour shift go there."""
    demand = {d: {hr: (0 if hr in (10, 13) else 1) for hr in range(8, 16)} for d in (1, 2, 3, 4)}
    data = make_input(employees=[("E1", ())], demand=demand,
                      templates=[(f"S{d}", d, h(8), 8, 50.0) for d in (1, 2, 3, 4)])
    r = solve(data, time_budget_s=10, workers=WORKERS)
    _checked(data, r)
    assert r.total_shortage_slots == 0
    for a in r.schedule.assignments:
        assert all(b.start // 60 in (10, 13) for b in a.breaks)


def test_two_employees_stagger_breaks():
    """Demand 1 with two employees on the same shift: overlapping breaks would leave a gap,
    staggered breaks cover every slot."""
    data = make_input(employees=[("E1", ()), ("E2", ())], demand=flat_demand([1, 2, 3, 4], 8, 12),
                      templates=[(f"S{d}", d, h(8), 4, 10.0) for d in (1, 2, 3, 4)])
    r = solve(data, time_budget_s=10, workers=WORKERS)
    _checked(data, r)
    assert r.total_shortage_slots == 0
    for d in {a.date for a in r.schedule.assignments}:
        starts = [a.breaks[0].start for a in r.schedule.assignments if a.date == d]
        assert len(set(starts)) == len(starts)


# ---------------------------------------------------------------- hard rules and exemption
def test_minimum_is_not_lowered_automatically():
    """Only three days have templates: the four-shift minimum cannot hold (spec section 1)."""
    data = make_input(employees=[("E1", ())], demand=flat_demand([1, 2, 3], 8, 12),
                      templates=[(f"S{d}", d, h(8), 4, 10.0) for d in (1, 2, 3)])
    r = solve(data, time_budget_s=10, workers=WORKERS)
    assert r.status == "INFEASIBLE"
    assert r.diagnostics == ["E1 can work on only 3 day(s) (Sunday, Monday, Tuesday) but needs at least 4 shifts"]


def test_absence_exemption_allows_fewer_than_four_shifts():
    data = make_input(employees=[("E1", ())], demand=flat_demand([1, 2, 3], 8, 12),
                      templates=[(f"S{d}", d, h(8), 4, 10.0) for d in (1, 2, 3)])
    r = solve(data, time_budget_s=10, workers=WORKERS, min_shifts_exempt=frozenset({"E1"}))
    assert r.status == "OPTIMAL"
    assert len(r.schedule.assignments) == 3
    assert r.schedule.min_shifts_exempt == {"E1"}
    _checked(data, r)


def test_unavailable_days_and_five_shift_cap():
    """Six days of demand, employee blocked on Monday and Wednesday: he can take at most
    four of the remaining days... and never more than five shifts in any case."""
    data = make_input(employees=[("E1", (2, 4)), ("E2", ())], demand=flat_demand(range(1, 7), 8, 12, 2),
                      templates=[(f"S{d}", d, h(8), 4, 10.0) for d in range(1, 7)])
    r = solve(data, time_budget_s=10, workers=WORKERS)
    _checked(data, r)
    e1_days = {a.date.isoweekday() % 7 + 1 for a in r.schedule.assignments if a.employee_id == "E1"}
    assert not e1_days & {2, 4}
    assert sum(a.employee_id == "E2" for a in r.schedule.assignments) == 5
