"""SCRUM-35: see where the schedule is understaffed and what it costs.

  * total shortage half-hours and total cost for a schedule version;
  * shortage and surplus per day and per half hour, recomputed by the independent checker;
  * cost per day;
  * the numbers match what the Excel export writes for the same version.
The day is given as text and resolved inside the server, so "Saturday" is refused whichever
model calls the tool (decision recorded in SCRUM-105).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import openpyxl
import pytest

from shiftopt.db import Store
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.mcp_server import build_server
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.queries import coverage_report
from tests.test_mcp_server import call

WED = dt.date(2026, 11, 4)


def _sheet(path, name):
    return [list(r) for r in openpyxl.load_workbook(path)[name].iter_rows(min_row=2, values_only=True)]


def _t(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ------------------------------------------------------------------ pure, against the course sample
@pytest.fixture(scope="module")
def sample():
    from tests.conftest import SAMPLES
    return read_input(SAMPLES / "Input_Small_10.xlsx").data, read_schedule(SAMPLES / "Output_Small_10.xlsx"), \
        SAMPLES / "Output_Small_10.xlsx"


def test_totals_match_the_sample(sample):
    data, schedule, out = sample
    r = coverage_report(data, schedule, group_size=3)
    cov, sched = _sheet(out, "Coverage"), _sheet(out, "Schedule")
    assert r.total_shortage_slots == sum(row[5] for row in cov)
    assert r.total_surplus_slots == sum(row[6] for row in cov)
    assert r.total_cost == pytest.approx(sum(row[10] for row in sched))
    assert r.total_shortage_hours == r.total_shortage_slots / 2


def test_per_day_cost_and_coverage_match_the_sample(sample):
    data, schedule, out = sample
    r = coverage_report(data, schedule, group_size=3)
    cost, short, surplus = defaultdict(float), defaultdict(int), defaultdict(int)
    for row in _sheet(out, "Schedule"):
        cost[row[0].date()] += row[10]
    for row in _sheet(out, "Coverage"):
        short[row[0].date()] += row[5]
        surplus[row[0].date()] += row[6]
    assert [d.date for d in r.days] == sorted(short)
    for d in r.days:
        assert d.cost == pytest.approx(cost[d.date]) and d.shortage_slots == short[d.date]
        assert d.surplus_slots == surplus[d.date]
    assert sum(d.cost for d in r.days) == pytest.approx(r.total_cost)


def test_half_hours_of_one_day_match_the_sample(sample):
    data, schedule, out = sample
    r = coverage_report(data, schedule, group_size=3, day=WED)
    expected = [row for row in _sheet(out, "Coverage") if row[0].date() == WED]
    got = [[s.slot_start, s.slot_end, s.required, s.assigned, s.shortage, s.surplus] for s in r.day_slots]
    assert got == [[row[1].strftime("%H:%M"), row[2].strftime("%H:%M"), *row[3:7]] for row in expected]
    workers = sorted(row[1] for row in _sheet(out, "Schedule") if row[0].date() == WED)
    assert sorted(s.employee_id for s in r.day_shifts) == workers


def test_understaffed_half_hours_are_listed(sample):
    data, schedule, _ = sample
    removed = next(a for a in schedule.assignments if a.date == WED)
    schedule.assignments.remove(removed)                      # take one shift away -> shortage appears
    try:
        r = coverage_report(data, schedule, group_size=3)
        assert r.total_shortage_slots > 0
        assert all(s.shortage > 0 for s in r.shortage_slots)
        assert sum(s.shortage for s in r.shortage_slots) == r.total_shortage_slots
        assert {s.date for s in r.shortage_slots} == {WED}
    finally:
        schedule.assignments.append(removed)


# ------------------------------------------------------------------ chat tool, against our export
@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def draft(store, seed=0):
    return run_initial(store, 10, workers=2, seed=seed).run


def test_tool_matches_the_export_of_the_same_version(store, tmp_path):
    run = draft(store)
    srv = build_server(store, export_dir=tmp_path)
    s = call(srv, "get_coverage", {}).structured_content            # one draft, no active: that draft
    assert s["ok"] and s["schedule_version"] == run.schedule_version and s["is_active"] is False
    out = export_run(store, run.schedule_version, tmp_path / "x.xlsx")
    rs = {k: v for k, v in openpyxl.load_workbook(out)["RunSummary"].iter_rows(min_row=2, values_only=True)}
    assert s["total_shortage_slots"] == rs["TotalShortageSlots"] == run.total_shortage_slots
    assert s["total_cost"] == rs["TotalCost"] == run.total_cost
    day = call(srv, "get_coverage", {"day": "רביעי"}).structured_content
    assert day["day"] == "2026-11-04"
    exported = [[_t(r[1].hour * 60 + r[1].minute), _t(r[2].hour * 60 + r[2].minute), *r[3:7]]
                for r in _sheet(out, "Coverage") if r[0].date() == WED]
    assert [[x["slot_start"], x["slot_end"], x["required"], x["assigned"], x["shortage"], x["surplus"]]
            for x in day["day_slots"]] == exported
    assert {x["employee_id"] for x in day["day_shifts"]} == \
        {r[1] for r in _sheet(out, "Schedule") if r[0].date() == WED}


def test_saturday_is_refused_by_the_server(store, tmp_path):
    draft(store)
    s = call(build_server(store, export_dir=tmp_path), "get_coverage", {"day": "Saturday"}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NOT_FOUND"
    assert len(s["error"]["candidates"]) == 6 and s["error"]["candidates"][0].startswith("2026-11-01")


def test_default_is_the_active_schedule(store, tmp_path):
    first = draft(store)
    approve_draft(store, first.schedule_version)
    draft(store, seed=1)                                               # a newer draft exists
    s = call(build_server(store, export_dir=tmp_path), "get_coverage", {}).structured_content
    assert s["schedule_version"] == first.schedule_version and s["is_active"] is True
    assert s["approval_status"] == "APPROVED"


def test_two_drafts_and_no_active_schedule_is_ambiguous(store, tmp_path):
    draft(store), draft(store, seed=1)
    s = call(build_server(store, export_dir=tmp_path), "get_coverage", {}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "AMBIGUOUS" and len(s["error"]["candidates"]) == 2
    named = call(build_server(store, export_dir=tmp_path), "get_coverage", {"ref": "#2"}).structured_content
    assert named["ok"] and named["schedule_version"] == "SV-0002"


def test_no_schedule_yet(store, tmp_path):
    s = call(build_server(store, export_dir=tmp_path), "get_coverage", {}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NOT_FOUND"
