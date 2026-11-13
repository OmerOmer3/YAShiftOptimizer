"""SCRUM-22 (input tables) and SCRUM-23 (runs and drafts): every run is stored, a run that
finds a valid schedule creates a DRAFT, and no run ever changes the active schedule."""
import pytest

from shiftopt.db import Store
from shiftopt.engine.solver import SolveResult
from shiftopt.ingest import import_workbook, read_input
from shiftopt.service import NoActiveInput, run_initial
from shiftopt.validator import validate

WORKERS = 2


@pytest.fixture
def store(tmp_path, small_input):
    st = Store(tmp_path / "db.sqlite")
    assert import_workbook(st, small_input).status == "OK"
    return st


def test_run_is_saved_with_a_draft(store):
    out = run_initial(store, 20, workers=WORKERS, seed=3)
    r = out.run
    assert r.run_id == "RUN-0001" and r.run_type == "INITIAL" and r.group_size == 2
    assert r.status == "OPTIMAL" and r.validation_passed is True
    assert r.total_shortage_slots == 0
    assert r.schedule_version == "SV-0001" and r.approval_status == "DRAFT"
    assert r.base_schedule_version is None  # initial run (spec section 8)
    assert r.time_budget_seconds == 20
    assert r.first_feasible_seconds is not None and r.first_feasible_seconds <= r.optimization_seconds
    assert r.solver_info["random_seed"] == 3 and r.solver_info["num_workers"] == WORKERS


def test_new_runs_never_change_the_active_schedule(store):
    run_initial(store, 10, workers=WORKERS)
    second = run_initial(store, 10, workers=WORKERS).run
    assert second.schedule_version == "SV-0002"
    assert store.active_schedule_version() is None  # only approval (sprint 3) activates
    assert store.schedule_version_info("SV-0001")["approval_status"] == "DRAFT"


def test_draft_loads_back_exactly(store):
    r = run_initial(store, 20, workers=WORKERS).run
    data = store.load_input(r.input_version)
    sched = store.load_schedule(r.schedule_version)
    rep = validate(data, sched)
    assert rep.passed
    assert rep.metrics.total_cost == r.total_cost
    assert rep.metrics.total_shortage_slots == r.total_shortage_slots
    assert all(len(a.breaks) in (1, 2) for a in sched.assignments)


def test_infeasible_run_is_saved_without_a_draft(tmp_path, infeasible_input):
    st = Store(tmp_path / "db.sqlite")
    import_workbook(st, infeasible_input)
    r = run_initial(st, 10, workers=WORKERS).run
    assert r.status == "INFEASIBLE"
    assert r.schedule_version is None
    assert r.total_cost is None and r.total_shortage_slots is None and r.first_feasible_seconds is None
    assert r.validation_passed is None
    assert any("needs at least 4 shifts" in d for d in r.diagnostics)
    assert [x.run_id for x in st.list_runs()] == ["RUN-0001"]


def test_schedule_rejected_by_the_checker_is_never_a_draft(store, monkeypatch, small_output):
    """Simulate an optimizer bug: the engine returns a schedule that breaks a rule."""
    broken = __import__("shiftopt.ingest.schedule_reader", fromlist=["read_schedule"]).read_schedule(small_output)
    broken.assignments.pop()  # E010 now has 3 shifts

    def fake_solve(data, budget, **kw):
        return SolveResult(status="OPTIMAL", schedule=broken, time_budget_seconds=budget, first_feasible_seconds=0.1,
                           optimization_seconds=0.1, total_shortage_slots=0, total_cost=11540.0,
                           coverage_proven=True, cost_proven=True, termination_reason="ALL_OBJECTIVES_PROVEN")

    monkeypatch.setattr("shiftopt.service.runs.solve", fake_solve)
    out = run_initial(store, 5)
    assert out.run.status == "ERROR" and out.run.termination_reason == "VALIDATION_FAILED"
    assert out.run.validation_passed is False
    assert out.run.schedule_version is None
    assert any("TOO_FEW_SHIFTS" in v for v in out.violations)


def test_no_input_yet(tmp_path):
    with pytest.raises(NoActiveInput):
        run_initial(Store(tmp_path / "db.sqlite"), 5)


def test_input_is_stored_in_tables(store, small_input):
    """SCRUM-22: employees, demand, templates and break rules have their own tables."""
    from sqlalchemy import inspect
    tables = set(inspect(store.engine).get_table_names())
    assert {"employees", "demand_hours", "shift_templates", "break_rules", "runs", "schedule_versions",
            "assignments"} <= tables
    loaded = store.load_input()
    assert loaded.employees == read_input(small_input).data.employees
