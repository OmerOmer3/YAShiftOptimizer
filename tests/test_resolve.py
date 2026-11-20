"""SCRUM-36: resolve employees, dates and drafts without guessing (spec section 4).

"A request that does not identify an employee, a date or a draft unambiguously must be
answered with a request for clarification." The resolvers either return exactly one
match or raise Unresolved with AMBIGUOUS / NOT_FOUND and the candidates; they never
change anything.
"""
from __future__ import annotations

import datetime as dt

import pytest

from shiftopt.db import Store
from shiftopt.domain import Employee
from shiftopt.ingest import import_workbook, read_input
from shiftopt.mcp_server import build_server
from shiftopt.service import run_initial
from shiftopt.service.resolve import Unresolved, resolve_date, resolve_draft, resolve_employee
from tests.test_mcp_server import call


@pytest.fixture(scope="module")
def data():
    from tests.conftest import SAMPLES
    d = read_input(SAMPLES / "Input_Small_10.xlsx").data
    # Real-looking names to exercise ambiguity (the sample uses "Employee 01".."Employee 10").
    names = {"E001": "Dana Levi", "E002": "Dana Cohen", "E003": "Yossi Mizrahi", "E004": "דנה אברהם",
             "E005": "Avi Levi"}
    for eid, n in names.items():
        e = d.employees[eid]
        d.employees[eid] = Employee(e.employee_id, n, e.unavailable_days, e.preferred_start)
    return d


def _fails(fn, *args, code):
    with pytest.raises(Unresolved) as ei:
        fn(*args)
    assert ei.value.code == code, ei.value
    return ei.value


# ------------------------------------------------------------------ employees
@pytest.mark.parametrize("q", ["E003", "e003", " E3 ", "3", "Yossi Mizrahi", "yossi mizrahi", "Mizrahi", "Yossi"])
def test_employee_unique(data, q):
    assert resolve_employee(data, q) == "E003"


def test_employee_hebrew_name(data):
    assert resolve_employee(data, "דנה אברהם") == "E004"


def test_employee_ambiguous_first_name(data):
    e = _fails(resolve_employee, data, "Dana", code="AMBIGUOUS")
    assert [c.split()[0] for c in e.candidates] == ["E001", "E002"]
    assert "Dana Levi" in e.candidates[0]


def test_employee_ambiguous_surname(data):
    e = _fails(resolve_employee, data, "Levi", code="AMBIGUOUS")
    assert {c.split()[0] for c in e.candidates} == {"E001", "E005"}


def test_employee_not_found_suggests_close_matches(data):
    e = _fails(resolve_employee, data, "Yosi Mizrachi", code="NOT_FOUND")
    assert e.candidates and e.candidates[0].startswith("E003")
    _fails(resolve_employee, data, "E099", code="NOT_FOUND")
    _fails(resolve_employee, data, "", code="NOT_FOUND")


def test_partial_word_is_not_a_match(data):
    # "Dan" is not a word of any name: no guessing by prefix.
    _fails(resolve_employee, data, "Dan", code="NOT_FOUND")


# ------------------------------------------------------------------ dates (week of 2026-11-01, Sunday)
@pytest.mark.parametrize("q,expected", [
    ("2026-11-04", dt.date(2026, 11, 4)), ("4.11", dt.date(2026, 11, 4)), ("04/11/2026", dt.date(2026, 11, 4)),
    ("Wednesday", dt.date(2026, 11, 4)), ("wed", dt.date(2026, 11, 4)), ("רביעי", dt.date(2026, 11, 4)),
    ("יום רביעי", dt.date(2026, 11, 4)), ("יום ד'", dt.date(2026, 11, 4)),
    ("Sunday", dt.date(2026, 11, 1)), ("ראשון", dt.date(2026, 11, 1)), ("Friday", dt.date(2026, 11, 6)),
])
def test_date_resolved_against_the_planning_week(data, q, expected):
    assert resolve_date(data, q) == expected


@pytest.mark.parametrize("q", ["Saturday", "שבת", "2026-11-07", "2026-11-11", "31.12", "next week", "3"])
def test_date_outside_the_week_or_unclear(data, q):
    e = _fails(resolve_date, data, q, code="NOT_FOUND")
    assert len(e.candidates) == 6 and e.candidates[0].startswith("2026-11-01")


# ------------------------------------------------------------------ drafts
@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def test_no_drafts(store):
    _fails(resolve_draft, store, None, code="NOT_FOUND")


def test_one_draft_needs_no_name(store):
    run = run_initial(store, 10, workers=2).run
    assert resolve_draft(store, None) == run.schedule_version == "SV-0001"


def test_two_drafts_without_a_name_are_ambiguous(store):
    run_initial(store, 10, workers=2)
    run_initial(store, 10, workers=2, seed=1)
    e = _fails(resolve_draft, store, None, code="AMBIGUOUS")
    assert [c.split()[0] for c in e.candidates] == ["SV-0002", "SV-0001"]   # newest first
    assert "RUN-0002" in e.candidates[0] and "cost" in e.candidates[0]
    assert resolve_draft(store, "latest") == "SV-0002"
    assert resolve_draft(store, "האחרונה") == "SV-0002"
    assert resolve_draft(store, "sv-0001") == "SV-0001"
    assert resolve_draft(store, "RUN-0001") == "SV-0001"
    _fails(resolve_draft, store, "SV-0009", code="NOT_FOUND")
    _fails(resolve_draft, store, "the good one", code="NOT_FOUND")


def test_drafts_of_an_older_input_are_not_offered(store, small_input):
    run_initial(store, 10, workers=2)
    assert import_workbook(store, small_input).status == "OK"     # new input version
    _fails(resolve_draft, store, None, code="NOT_FOUND")


# ------------------------------------------------------------------ through the chat tools
def test_resolve_reference_tool(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    ok = call(srv, "resolve_reference", {"kind": "date", "text": "רביעי"}).structured_content
    assert ok["ok"] and ok["value"] == "2026-11-04" and "Wednesday" in ok["label"]
    emp = call(srv, "resolve_reference", {"kind": "employee", "text": "E3"}).structured_content
    assert emp["value"] == "E003"
    amb = call(srv, "resolve_reference", {"kind": "employee", "text": "Employee"}).structured_content
    assert amb["ok"] is False and amb["error"]["code"] == "AMBIGUOUS" and len(amb["error"]["candidates"]) == 10
    assert call(srv, "resolve_reference", {"kind": "shift", "text": "x"}).is_error   # kind is validated


def test_export_without_a_name_asks_when_there_are_two_drafts(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    run_initial(store, 10, workers=2)
    one = call(srv, "export_schedule", {}).structured_content
    assert one["ok"] and one["schedule_version"] == "SV-0001"
    run_initial(store, 10, workers=2, seed=1)
    files_before = set(tmp_path.iterdir())
    two = call(srv, "export_schedule", {}).structured_content
    assert two["ok"] is False and two["error"]["code"] == "AMBIGUOUS"
    assert set(tmp_path.iterdir()) == files_before                     # nothing was written
