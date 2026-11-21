"""SCRUM-106: the engine's reported totals are the totals of the schedule it returns.

Found on a slower computer: when stage 1 (shortage) is stopped by the time budget, the
shortage variables could keep slack (they were only bounded below), and the engine
reported the stage-1 objective (440) instead of the schedule's real shortage (385, as the
independent checker computed). Three layers of protection are tested here:
  * the model's shortage variables are exact (independent of machine speed);
  * the engine reports the returned schedule's totals when time runs out;
  * the service refuses to store a run whose engine totals differ from the checker.
"""
from __future__ import annotations

import pytest
from ortools.sat.python import cp_model

from shiftopt.db import Store
from shiftopt.engine.model import ScheduleModel
from shiftopt.engine.solver import SolveResult, solve
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.service import run_initial
from shiftopt.validator import validate
from tests.builders import flat_demand, h, make_input


def test_shortage_variables_are_exact():
    """Maximize the shortage instead of minimizing it: a lower bound alone would let every
    shortage variable rise to RequiredEmployees, far above the schedule's real shortage.
    Exact variables keep the objective equal to what the checker counts. No time limit
    is involved, so the result does not depend on the computer."""
    data = make_input(employees=[("E1", ()), ("E2", ())], demand=flat_demand([1, 2, 3, 4], 8, 12, 2),
                      templates=[(f"S{d}", d, h(8), 4, 10.0) for d in (1, 2, 3, 4)])
    sm = ScheduleModel(data)
    sm.model.Maximize(sm.total_shortage)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.Solve(sm.model) == cp_model.OPTIMAL
    actual = validate(data, sm.extract(solver)).metrics.total_shortage_slots
    assert round(solver.ObjectiveValue()) == actual
    assert actual < 2 * 8 * 4          # the employees do cover something; the bound alone would give 64


@pytest.mark.parametrize("budget,workers,seed", [(2.5, 8, 0), (3.5, 4, 0), (5, 8, 1)])
def test_reported_totals_match_the_checker_when_time_runs_out(large_input, budget, workers, seed):
    """Short budgets on Input_Large_50 stop stage 1 before it is proven. These settings
    reproduced the bug on a 12-core machine (e.g. 254 reported, 239 actual)."""
    data = read_input(large_input).data
    r = solve(data, time_budget_s=budget, workers=workers, seed=seed)
    if r.schedule is None:
        pytest.skip(f"no schedule within {budget}s on this computer ({r.status})")
    m = validate(data, r.schedule).metrics
    assert r.total_shortage_slots == m.total_shortage_slots
    assert r.total_cost == pytest.approx(m.total_cost)


def test_run_with_engine_totals_that_disagree_is_an_error(tmp_path, small_input, small_output, monkeypatch):
    """Safety net: a valid schedule reported with the wrong totals is never stored as a draft."""
    store = Store(str(tmp_path / "db.sqlite"))
    import_workbook(store, small_input)
    schedule = read_schedule(small_output)              # valid; real shortage 0, cost 11,540

    def fake_solve(data, budget, **kw):
        return SolveResult(status="FEASIBLE", schedule=schedule, time_budget_seconds=budget,
                           first_feasible_seconds=0.1, optimization_seconds=0.1, total_shortage_slots=55,
                           total_cost=11540.0, coverage_proven=False, cost_proven=False,
                           termination_reason="TIME_LIMIT_COVERAGE_NOT_PROVEN")

    monkeypatch.setattr("shiftopt.service.runs.solve", fake_solve)
    run = run_initial(store, 5).run
    assert run.status == "ERROR" and run.termination_reason == "METRICS_MISMATCH"
    assert run.schedule_version is None
    assert any("55" in d and "0" in d for d in run.diagnostics)
