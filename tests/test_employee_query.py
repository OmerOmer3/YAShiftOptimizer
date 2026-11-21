"""SCRUM-34: ask about an employee's hours and shifts.

  * "How many hours were scheduled for E003?" returns the employee's shifts, break times,
    paid hours and cost, computed by the server (independent checker's metrics);
  * the answer says which schedule version it is about (active or a named draft);
  * an unknown employee returns a clear error with candidates, so the chat can ask.
The employee is given as text and resolved inside the server (decision in SCRUM-105).
"""
from __future__ import annotations

import openpyxl
import pytest

from shiftopt.db import Store
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.queries import employee_report
from tests.test_mcp_server import call


def _sheet(path, name):
    return [list(r) for r in openpyxl.load_workbook(path)[name].iter_rows(min_row=2, values_only=True)]


def _hm(t):
    return None if t is None else t.strftime("%H:%M")


# ------------------------------------------------------------------ pure, against the course sample
@pytest.fixture(scope="module")
def sample():
    from tests.conftest import SAMPLES
    out = SAMPLES / "Output_Small_10.xlsx"
    return read_input(SAMPLES / "Input_Small_10.xlsx").data, read_schedule(out), out


def test_e003_matches_the_sample_employee_summary(sample):
    data, schedule, out = sample
    r = employee_report(data, schedule, "E003", group_size=3)
    [row] = [x for x in _sheet(out, "EmployeeSummary") if x[0] == "E003"]
    assert r.employee_id == "E003" and r.name == "Employee 03"
    assert r.shift_count == row[1]
    assert r.paid_hours == pytest.approx(row[2]) and r.total_cost == pytest.approx(row[3])
    assert r.preferred_start == _hm(row[4])
    assert r.preferred_count == row[5] and r.preference_rate == pytest.approx(row[6])
    assert r.min_shifts_exempt is bool(row[7])


def test_e003_shifts_and_breaks_match_the_sample_schedule(sample):
    data, schedule, out = sample
    r = employee_report(data, schedule, "E003", group_size=3)
    rows = sorted((x for x in _sheet(out, "Schedule") if x[1] == "E003"), key=lambda x: x[0])
    assert [s.date.isoformat() for s in r.shifts] == [x[0].date().isoformat() for x in rows]
    for s, x in zip(r.shifts, rows):
        assert (s.shift_id, s.start, s.end) == (x[2], _hm(x[3]), _hm(x[4]))
        assert s.breaks == [f"{_hm(b)}-{_hm(e)}" for b, e in ((x[5], x[6]), (x[7], x[8])) if b is not None]
        assert s.paid_hours == pytest.approx(x[9]) and s.cost == pytest.approx(x[10])
    assert sum(s.paid_hours for s in r.shifts) == pytest.approx(r.paid_hours)


def test_input_facts_are_included(sample):
    data, schedule, _ = sample
    r = employee_report(data, schedule, "E003", group_size=3)
    e = data.employees["E003"]
    assert r.unavailable_dates == sorted(data.date_for_day(d).isoformat() for d in e.unavailable_days)


def test_pair_team_leaves_preference_counts_empty(sample):
    data, schedule, _ = sample
    r = employee_report(data, schedule, "E003", group_size=2)
    assert r.preferred_start is not None and r.preferred_count is None and r.preference_rate is None


# ------------------------------------------------------------------ chat tool
@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def test_tool_matches_the_export_of_the_same_version(store, tmp_path):
    run = run_initial(store, 10, workers=2).run
    srv = build_server(store, export_dir=tmp_path)
    s = call(srv, "get_employee_schedule", {"employee": "E003"}).structured_content
    assert s["ok"] and s["schedule_version"] == run.schedule_version
    assert s["is_active"] is False and s["approval_status"] == "DRAFT"     # says which version
    out = export_run(store, run.schedule_version, tmp_path / "x.xlsx")
    [row] = [x for x in _sheet(out, "EmployeeSummary") if x[0] == "E003"]
    assert (s["shift_count"], s["paid_hours"], s["total_cost"]) == (row[1], row[2], row[3])
    assert len(s["shifts"]) == row[1]


@pytest.mark.parametrize("text", ["E003", "e3", "3", "Employee 03"])
def test_tool_resolves_the_employee_in_the_server(store, tmp_path, text):
    run_initial(store, 10, workers=2)
    s = call(build_server(store, export_dir=tmp_path), "get_employee_schedule", {"employee": text}).structured_content
    assert s["ok"] and s["employee_id"] == "E003"


def test_unknown_and_ambiguous_employee(store, tmp_path):
    run_initial(store, 10, workers=2)
    srv = build_server(store, export_dir=tmp_path)
    nf = call(srv, "get_employee_schedule", {"employee": "E099"}).structured_content
    assert nf["ok"] is False and nf["error"]["code"] == "NOT_FOUND" and "E099" in nf["error"]["message"]
    amb = call(srv, "get_employee_schedule", {"employee": "Employee"}).structured_content
    assert amb["ok"] is False and amb["error"]["code"] == "AMBIGUOUS" and len(amb["error"]["candidates"]) == 10


def test_active_schedule_and_named_draft(store, tmp_path):
    first = run_initial(store, 10, workers=2).run.schedule_version
    approve_draft(store, first)
    second = run_initial(store, 10, workers=2, seed=1).run.schedule_version
    srv = build_server(store, export_dir=tmp_path)
    a = call(srv, "get_employee_schedule", {"employee": "E003"}).structured_content
    assert a["schedule_version"] == first and a["is_active"] is True
    d = call(srv, "get_employee_schedule", {"employee": "E003", "ref": second}).structured_content
    assert d["schedule_version"] == second and d["is_active"] is False


def test_no_schedule_yet(store, tmp_path):
    s = call(build_server(store, export_dir=tmp_path), "get_employee_schedule", {"employee": "E003"}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NOT_FOUND"
