"""SCRUM-39: lock and unlock a future shift (spec sections 3, 4, 7 and 10).

  * locks belong to shifts of the active schedule; only a shift that has not started can
    be locked or unlocked (started and completed shifts are kept anyway);
  * every lock and unlock is stored and recorded in the change log; repeating it changes
    nothing more;
  * the exported Schedule sheet shows IsLocked;
  * a repair draft that does not keep a locked shift exactly is refused at approval
    (LOCK_VIOLATED), except the shift of an employee reported absent;
  * the chat tools and the command line resolve the employee and the date.

The sample week is 1-6 November 2026, so these tests fix "now" (shiftopt.clock).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import types

import openpyxl
import pytest

from shiftopt import clock
from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.domain import Break
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.approve import ApprovalRefused, locked_shifts_not_kept
from shiftopt.service.locks import LockRefused, set_lock
from tests.test_mcp_server import call, list_tools
from tests.test_stale_draft import repair_draft

MONDAY_7AM = dt.datetime(2026, 11, 2, 7, 0)   # before every shift from Monday on


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(MONDAY_7AM)
    yield
    clock.set_now(None)


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    v = run_initial(s, 10, workers=2, seed=0).run.schedule_version
    approve_draft(s, v)
    return s


def shift_on(store, day: dt.date):
    """Some shift of the active schedule on `day`."""
    sched = store.load_schedule(store.active_schedule_version())
    return next(a for a in sched.assignments if a.date == day)


def locked_in_active(store, employee_id, day) -> bool:
    sched = store.load_schedule(store.active_schedule_version())
    return next(a for a in sched.assignments if a.employee_id == employee_id and a.date == day).is_locked


# ------------------------------------------------------------------ clock
def test_clock_is_fixed_for_tests_and_parses_iso():
    assert clock.now() == MONDAY_7AM and clock.is_fixed()
    assert clock.parse("2026-11-03 07:30") == dt.datetime(2026, 11, 3, 7, 30)
    assert clock.has_started(dt.date(2026, 11, 2), 8 * 60, dt.datetime(2026, 11, 2, 8, 0))       # at the start
    assert not clock.has_started(dt.date(2026, 11, 2), 8 * 60, dt.datetime(2026, 11, 2, 7, 59))
    with pytest.raises(ValueError):
        clock.parse("next Tuesday")


def test_clock_from_environment(monkeypatch):
    clock.set_now(None)
    monkeypatch.setenv(clock.ENV_VAR, "2026-11-04T12:00")
    assert clock.now() == dt.datetime(2026, 11, 4, 12, 0)


# ------------------------------------------------------------------ service
def test_lock_a_future_shift_and_record_it(store):
    a = shift_on(store, dt.date(2026, 11, 4))
    active = store.active_schedule_version()
    out = set_lock(store, a.employee_id, a.date, True)
    assert (out.is_locked, out.changed, out.schedule_version) == (True, True, active)
    assert locked_in_active(store, a.employee_id, a.date)
    assert store.active_schedule_version() == active          # no new version, no shift changed
    entry = store.list_audit(limit=1)[0]
    assert entry["action"] == "LOCKED" and entry["schedule_version"] == active
    assert entry["details"] == {"employee_id": a.employee_id, "date": "2026-11-04", "shift_id": a.shift_id}


def test_unlock_and_repeats_change_nothing_more(store):
    a = shift_on(store, dt.date(2026, 11, 4))
    set_lock(store, a.employee_id, a.date, True)
    again = set_lock(store, a.employee_id, a.date, True)
    assert again.changed is False and len(store.list_audit()) == 2          # APPROVED + one LOCKED
    out = set_lock(store, a.employee_id, a.date, False)
    assert (out.is_locked, out.changed) == (False, True)
    assert not locked_in_active(store, a.employee_id, a.date)
    assert [e["action"] for e in store.list_audit()][:2] == ["UNLOCKED", "LOCKED"]


def test_started_and_completed_shifts_cannot_be_locked(store):
    sunday = shift_on(store, dt.date(2026, 11, 1))                          # completed before Monday 07:00
    with pytest.raises(LockRefused) as ei:
        set_lock(store, sunday.employee_id, sunday.date, True)
    assert ei.value.code == "SHIFT_STARTED"
    later = shift_on(store, dt.date(2026, 11, 4))
    start = clock.shift_start(later.date, later.start)
    with pytest.raises(LockRefused) as ei:                                  # exactly at its start
        set_lock(store, later.employee_id, later.date, True, now=start)
    assert ei.value.code == "SHIFT_STARTED"
    assert set_lock(store, later.employee_id, later.date, True,
                    now=start - dt.timedelta(minutes=1)).is_locked           # one minute before is fine
    assert len(store.list_audit()) == 2


def test_no_shift_that_day_or_no_active_schedule(store, tmp_path, small_input):
    sched = store.load_schedule(store.active_schedule_version())
    working = {a.date for a in sched.assignments if a.employee_id == "E001"}
    free = next(d for d in (dt.date(2026, 11, 1) + dt.timedelta(days=i) for i in range(6)) if d not in working)
    with pytest.raises(LockRefused) as ei:
        set_lock(store, "E001", free, True)
    assert ei.value.code == "NOT_FOUND"

    empty = Store(str(tmp_path / "empty.sqlite"))
    import_workbook(empty, small_input)
    run_initial(empty, 10, workers=2, seed=0)                               # a draft, nothing approved
    with pytest.raises(LockRefused) as ei:
        set_lock(empty, "E001", dt.date(2026, 11, 4), True)
    assert ei.value.code == "NOT_FOUND"


def test_lock_is_confirmed_against_the_active_schedule_in_the_transaction(store):
    a = shift_on(store, dt.date(2026, 11, 4))
    with pytest.raises(Exception) as ei:
        store.set_lock("SV-0999", a.employee_id, a.date, True)
    assert type(ei.value).__name__ == "StaleState"
    assert not locked_in_active(store, a.employee_id, a.date)


def test_export_shows_is_locked(store, tmp_path):
    a = shift_on(store, dt.date(2026, 11, 4))
    set_lock(store, a.employee_id, a.date, True)
    path = export_run(store, store.active_schedule_version(), tmp_path / "out.xlsx")
    ws = openpyxl.load_workbook(path)["Schedule"]
    header = [c.value for c in ws[1]]
    rows = [dict(zip(header, (c.value for c in r))) for r in ws.iter_rows(min_row=2)]
    flags = {(r["EmployeeID"], r["Date"].date() if isinstance(r["Date"], dt.datetime) else r["Date"]): r["IsLocked"]
             for r in rows}
    assert flags[(a.employee_id, a.date)] is True
    assert sum(1 for v in flags.values() if v) == 1


# ------------------------------------------------------------------ locks are kept by repairs
def test_repair_draft_that_changes_a_locked_shift_is_refused(store):
    a = shift_on(store, dt.date(2026, 11, 4))
    base = store.active_schedule_version()
    set_lock(store, a.employee_id, a.date, True)

    same = repair_draft(store, base)                                        # keeps every shift: approved
    assert store.load_schedule(same) is not None

    changed = store.load_schedule(base)
    for i, x in enumerate(changed.assignments):
        if (x.employee_id, x.date) == (a.employee_id, a.date) and len(x.breaks) >= 1:
            moved = tuple(Break(b.start + 30, b.end + 30) if j == 0 else b for j, b in enumerate(x.breaks))
            changed.assignments[i] = dataclasses.replace(x, breaks=moved)
    bad = _save_repair(store, base, changed)
    with pytest.raises(ApprovalRefused) as ei:
        approve_draft(store, bad)
    assert ei.value.code == "LOCK_VIOLATED"
    assert f"{a.employee_id} on 2026-11-04" in ei.value.details[0]
    assert store.active_schedule_version() == base

    assert approve_draft(store, same).schedule_version == same


def test_locked_shift_of_an_absent_employee_may_be_removed(store):
    a = shift_on(store, dt.date(2026, 11, 4))
    base = store.active_schedule_version()
    set_lock(store, a.employee_id, a.date, True)
    sched = store.load_schedule(base)
    sched.assignments = [x for x in sched.assignments if (x.employee_id, x.date) != (a.employee_id, a.date)]
    sched.min_shifts_exempt = {a.employee_id}
    assert locked_shifts_not_kept(store.load_schedule(base), sched) == []
    sched.min_shifts_exempt = set()
    assert "removed" in locked_shifts_not_kept(store.load_schedule(base), sched)[0]


def _save_repair(store, base, schedule) -> str:
    """A REPAIR draft with the given assignments, stored with base_version=base."""
    run = store.get_run(store.schedule_version_info(base)["run_id"])
    result = types.SimpleNamespace(time_budget_seconds=1, status="FEASIBLE", termination_reason="TEST",
                                   first_feasible_seconds=0.1, total_shortage_slots=run.total_shortage_slots,
                                   total_cost=run.total_cost, coverage_proven=None, cost_proven=None,
                                   solver_info={}, diagnostics=[])
    _, v = store.save_run(input_version=store.active_input_version(), run_type="REPAIR", group_size=2,
                          result=result, validation_passed=True, optimization_seconds=0.1,
                          started_at=dt.datetime.now(), schedule=schedule, base_version=base)
    return v


# ------------------------------------------------------------------ chat tools and command line
def test_tools_lock_and_unlock_with_names_and_day_names(store, tmp_path):
    a = shift_on(store, dt.date(2026, 11, 4))
    srv = build_server(store, export_dir=tmp_path)
    r = call(srv, "lock_shift", {"employee": a.employee_id, "date": "Wednesday"}).structured_content
    assert r["ok"] and r["is_locked"] and r["changed"] and r["date"] == "2026-11-04"
    assert r["weekday"] == "Wednesday" and r["shift_id"] == a.shift_id and r["now"] == "2026-11-02T07:00"
    log = call(srv, "list_changes", {"limit": 1}).structured_content["entries"][0]
    assert (log["action"], log["employee_id"], log["date"]) == ("LOCKED", a.employee_id, "2026-11-04")
    r = call(srv, "unlock_shift", {"employee": a.employee_id, "date": "רביעי"}).structured_content
    assert r["ok"] and r["is_locked"] is False
    assert call(srv, "get_system_status", {}).structured_content["now"] == "2026-11-02T07:00"


def test_tools_report_started_unknown_and_ambiguous(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    sunday = shift_on(store, dt.date(2026, 11, 1))
    r = call(srv, "lock_shift", {"employee": sunday.employee_id, "date": "2026-11-01"}).structured_content
    assert (r["ok"], r["error"]["code"]) == (False, "SHIFT_STARTED")
    r = call(srv, "lock_shift", {"employee": "E001", "date": "Saturday"}).structured_content
    assert r["error"]["code"] in ("NOT_FOUND", "INVALID_PARAMETER") and r["error"]["candidates"]
    r = call(srv, "lock_shift", {"employee": "nobody", "date": "Wednesday"}).structured_content
    assert r["error"]["code"] in ("NOT_FOUND", "AMBIGUOUS")
    tools = {t.name: t for t in list_tools(srv)}
    assert {"lock_shift", "unlock_shift"} <= tools.keys()
    assert tools["lock_shift"].annotations.destructive_hint is False


def test_cli_lock_unlock_and_log(store, capsys):
    a = shift_on(store, dt.date(2026, 11, 4))
    db = str(store.engine.url.database)
    clock.set_now(None)
    assert main(["--db", db, "--now", "2026-11-02T07:00", "lock", a.employee_id, "2026-11-04"]) == 0
    assert "is locked" in capsys.readouterr().out
    assert not clock.is_fixed()                                              # --now applies to one command
    assert main(["--db", db, "--now", "2026-11-02T07:00", "lock", a.employee_id, "Wednesday"]) == 0
    assert "already locked" in capsys.readouterr().out
    assert main(["--db", db, "log", "--limit", "1"]) == 0
    assert f"LOCKED    {store.active_schedule_version()}" in capsys.readouterr().out
    assert main(["--db", db, "--now", "2026-11-05T07:00", "unlock", a.employee_id, "2026-11-04"]) == 1
    assert "SHIFT_STARTED" in capsys.readouterr().out
