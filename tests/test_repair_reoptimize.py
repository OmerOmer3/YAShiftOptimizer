"""SCRUM-42: re-optimize future shifts when no single replacement exists (spec sections 3, 8, 9).

  * runs only when no single replacement exists, with the rest of the same time budget;
  * objectives in order without weights: shortage, then changed (employee, date) pairs,
    then cost; StabilityOptimalityProven reports the changes stage;
  * started, completed and locked shifts are fixed with their breaks, and no shift is added
    on a template that has already started;
  * a change is an (employee, date) pair whose template changed, was added or was removed;
    a swap on one day counts once, removal plus addition count as two, break-only moves not;
  * no solution keeps the active schedule, and the absence stays open.
"""
from __future__ import annotations

import datetime as dt
import types

import openpyxl
import pytest

from shiftopt import clock
from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.domain import Assignment, Break, Schedule
from shiftopt.engine.repair import RepairResult
from shiftopt.export import export_run
from shiftopt.export.service import changed_assignments
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.absence import report_absence
from shiftopt.service.locks import set_lock
from shiftopt.service.repair import run_repair
from shiftopt.validator import validate
from tests.builders import WEEK, flat_demand, h, make_input
from tests.test_mcp_server import call

MONDAY_7AM = dt.datetime(2026, 11, 2, 7, 0)
MON, WED, THU = (WEEK + dt.timedelta(days=i) for i in (1, 3, 4))


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(MONDAY_7AM)
    yield
    clock.set_now(None)


def shift(eid, day, start=h(8), hours=4, cost=100.0) -> Assignment:
    return Assignment(WEEK + dt.timedelta(days=day - 1), eid, f"S{day}", start, start + hours * 60,
                      (Break(start + 90, start + 120),), float(hours), cost)


def approved(store, data, schedule) -> str:
    data.source_file = "hand_built.xlsx"
    iv = store.save_input_version(data)
    assert validate(data, schedule, group_size=2).passed
    result = types.SimpleNamespace(time_budget_seconds=1, status="FEASIBLE", termination_reason="HAND_BUILT",
                                   first_feasible_seconds=None, total_shortage_slots=None, total_cost=None,
                                   coverage_proven=None, cost_proven=None, solver_info={}, diagnostics=[])
    _, v = store.save_run(input_version=iv, run_type="INITIAL", group_size=2, result=result, validation_passed=True,
                          optimization_seconds=0.0, started_at=dt.datetime.now(), schedule=schedule)
    approve_draft(store, v)
    return v


@pytest.fixture
def sample(tmp_path, small_input):
    """Input_Small_10, approved, and an absence on Wednesday: no single replacement exists there."""
    s = Store(str(tmp_path / "s.sqlite"))
    import_workbook(s, small_input)
    approve_draft(s, run_initial(s, 10, workers=2, seed=0).run.schedule_version)
    return s


def wednesday_shift(store, skip=()):
    return next(a for a in store.load_schedule(store.active_schedule_version()).assignments
                if a.date == WED and a.employee_id not in skip)


# ------------------------------------------------------------------ a week with a known answer
def test_fewest_changes_after_least_shortage(tmp_path):
    """Demand 1 employee 08:00-12:00 every day; one employee alone leaves his break uncovered.
    A works Sun-Wed, B works Tue-Fri. A is absent on Wednesday; B already works Wednesday,
    so there is no single replacement.
    Least shortage first: a second employee covers the other's break wherever possible on a
    future day -- B adds Monday, and A (absent on Wednesday only, now at 3 shifts) adds
    Thursday and Friday. Sunday has started; Wednesday cannot be improved. Shortage comes
    before the number of changes, so these additions are made; nothing else changes:
    A-Wednesday removed, B-Monday, A-Thursday, A-Friday added = 4 changes."""
    store = Store(str(tmp_path / "db.sqlite"))
    data = make_input(employees=[("A", ()), ("B", ())], demand=flat_demand(range(1, 7), 8, 12, 1),
                      templates=[(f"S{d}", d, h(8), 4, 100.0) for d in range(1, 7)])
    base = approved(store, data, Schedule([shift("A", d) for d in (1, 2, 3, 4)] +
                                          [shift("B", d) for d in (3, 4, 5, 6)]))
    report_absence(store, "A", WED)
    out = run_repair(store, 10, workers=2)
    run = out.run
    assert out.mode == "REOPTIMIZED" and run.status == "OPTIMAL"
    assert run.coverage_proven and run.stability_proven and run.cost_proven
    draft = store.load_schedule(run.schedule_version)
    old = {(a.employee_id, a.date): a.shift_id for a in store.load_schedule(base).assignments}
    new = {(a.employee_id, a.date): a.shift_id for a in draft.assignments}
    fri = WEEK + dt.timedelta(days=5)
    assert {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)} == \
        {("A", WED), ("B", MON), ("A", THU), ("A", fri)}
    assert out.changed_assignments == changed_assignments(store.load_schedule(base), draft) == 4
    # Shortage: only the uncovered breaks of Sunday (A alone, started) and Wednesday (B alone).
    assert run.total_shortage_slots == 2
    assert draft.min_shifts_exempt == {"A"} and run.validation_passed


# ------------------------------------------------------------------ the course sample
def test_sample_repair_is_valid_and_proven(sample):
    base = sample.active_schedule_version()
    a = wednesday_shift(sample)
    report_absence(sample, a.employee_id, WED)
    out = run_repair(sample, 20, workers=2)
    run = out.run
    assert out.mode == "REOPTIMIZED" and run.run_type == "REPAIR" and run.base_schedule_version == base
    assert run.validation_passed and run.termination_reason.endswith("REOPTIMIZED")
    assert run.time_budget_seconds == 20 and run.optimization_seconds <= 20 + 1
    draft = sample.load_schedule(run.schedule_version)
    assert all((x.employee_id, x.date) != (a.employee_id, WED) for x in draft.assignments)
    assert out.changed_assignments == changed_assignments(sample.load_schedule(base), draft) >= 2
    report = validate(sample.load_input(), draft, group_size=2)
    assert report.passed and report.metrics.total_shortage_slots == run.total_shortage_slots
    assert sample.active_schedule_version() == base


def test_started_and_locked_shifts_are_fixed_and_the_past_gets_no_new_shift(sample):
    base = sample.active_schedule_version()
    a = wednesday_shift(sample)
    locked = next(x for x in sample.load_schedule(base).assignments if x.date == THU)
    set_lock(sample, locked.employee_id, THU, True)
    report_absence(sample, a.employee_id, WED, now=dt.datetime(2026, 11, 3, 7, 0))
    clock.set_now(dt.datetime(2026, 11, 3, 7, 0))                   # Tuesday 07:00: Sunday, Monday are past
    draft = sample.load_schedule(run_repair(sample, 20, workers=2).run.schedule_version)
    old = {(x.employee_id, x.date): x for x in sample.load_schedule(base).assignments}
    new = {(x.employee_id, x.date): x for x in draft.assignments}
    past = {k for k in old.keys() | new.keys() if k[1] < dt.date(2026, 11, 3)}
    for k in past | {(locked.employee_id, THU)}:
        assert (new[k].shift_id, new[k].breaks) == (old[k].shift_id, old[k].breaks)
    assert new[(locked.employee_id, THU)].is_locked


def test_approving_resolves_the_absence_and_export_has_the_repair_fields(sample, tmp_path):
    a = wednesday_shift(sample)
    report_absence(sample, a.employee_id, WED)
    out = run_repair(sample, 20, workers=2)
    ws = openpyxl.load_workbook(export_run(sample, out.run.run_id, tmp_path / "r.xlsx"))["RunSummary"]
    summary = {r[0]: r[1] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert summary["RunType"] == "REPAIR" and summary["ChangedAssignments"] == out.changed_assignments
    assert summary["StabilityOptimalityProven"] is out.run.stability_proven
    approve_draft(sample, out.run.schedule_version)
    assert sample.list_absences()[0]["status"] == "RESOLVED"


# ------------------------------------------------------------------ change counting (spec section 3)
def test_change_counting_rules():
    base = Schedule([shift("A", 4), shift("B", 5), shift("C", 2)])
    swapped = Assignment(THU, "B", "S5b", h(10), h(14), (Break(h(11, 30), h(12)),), 4.0, 100.0)
    moved_break = Assignment(MON, "C", "S2", h(8), h(12), (Break(h(10), h(10, 30)),), 4.0, 100.0)
    new = Schedule([swapped, moved_break, shift("D", 4)])
    # A removed + D added = 2; B swapped on the same day = 1; C break-only move = 0.
    assert changed_assignments(base, new) == 3


# ------------------------------------------------------------------ no repair
@pytest.mark.parametrize("status", ["INFEASIBLE", "NO_SOLUTION_FOUND"])
def test_no_repair_keeps_the_active_schedule_and_the_absence_open(sample, monkeypatch, status):
    base = sample.active_schedule_version()
    a = wednesday_shift(sample)
    report_absence(sample, a.employee_id, WED)

    def nothing(*args, **kw):
        return RepairResult(status=status, schedule=None, time_budget_seconds=kw.get("time_budget_s", 1),
                            first_feasible_seconds=None, optimization_seconds=0.1, total_shortage_slots=None,
                            total_cost=None, coverage_proven=None, cost_proven=None, stability_proven=None,
                            termination_reason="TEST", diagnostics=["no repair in this test"])
    monkeypatch.setattr("shiftopt.service.repair.reoptimize", nothing)
    out = run_repair(sample, 5, workers=2)
    assert out.run.status == status and out.run.schedule_version is None
    assert "no approved answer yet" in out.message
    assert sample.active_schedule_version() == base and sample.list_absences()[0]["status"] == "OPEN"


# ------------------------------------------------------------------ tool and command line
def test_tool_and_cli_report_the_reoptimized_repair(sample, tmp_path, capsys):
    a = wednesday_shift(sample)
    srv = build_server(sample, export_dir=tmp_path)
    call(srv, "report_absence", {"employee": a.employee_id, "date": "Wednesday"})
    r = call(srv, "repair_schedule", {"time_budget_seconds": 20, "workers": 2}).structured_content
    assert r["ok"] and r["mode"] == "REOPTIMIZED" and r["changed_assignments"] >= 2 and r["replacements"] == []
    assert r["run"]["run_type"] == "REPAIR"
    db = str(sample.engine.url.database)
    assert main(["--db", db, "repair", "--time-budget", "20", "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "ChangedAssignments" in out and "re-optimized" in out
