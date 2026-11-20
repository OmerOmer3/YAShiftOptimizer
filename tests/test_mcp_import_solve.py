"""SCRUM-33: import the week and create a schedule from the chat (MCP tools, in-process).

Acceptance criteria:
  * an invalid file returns INVALID_INPUT with sheet, row, column and reason, and the
    previous data stays active (spec section 6);
  * create_schedule starts a run with the given time budget and returns its run ID;
  * the run status can be checked: status, shortage, cost, which objectives were proven;
  * the result is a draft; the active schedule does not change (spec section 4).
"""
from __future__ import annotations

import pytest

from shiftopt.db import Store
from shiftopt.mcp_server import build_server
from tests.test_mcp_server import call


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "db.sqlite"))


@pytest.fixture
def srv(store, tmp_path):
    return build_server(store, export_dir=tmp_path / "exports")


# ------------------------------------------------------------------ import_input
def test_import_valid_file(srv, store, small_input):
    s = call(srv, "import_input", {"file_path": str(small_input)}).structured_content
    assert s["ok"] is True and s["error"] is None
    assert s["input_version"] == store.active_input_version() == "IN-0001"
    assert s["week_start"] == "2026-11-01"
    assert s["employee_count"] == 10 and s["issues"] == []


def test_invalid_file_reports_where_and_keeps_the_previous_version(srv, store, small_input, mutate):
    call(srv, "import_input", {"file_path": str(small_input)})
    def clear_first_id(wb):
        wb["Employees"]["A2"].value = None                      # missing EmployeeID
    bad = mutate(clear_first_id)
    s = call(srv, "import_input", {"file_path": str(bad)}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "INVALID_INPUT"
    assert s["input_version"] is None
    assert s["active_input_version"] == store.active_input_version() == "IN-0001"
    issue = s["issues"][0]
    assert issue["sheet"] == "Employees" and issue["row"] == 2 and issue["column"] and issue["message"]


def test_import_missing_or_wrong_file(srv, tmp_path):
    s = call(srv, "import_input", {"file_path": str(tmp_path / "nope.xlsx")}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NOT_FOUND"
    (tmp_path / "notes.txt").write_text("hi")
    s = call(srv, "import_input", {"file_path": str(tmp_path / "notes.txt")}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "INVALID_PARAMETER"


# ------------------------------------------------------------------ create_schedule
def test_create_schedule_without_input(srv):
    s = call(srv, "create_schedule", {"time_budget_seconds": 10}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NO_ACTIVE_INPUT"


def test_time_budget_must_be_positive(srv, small_input):
    call(srv, "import_input", {"file_path": str(small_input)})
    assert call(srv, "create_schedule", {"time_budget_seconds": 0}).is_error
    assert call(srv, "create_schedule", {"time_budget_seconds": -5}).is_error


def test_create_schedule_returns_a_draft_and_keeps_the_active_schedule(srv, store, small_input):
    call(srv, "import_input", {"file_path": str(small_input)})
    s = call(srv, "create_schedule", {"time_budget_seconds": 20, "workers": 2}).structured_content
    assert s["ok"] is True
    run = s["run"]
    assert run["run_id"] == "RUN-0001" and run["status"] in ("OPTIMAL", "FEASIBLE")
    assert run["schedule_version"] == "SV-0001" and run["approval_status"] == "DRAFT"
    assert run["time_budget_seconds"] == 20 and run["validation_passed"] is True
    rec = store.get_run("RUN-0001")
    assert run["total_cost"] == rec.total_cost and run["total_shortage_slots"] == rec.total_shortage_slots
    assert store.active_schedule_version() is None and s["active_schedule_version"] is None


def test_infeasible_week_is_a_result_not_an_error(srv, infeasible_input):
    call(srv, "import_input", {"file_path": str(infeasible_input)})
    s = call(srv, "create_schedule", {"time_budget_seconds": 10, "workers": 2}).structured_content
    assert s["ok"] is True                                 # the tool worked; the week has no solution
    run = s["run"]
    assert run["status"] == "INFEASIBLE" and run["schedule_version"] is None
    assert run["total_cost"] is None and run["first_feasible_seconds"] is None   # empty, not zero
    assert any("at least 4 shifts" in d for d in run["diagnostics"])


# ------------------------------------------------------------------ get_run_status
def test_get_run_status_latest_and_by_id(srv, small_input):
    call(srv, "import_input", {"file_path": str(small_input)})
    created = call(srv, "create_schedule", {"time_budget_seconds": 20, "workers": 2}).structured_content["run"]
    latest = call(srv, "get_run_status", {}).structured_content
    by_id = call(srv, "get_run_status", {"run_id": "run-0001"}).structured_content
    assert latest["ok"] and by_id["ok"]
    assert latest["run"] == by_id["run"] == created
    for k in ("coverage_proven", "cost_proven", "termination_reason", "solver_info"):
        assert k in latest["run"]


def test_get_run_status_unknown_run(srv, small_input):
    s = call(srv, "get_run_status", {}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "NOT_FOUND"
    call(srv, "import_input", {"file_path": str(small_input)})
    call(srv, "create_schedule", {"time_budget_seconds": 10, "workers": 2})
    s = call(srv, "get_run_status", {"run_id": "RUN-0042"}).structured_content
    assert s["error"]["code"] == "NOT_FOUND" and s["error"]["candidates"] == ["RUN-0001"]
