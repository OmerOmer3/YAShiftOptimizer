"""SCRUM-40: report a full-day absence (spec sections 3, 4 and 8).

  * the absence is recorded (database and change log) against the active schedule; the
    active schedule itself does not change;
  * a day without a shift for that employee returns a message and changes nothing;
  * a shift that has started cannot be reported absent;
  * the repair starting point removes the absent shift even if it is locked, makes the
    employee exempt from the 4-shift minimum that week only, and keeps every other shift;
    started, completed and locked shifts are listed as fixed;
  * a repair draft may not give the absent employee a shift that day; approving a repair
    that removes the shift resolves the absence;
  * the chat tool and the command line resolve the employee and the date.
"""
from __future__ import annotations

import datetime as dt

import pytest

from shiftopt import clock
from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.absence import AbsenceRefused, absent_shifts_kept, repair_start, report_absence
from shiftopt.service.approve import ApprovalRefused
from shiftopt.service.locks import set_lock
from shiftopt.validator import validate
from tests.test_locks import _save_repair
from tests.test_mcp_server import call

MONDAY_7AM = dt.datetime(2026, 11, 2, 7, 0)
WEDNESDAY = dt.date(2026, 11, 4)


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(MONDAY_7AM)
    yield
    clock.set_now(None)


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    approve_draft(s, run_initial(s, 10, workers=2, seed=0).run.schedule_version)
    return s


def shift_on(store, day, employee=None):
    sched = store.load_schedule(store.active_schedule_version())
    return next(a for a in sched.assignments if a.date == day and (employee is None or a.employee_id == employee))


def free_day(store, employee):
    sched = store.load_schedule(store.active_schedule_version())
    working = {a.date for a in sched.assignments if a.employee_id == employee}
    return next(d for d in (dt.date(2026, 11, 2) + dt.timedelta(days=i) for i in range(5)) if d not in working)


# ------------------------------------------------------------------ reporting
def test_report_records_the_absence_and_changes_no_schedule(store):
    a = shift_on(store, WEDNESDAY)
    active = store.active_schedule_version()
    before = store.load_schedule(active).assignments
    out = report_absence(store, a.employee_id, WEDNESDAY)
    assert out.created and out.absence_id == "AB-0001" and out.shift_id == a.shift_id
    assert "has not changed" in out.message
    assert store.active_schedule_version() == active and store.load_schedule(active).assignments == before
    ab = store.list_absences()[0]
    assert (ab["employee_id"], ab["date"], ab["status"], ab["reported_on_version"]) == \
        (a.employee_id, WEDNESDAY, "OPEN", active)
    entry = store.list_audit(limit=1)[0]
    assert entry["action"] == "ABSENCE_REPORTED"
    assert entry["details"]["absence_id"] == "AB-0001" and entry["details"]["employee_id"] == a.employee_id


def test_day_without_a_shift_changes_nothing(store):
    day = free_day(store, "E001")
    out = report_absence(store, "E001", day)
    assert out.absence_id is None and not out.created and "nothing was changed" in out.message
    assert store.list_absences() == [] and len(store.list_audit()) == 1          # only the approval


def test_started_shift_cannot_be_reported(store):
    sunday = shift_on(store, dt.date(2026, 11, 1))
    with pytest.raises(AbsenceRefused) as ei:
        report_absence(store, sunday.employee_id, sunday.date)
    assert ei.value.code == "SHIFT_STARTED"
    assert store.list_absences() == []


def test_repeated_report_is_not_recorded_twice(store):
    a = shift_on(store, WEDNESDAY)
    report_absence(store, a.employee_id, WEDNESDAY)
    again = report_absence(store, a.employee_id, WEDNESDAY)
    assert (again.absence_id, again.created) == ("AB-0001", False) and "already reported" in again.message
    assert len(store.list_absences()) == 1


def test_no_active_schedule(tmp_path, small_input):
    s = Store(str(tmp_path / "x.sqlite"))
    import_workbook(s, small_input)
    with pytest.raises(AbsenceRefused) as ei:
        report_absence(s, "E001", WEDNESDAY)
    assert ei.value.code == "NOT_FOUND"


# ------------------------------------------------------------------ repair starting point
def test_starting_point_removes_the_shift_even_if_locked(store, small_input):
    a = shift_on(store, WEDNESDAY)
    set_lock(store, a.employee_id, WEDNESDAY, True)
    out = report_absence(store, a.employee_id, WEDNESDAY)
    assert out.was_locked and "still removed" in out.message
    start = repair_start(store)
    active = store.load_schedule(store.active_schedule_version())
    assert [(r.employee_id, r.date) for r in start.removed] == [(a.employee_id, WEDNESDAY)]
    assert len(start.schedule.assignments) == len(active.assignments) - 1
    assert (a.employee_id, WEDNESDAY) in start.blocked
    assert start.schedule.min_shifts_exempt == {a.employee_id}
    assert all((x.employee_id, x.date) != (a.employee_id, WEDNESDAY) for x in start.fixed)
    # Every other shift is kept exactly; the result passes the independent checker (only shortage may rise).
    others = [x for x in active.assignments if (x.employee_id, x.date) != (a.employee_id, WEDNESDAY)]
    assert start.schedule.assignments == others
    report = validate(store.load_input(), start.schedule, group_size=2)
    assert report.passed, report.violations


def test_exemption_is_only_for_the_absent_employee(store):
    a = shift_on(store, WEDNESDAY)
    report_absence(store, a.employee_id, WEDNESDAY)
    start = repair_start(store)
    data = store.load_input()
    # Taking another employee below 4 shifts breaks their minimum: the checker says so.
    other = next(x.employee_id for x in start.schedule.assignments if x.employee_id != a.employee_id)
    theirs = [x for x in start.schedule.assignments if x.employee_id == other]
    for x in theirs[: len(theirs) - 3]:
        start.schedule.assignments.remove(x)
    report = validate(data, start.schedule, group_size=2)
    assert not report.passed and all(v.employee_id == other for v in report.violations)
    # The absent employee, at 3 or fewer shifts, is not reported.
    assert sum(1 for x in start.schedule.assignments if x.employee_id == a.employee_id) <= 4


def test_fixed_shifts_are_started_or_locked(store):
    a = shift_on(store, WEDNESDAY)
    locked = shift_on(store, dt.date(2026, 11, 5))
    set_lock(store, locked.employee_id, locked.date, True)
    report_absence(store, a.employee_id, WEDNESDAY)
    start = repair_start(store, now=dt.datetime(2026, 11, 3, 12, 0))            # Tuesday noon
    fixed = {(x.employee_id, x.date) for x in start.fixed}
    assert (locked.employee_id, locked.date) in fixed
    assert all(x.date <= dt.date(2026, 11, 3) or x.is_locked for x in start.fixed)
    assert all((x.employee_id, x.date) in fixed for x in start.schedule.assignments if x.date <= dt.date(2026, 11, 2))


def test_two_open_absences_are_repaired_together(store):
    a = shift_on(store, WEDNESDAY)
    b = next(x for x in store.load_schedule(store.active_schedule_version()).assignments
             if x.date == dt.date(2026, 11, 5) and x.employee_id != a.employee_id)
    report_absence(store, a.employee_id, WEDNESDAY)
    report_absence(store, b.employee_id, b.date)
    start = repair_start(store)
    assert len(start.removed) == 2 and start.schedule.min_shifts_exempt == {a.employee_id, b.employee_id}


def test_no_open_absence_to_repair(store):
    with pytest.raises(AbsenceRefused) as ei:
        repair_start(store)
    assert ei.value.code == "NOT_FOUND"


# ------------------------------------------------------------------ approval and export
def test_repair_that_keeps_the_absent_shift_is_refused(store):
    a = shift_on(store, WEDNESDAY)
    base = store.active_schedule_version()
    report_absence(store, a.employee_id, WEDNESDAY)
    bad = _save_repair(store, base, store.load_schedule(base))                  # absent shift still there
    assert absent_shifts_kept(store, store.active_input_version(), store.load_schedule(bad))
    with pytest.raises(ApprovalRefused) as ei:
        approve_draft(store, bad)
    assert ei.value.code == "VALIDATION_FAILED" and "reported absent" in ei.value.details[0]


def test_approving_the_repair_resolves_the_absence(store, tmp_path):
    a = shift_on(store, WEDNESDAY)
    base = store.active_schedule_version()
    report_absence(store, a.employee_id, WEDNESDAY)
    draft = _save_repair(store, base, repair_start(store).schedule)
    assert store.list_absences()[0]["status"] == "OPEN"
    approve_draft(store, draft)
    ab = store.list_absences()[0]
    assert (ab["status"], ab["resolved_by_version"]) == ("RESOLVED", draft)
    # The export of the repair version marks the exemption (spec section 8).
    import openpyxl
    ws = openpyxl.load_workbook(export_run(store, draft, tmp_path / "r.xlsx"))["EmployeeSummary"]
    header = [c.value for c in ws[1]]
    exempt = {r[header.index("EmployeeID")]: r[header.index("MinShiftsExempt")]
              for r in ws.iter_rows(min_row=2, values_only=True)}
    assert exempt[a.employee_id] is True and sum(1 for v in exempt.values() if v) == 1


def test_initial_draft_does_not_resolve_an_absence(store):
    a = shift_on(store, WEDNESDAY)
    report_absence(store, a.employee_id, WEDNESDAY)
    other = run_initial(store, 10, workers=2, seed=1).run.schedule_version
    if absent_shifts_kept(store, store.active_input_version(), store.load_schedule(other)):
        with pytest.raises(ApprovalRefused) as ei:                  # it plans the absent employee that day
            approve_draft(store, other)
        assert ei.value.code == "VALIDATION_FAILED"
    else:
        approve_draft(store, other)
    assert store.list_absences()[0]["status"] == "OPEN"             # only a repair resolves it


# ------------------------------------------------------------------ chat tool and command line
def test_tool_reports_an_absence_with_names(store, tmp_path):
    a = shift_on(store, WEDNESDAY)
    srv = build_server(store, export_dir=tmp_path)
    r = call(srv, "report_absence", {"employee": a.employee_id, "date": "רביעי"}).structured_content
    assert r["ok"] and r["created"] and r["absence_id"] == "AB-0001" and r["weekday"] == "Wednesday"
    assert r["min_shifts_exempt"] is True and r["shift_id"] == a.shift_id
    s = call(srv, "get_system_status", {}).structured_content
    assert s["open_absences"] == [f"AB-0001 {a.employee_id} 2026-11-04"]
    log = call(srv, "list_changes", {"limit": 1}).structured_content["entries"][0]
    assert (log["action"], log["absence_id"]) == ("ABSENCE_REPORTED", "AB-0001")


def test_tool_no_shift_started_and_unknown(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    day = free_day(store, "E001")
    r = call(srv, "report_absence", {"employee": "E001", "date": day.isoformat()}).structured_content
    assert r["ok"] and r["absence_id"] is None and "nothing was changed" in r["message"]
    sunday = shift_on(store, dt.date(2026, 11, 1))
    r = call(srv, "report_absence", {"employee": sunday.employee_id, "date": "Sunday"}).structured_content
    assert r["error"]["code"] == "SHIFT_STARTED"
    r = call(srv, "report_absence", {"employee": "E001", "date": "Saturday"}).structured_content
    assert not r["ok"] and r["error"]["candidates"]


def test_cli_absence_and_list(store, capsys):
    a = shift_on(store, WEDNESDAY)
    db = str(store.engine.url.database)
    clock.set_now(None)
    assert main(["--db", db, "--now", "2026-11-02T07:00", "absence", a.employee_id, "Wednesday"]) == 0
    assert "Absence AB-0001 recorded" in capsys.readouterr().out
    assert main(["--db", db, "absences"]) == 0
    out = capsys.readouterr().out
    assert "AB-0001" in out and "OPEN" in out
    assert main(["--db", db, "--now", "2026-11-05T07:00", "absence", a.employee_id, "Wednesday"]) == 1
    assert "SHIFT_STARTED" in capsys.readouterr().out
