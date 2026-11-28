"""SCRUM-43: show the repair draft and its impact (spec sections 3, 7 and 8).

  * the draft is compared with the schedule it would replace: a repair draft with its base,
    any other draft with the active schedule;
  * changed (employee, date) pairs and break-only moves are listed separately;
    ChangedAssignments counts pairs only;
  * shortage and cost before and after come from the independent checker;
  * the export of a repair version fills the repair fields, and its summaries include the
    past shifts that remain;
  * the active schedule changes only on approval; a repair whose base is no longer active is
    refused (BASE_VERSION_STALE).
"""
from __future__ import annotations

import datetime as dt

import openpyxl
import pytest

from shiftopt import clock
from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.domain import Assignment, Break, Schedule
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.absence import report_absence
from shiftopt.service.approve import ApprovalRefused
from shiftopt.service.compare import compare, diff_schedules
from shiftopt.service.locks import set_lock
from shiftopt.service.repair import run_repair
from shiftopt.service.resolve import Unresolved
from shiftopt.validator.metrics import compute_metrics
from tests.builders import WEEK, h
from tests.test_mcp_server import call
from tests.test_repair_replacement import make_store

WED = WEEK + dt.timedelta(days=3)
THU = WEEK + dt.timedelta(days=4)


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(dt.datetime(2026, 11, 2, 7, 0))
    yield
    clock.set_now(None)


@pytest.fixture
def repaired(tmp_path):
    """The hand-built week of test_repair_replacement: A absent on Wednesday, B replaces."""
    store = make_store(tmp_path)
    report_absence(store, "A", WED)
    return store, run_repair(store, 10, workers=2)


@pytest.fixture
def sample(tmp_path, small_input):
    s = Store(str(tmp_path / "s.sqlite"))
    import_workbook(s, small_input)
    approve_draft(s, run_initial(s, 10, workers=2, seed=0).run.schedule_version)
    return s


# ------------------------------------------------------------------ the diff (spec section 3)
def test_diff_lists_changes_and_break_moves_separately():
    def a(eid, day, sid, start, brk):
        return Assignment(WEEK + dt.timedelta(days=day - 1), eid, sid, start, start + 240,
                          (Break(brk, brk + 30),), 4.0, 100.0)
    before = Schedule([a("A", 4, "S4", h(8), h(9, 30)), a("B", 5, "S5", h(8), h(9, 30)),
                       a("C", 2, "S2", h(8), h(9, 30))])
    after = Schedule([a("B", 5, "S5b", h(10), h(11, 30)), a("C", 2, "S2", h(8), h(10)),
                      a("D", 4, "S4", h(8), h(9, 30))])
    d = diff_schedules(before, after)
    assert [(c.employee_id, c.kind) for c in d.changes] == [("A", "REMOVED"), ("D", "ADDED"),
                                                          ("B", "TEMPLATE_CHANGED")]
    assert d.changes[2].before == "S5 08:00-12:00" and d.changes[2].after == "S5b 10:00-14:00"
    assert [(m.employee_id, m.before, m.after) for m in d.break_moves] == [("C", ["09:30-10:00"], ["10:00-10:30"])]
    assert d.changed_assignments == 3                      # the break move is not counted


# ------------------------------------------------------------------ comparison of a repair draft
def test_repair_draft_compared_with_its_base(repaired):
    store, out = repaired
    base = store.active_schedule_version()
    c = compare(store)                                     # 'the draft': the only draft
    assert (c.draft, c.run_type, c.compared_with, c.compared_with_is_active) == \
        (out.run.schedule_version, "REPAIR", base, True)
    assert [(x.employee_id, x.date, x.kind) for x in c.diff.changes] == [("A", WED, "REMOVED"),
                                                                       ("B", WED, "ADDED")]
    assert c.diff.changed_assignments == 2 == out.changed_assignments
    data = store.load_input()
    mb = compute_metrics(data, store.load_schedule(base))
    ma = compute_metrics(data, store.load_schedule(c.draft))
    assert (c.before.shortage_slots, c.before.cost) == (mb.total_shortage_slots, mb.total_cost)
    assert (c.after.shortage_slots, c.after.cost) == (ma.total_shortage_slots, ma.total_cost)
    assert c.after.cost == c.before.cost                   # same template replaces the absent shift
    assert c.min_shifts_exempt == ["A"] and c.open_absences == ["AB-0001 A 2026-11-04"]
    assert store.active_schedule_version() == base         # comparing changes nothing


def test_reoptimized_draft_on_the_sample(sample, tmp_path):
    a = next(x for x in sample.load_schedule(sample.active_schedule_version()).assignments if x.date == WED)
    report_absence(sample, a.employee_id, WED)
    out = run_repair(sample, 20, workers=2)
    c = compare(sample, out.run.schedule_version)
    assert c.diff.changed_assignments == out.changed_assignments
    assert c.after.shortage_slots == out.run.total_shortage_slots and c.after.cost == out.run.total_cost
    assert all(d.shortage_before != d.shortage_after or d.cost_before != d.cost_after for d in c.days)
    assert WED in {d.date for d in c.days}
    ws = openpyxl.load_workbook(export_run(sample, out.run.run_id, tmp_path / "r.xlsx"))["RunSummary"]
    summary = {r[0]: r[1] for r in ws.iter_rows(min_row=2, values_only=True)}
    assert summary["ChangedAssignments"] == c.diff.changed_assignments


def test_initial_draft_is_compared_with_the_active_schedule(sample):
    other = run_initial(sample, 10, workers=2, seed=1).run.schedule_version
    c = compare(sample, other)
    assert c.run_type == "INITIAL" and c.compared_with == sample.active_schedule_version()


def test_no_active_schedule_and_bad_references(tmp_path, small_input, sample):
    s = Store(str(tmp_path / "x.sqlite"))
    import_workbook(s, small_input)
    v = run_initial(s, 10, workers=2, seed=0).run.schedule_version
    c = compare(s, v)
    assert c.before is None and c.compared_with is None and "nothing to compare" in c.note
    with pytest.raises(Unresolved) as ei:
        compare(sample, sample.active_schedule_version())
    assert ei.value.code == "INVALID_PARAMETER"
    run_initial(s, 10, workers=2, seed=1)
    with pytest.raises(Unresolved) as ei:                  # two drafts and none named
        compare(s)
    assert ei.value.code == "AMBIGUOUS" and len(ei.value.candidates) == 2


# ------------------------------------------------------------------ export of a repair version
def test_export_of_a_repair_version_has_the_repair_fields(repaired, tmp_path):
    store, out = repaired
    set_lock(store, "C", THU, True)                        # locked after the repair: not in this draft
    wb = openpyxl.load_workbook(export_run(store, out.run.schedule_version, tmp_path / "r.xlsx"))
    summary = {r[0]: r[1] for r in wb["RunSummary"].iter_rows(min_row=2, values_only=True)}
    assert summary["RunType"] == "REPAIR" and summary["BaseScheduleVersion"] == store.active_schedule_version()
    assert summary["ChangedAssignments"] == 2 and summary["StabilityOptimalityProven"] is True
    ws = wb["EmployeeSummary"]
    header = [c.value for c in ws[1]]
    rows = {r[header.index("EmployeeID")]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["A"][header.index("MinShiftsExempt")] is True
    assert rows["A"][header.index("ShiftCount")] == 3     # Sun, Mon, Tue remain: past shifts included
    assert rows["B"][header.index("ShiftCount")] == 5


def test_lock_flag_is_carried_into_the_repair_export(tmp_path):
    store = make_store(tmp_path)
    set_lock(store, "C", THU, True)
    report_absence(store, "A", WED)
    out = run_repair(store, 10, workers=2)
    ws = openpyxl.load_workbook(export_run(store, out.run.schedule_version, tmp_path / "r.xlsx"))["Schedule"]
    header = [c.value for c in ws[1]]
    locked = [(r[header.index("EmployeeID")], r[header.index("ShiftID")]) for r in ws.iter_rows(min_row=2,
              values_only=True) if r[header.index("IsLocked")]]
    assert locked == [("C", "S5")]


# ------------------------------------------------------------------ approval
def test_only_approval_changes_the_active_schedule_and_a_stale_repair_is_refused(tmp_path):
    store = make_store(tmp_path)
    base = store.active_schedule_version()
    report_absence(store, "A", WED)
    first = run_repair(store, 10, workers=2).run.schedule_version
    second = run_repair(store, 10, workers=2, seed=1).run.schedule_version
    assert store.active_schedule_version() == base
    approve_draft(store, first)
    with pytest.raises(ApprovalRefused) as ei:
        approve_draft(store, second)
    assert ei.value.code == "BASE_VERSION_STALE"
    assert store.active_schedule_version() == first


# ------------------------------------------------------------------ tool and command line
def test_tool_compare_draft(repaired, tmp_path):
    store, out = repaired
    r = call(build_server(store, export_dir=tmp_path), "compare_draft", {}).structured_content
    assert r["ok"] and r["draft"] == out.run.schedule_version and r["changed_assignments"] == 2
    assert [(x["employee_id"], x["kind"], x["weekday"]) for x in r["changes"]] == \
        [("A", "REMOVED", "Wednesday"), ("B", "ADDED", "Wednesday")]
    assert r["before"]["cost"] == r["after"]["cost"]
    r = call(build_server(store, export_dir=tmp_path), "compare_draft",
             {"ref": store.active_schedule_version()}).structured_content
    assert r["error"]["code"] == "INVALID_PARAMETER"


def test_cli_compare(repaired, capsys):
    store, out = repaired
    assert main(["--db", str(store.engine.url.database), "compare", out.run.schedule_version]) == 0
    text = capsys.readouterr().out
    assert "ChangedAssignments 2" in text and "REMOVED" in text and "ADDED" in text
