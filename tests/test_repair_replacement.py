"""SCRUM-41: find a single replacement (spec sections 3, 8 and 9).

A hand-built week makes the right answer known in advance:

    templates: S1..S6, one per day Sunday..Friday, 08:00-12:00 (4 h, one break), cost 100
    demand:    2 employees 08:00-12:00 every day
    A  works Sun Mon Tue Wed          B  works Sun Mon Thu Fri
    C  works Tue Wed Thu Fri          D  works Sun Mon Tue Thu   (D is unavailable on Wednesday)

A is reported absent on Wednesday. Free on Wednesday: B (4 shifts, may take a 5th) and D
(unavailable). So the only valid single replacement is B. Variants make B unusable
(already working Wednesday, or already at 5 shifts) so that none exists.

"Now" is Monday 07:00, so Sunday's shifts have started and must stay exactly as they are.
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

WED = WEEK + dt.timedelta(days=3)
THU = WEEK + dt.timedelta(days=4)
WEEKDAYS = {"A": (1, 2, 3, 4), "B": (1, 2, 5, 6), "C": (3, 4, 5, 6), "D": (1, 2, 3, 5)}


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(dt.datetime(2026, 11, 2, 7, 0))
    yield
    clock.set_now(None)


def week_input():
    return make_input(employees=[("A", ()), ("B", ()), ("C", ()), ("D", (4,))],
                      demand=flat_demand(range(1, 7), 8, 12, 2),
                      templates=[(f"S{d}", d, h(8), 4, 100.0) for d in range(1, 7)])


def shift(eid, day) -> Assignment:
    return Assignment(WEEK + dt.timedelta(days=day - 1), eid, f"S{day}", h(8), h(12), (Break(h(9, 30), h(10)),),
                      4.0, 100.0)


def make_store(tmp_path, days=WEEKDAYS) -> Store:
    store = Store(str(tmp_path / "db.sqlite"))
    data = week_input()
    data.source_file = "hand_built.xlsx"
    iv = store.save_input_version(data)
    schedule = Schedule([shift(e, d) for e, ds in days.items() for d in ds])
    assert validate(data, schedule, group_size=2).passed
    result = types.SimpleNamespace(time_budget_seconds=1, status="FEASIBLE", termination_reason="HAND_BUILT",
                                   first_feasible_seconds=None, total_shortage_slots=None, total_cost=None,
                                   coverage_proven=None, cost_proven=None, solver_info={}, diagnostics=[])
    _, v = store.save_run(input_version=iv, run_type="INITIAL", group_size=2, result=result, validation_passed=True,
                          optimization_seconds=0.0, started_at=dt.datetime.now(), schedule=schedule)
    approve_draft(store, v)
    return store


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


# ------------------------------------------------------------------ the replacement
def test_the_only_valid_candidate_replaces_the_absent_employee(store):
    base = store.active_schedule_version()
    before = store.load_schedule(base)
    report_absence(store, "A", WED)
    out = run_repair(store, 10, workers=2)
    run = out.run
    assert (run.status, run.run_type, run.base_schedule_version) == ("OPTIMAL", "REPAIR", base)
    assert run.validation_passed and run.coverage_proven and run.cost_proven and run.stability_proven
    assert "single-replacement scope" in run.diagnostics[0]
    assert [(r.absent_employee, r.date, r.shift_id, r.replacement_employee) for r in out.replacements] == \
        [("A", WED, "S4", "B")]
    draft = store.load_schedule(run.schedule_version)
    pairs = {(a.employee_id, a.date): a.shift_id for a in draft.assignments}
    assert ("A", WED) not in pairs and pairs[("B", WED)] == "S4"
    assert changed_assignments(before, draft) == 2                  # removal + addition (spec section 3)
    assert draft.min_shifts_exempt == {"A"}
    assert sum(1 for a in draft.assignments if a.employee_id == "B") == 5
    assert run.total_cost == before_cost(before)                    # same template, same cost
    assert store.active_schedule_version() == base                  # nothing active changed
    assert "B takes S4 on 2026-11-04 instead of A" in out.message


def before_cost(schedule) -> float:
    return round(sum(a.cost for a in schedule.assignments), 2)


@pytest.mark.parametrize("variant", [
    {**WEEKDAYS, "B": (1, 2, 4, 5)},                   # B already works on Wednesday
    {**WEEKDAYS, "B": (1, 2, 3, 5, 6)},                # B already has 5 shifts
])
def test_no_single_replacement_changes_nothing(tmp_path, variant):
    store = make_store(tmp_path, variant)
    base = store.active_schedule_version()
    runs_before = len(store.list_runs())
    report_absence(store, "A", WED)
    out = run_repair(store, 10, workers=2)
    assert out.run is None and "No single replacement" in out.message
    assert len(store.list_runs()) == runs_before                    # nothing stored
    assert store.active_schedule_version() == base
    assert store.list_absences()[0]["status"] == "OPEN"


def test_started_and_locked_shifts_stay_exactly_as_they_are(store):
    base = store.active_schedule_version()
    set_lock(store, "C", THU, True)
    report_absence(store, "A", WED)
    draft = store.load_schedule(run_repair(store, 10, workers=2).run.schedule_version)
    old = {(a.employee_id, a.date): a for a in store.load_schedule(base).assignments}
    new = {(a.employee_id, a.date): a for a in draft.assignments}
    for key in [k for k in old if k[1] == WEEK] + [("C", THU)]:     # Sunday (started) and the locked shift
        assert (new[key].shift_id, new[key].breaks) == (old[key].shift_id, old[key].breaks)
    assert new[("C", THU)].is_locked and not new[("B", WED)].is_locked


def test_approving_the_repair_resolves_the_absence(store):
    report_absence(store, "A", WED)
    v = run_repair(store, 10, workers=2).run.schedule_version
    assert approve_draft(store, v).schedule_version == v
    ab = store.list_absences()[0]
    assert (ab["status"], ab["resolved_by_version"]) == ("RESOLVED", v)


def test_nothing_to_repair(store):
    from shiftopt.service.absence import AbsenceRefused
    with pytest.raises(AbsenceRefused):
        run_repair(store, 5, workers=2)


def test_course_sample_has_no_single_replacement_on_wednesday(tmp_path, small_input):
    """In Input_Small_10 the employees free on a day are exactly those unavailable that day,
    so no absence there has a single replacement; the broader repair is SCRUM-42."""
    s = Store(str(tmp_path / "s.sqlite"))
    import_workbook(s, small_input)
    approve_draft(s, run_initial(s, 10, workers=2, seed=0).run.schedule_version)
    a = next(x for x in s.load_schedule(s.active_schedule_version()).assignments if x.date == dt.date(2026, 11, 4))
    report_absence(s, a.employee_id, a.date)
    assert run_repair(s, 10, workers=2).run is None


# ------------------------------------------------------------------ export, tool, command line
def test_export_of_the_repair_version(store, tmp_path):
    report_absence(store, "A", WED)
    run = run_repair(store, 10, workers=2).run
    ws = openpyxl.load_workbook(export_run(store, run.run_id, tmp_path / "r.xlsx"))["RunSummary"]
    summary = {r[0]: r[1] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert summary["RunType"] == "REPAIR" and summary["BaseScheduleVersion"] == run.base_schedule_version
    assert summary["ChangedAssignments"] == 2 and summary["StabilityOptimalityProven"] is True


def test_tool_repair_schedule(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    r = call(srv, "repair_schedule", {"time_budget_seconds": 10, "workers": 2}).structured_content
    assert not r["ok"] and r["error"]["code"] == "NOT_FOUND"          # nothing reported yet
    call(srv, "report_absence", {"employee": "A", "date": "Wednesday"})
    r = call(srv, "repair_schedule", {"time_budget_seconds": 10, "workers": 2}).structured_content
    assert r["ok"] and r["run"]["run_type"] == "REPAIR" and r["run"]["stability_proven"] is True
    assert r["replacements"] == [{"absent_employee": "A", "date": "2026-11-04", "shift_id": "S4",
                                  "replacement_employee": "B", "replacement_name": "B"}]
    assert r["open_absences"] == ["AB-0001 A 2026-11-04"]
    assert r["active_schedule_version"] == "SV-0001"


def test_cli_repair(store, capsys):
    db = str(store.engine.url.database)
    clock.set_now(None)
    assert main(["--db", db, "--now", "2026-11-02T07:00", "absence", "A", "Wednesday"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "--now", "2026-11-02T07:00", "repair", "--time-budget", "10", "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "RunType                   REPAIR" in out and "StabilityOptimalityProven TRUE" in out
    assert "B takes S4" in out


# ------------------------------------------------------------------ database created by an earlier version
def test_an_older_database_gets_the_new_column(tmp_path):
    path = tmp_path / "old.sqlite"
    s = Store(str(path))
    with s.engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN stability_proven")
    s2 = Store(str(path))
    with s2.engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(runs)")}
    assert "stability_proven" in cols


def test_two_absences_on_the_same_shift_get_two_different_replacements(tmp_path):
    """A and C both work Wednesday; both are absent. B and E are free on Wednesday."""
    days = {**WEEKDAYS, "E": (1, 2, 3, 5)}
    store = Store(str(tmp_path / "db.sqlite"))
    data = make_input(employees=[("A", ()), ("B", ()), ("C", ()), ("D", (4,)), ("E", ())],
                      demand=flat_demand(range(1, 7), 8, 12, 2),
                      templates=[(f"S{d}", d, h(8), 4, 100.0) for d in range(1, 7)])
    iv = store.save_input_version(data)
    schedule = Schedule([shift(e, d) for e, ds in days.items() for d in ds])
    result = types.SimpleNamespace(time_budget_seconds=1, status="FEASIBLE", termination_reason="HAND_BUILT",
                                   first_feasible_seconds=None, total_shortage_slots=None, total_cost=None,
                                   coverage_proven=None, cost_proven=None, solver_info={}, diagnostics=[])
    _, v = store.save_run(input_version=iv, run_type="INITIAL", group_size=2, result=result, validation_passed=True,
                          optimization_seconds=0.0, started_at=dt.datetime.now(), schedule=schedule)
    approve_draft(store, v)
    report_absence(store, "A", WED)
    report_absence(store, "C", WED)
    out = run_repair(store, 10, workers=2)
    assert sorted(r.replacement_employee for r in out.replacements) == ["B", "E"]
    draft = store.load_schedule(out.run.schedule_version)
    assert sorted(a.employee_id for a in draft.assignments if a.date == WED) == ["B", "E"]
    assert changed_assignments(store.load_schedule(v), draft) == 4
