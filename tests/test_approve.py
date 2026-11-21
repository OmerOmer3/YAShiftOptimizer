"""SCRUM-37: approve a draft before it becomes the schedule (spec section 4).

  * approval sets the version APPROVED and makes it the one active schedule;
  * before approving, the server checks that the input version is still current and that
    the schedule passes the independent checker;
  * creating a draft and approving it are separate operations; nothing else changes the
    active schedule;
  * the approval is recorded with its time and version.

The base-version check (BASE_VERSION_STALE, for repair drafts) is SCRUM-38.
"""
from __future__ import annotations

import datetime as dt

import openpyxl
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.db.store import AssignmentRow, ScheduleVersionRow
from shiftopt.export import export_run
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import run_initial
from shiftopt.service.approve import ApprovalRefused, approve_draft
from tests.test_mcp_server import call


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def draft(store, seed=0) -> str:
    v = run_initial(store, 10, workers=2, seed=seed).run.schedule_version
    assert v is not None
    return v


def active_count(store) -> int:
    with Session(store.engine) as s:
        return len(s.scalars(select(ScheduleVersionRow).where(ScheduleVersionRow.is_active)).all())


def refused(store, version, code):
    before = (store.active_schedule_version(), store.list_schedule_versions())
    with pytest.raises(ApprovalRefused) as ei:
        approve_draft(store, version)
    assert ei.value.code == code, ei.value
    assert (store.active_schedule_version(), store.list_schedule_versions()) == before   # nothing changed
    return ei.value


# ------------------------------------------------------------------ service
def test_approve_makes_the_draft_the_active_schedule(store):
    v = draft(store)
    t0 = dt.datetime.now()
    out = approve_draft(store, v)
    assert out.schedule_version == v and out.replaced_version is None
    assert t0 <= out.approved_at <= dt.datetime.now()
    info = store.schedule_version_info(v)
    assert info["approval_status"] == "APPROVED" and info["is_active"] is True
    assert store.active_schedule_version() == v and active_count(store) == 1
    rec = store.get_run(info["run_id"])
    assert out.total_cost == rec.total_cost and out.total_shortage_slots == rec.total_shortage_slots


def test_approving_another_draft_replaces_the_active_one(store):
    first, second = draft(store), draft(store, seed=1)
    approve_draft(store, first)
    out = approve_draft(store, second)
    assert out.replaced_version == first
    assert store.active_schedule_version() == second and active_count(store) == 1
    old = store.schedule_version_info(first)
    assert old["approval_status"] == "APPROVED" and old["is_active"] is False   # history is kept


def test_a_new_run_does_not_change_the_active_schedule(store):
    v = draft(store)
    approve_draft(store, v)
    draft(store, seed=1)
    assert store.active_schedule_version() == v


def test_already_approved(store):
    v = draft(store)
    approve_draft(store, v)
    refused(store, v, "ALREADY_APPROVED")


def test_unknown_version(store):
    refused(store, "SV-0042", "NOT_FOUND")


def test_draft_of_an_older_input_is_refused(store, small_input):
    v = draft(store)
    assert import_workbook(store, small_input).status == "OK"          # IN-0002 becomes active
    e = refused(store, v, "INPUT_VERSION_STALE")
    assert "IN-0001" in e.message and "IN-0002" in e.message


def test_schedule_that_fails_the_checker_is_refused(store):
    v = draft(store)
    with Session(store.engine) as s, s.begin():                         # tamper: drop every break
        s.execute(update(AssignmentRow).where(AssignmentRow.schedule_version_id == int(v[3:]))
                  .values(break1_start=None, break1_end=None, break2_start=None, break2_end=None))
    e = refused(store, v, "VALIDATION_FAILED")
    assert e.details and "break" in " ".join(e.details).lower()


def test_export_after_approval_says_approved(store, tmp_path):
    v = draft(store)
    approve_draft(store, v)
    out = export_run(store, v, tmp_path / "a.xlsx")
    rs = {k: val for k, val in openpyxl.load_workbook(out)["RunSummary"].iter_rows(min_row=2, values_only=True)}
    assert rs["ApprovalStatus"] == "APPROVED"


# ------------------------------------------------------------------ chat tool
def test_tool_approves_a_named_draft(store, tmp_path):
    v = draft(store)
    srv = build_server(store, export_dir=tmp_path)
    s = call(srv, "approve_draft", {"ref": v}).structured_content
    assert s["ok"] and s["schedule_version"] == v and s["active_schedule_version"] == v
    assert s["approved_at"] and s["replaced_version"] is None
    assert call(srv, "get_system_status", {}).structured_content["active_schedule_version"] == v


def test_tool_asks_which_draft_when_there_are_two(store, tmp_path):
    draft(store), draft(store, seed=1)
    s = call(build_server(store, export_dir=tmp_path), "approve_draft", {}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "AMBIGUOUS" and len(s["error"]["candidates"]) == 2
    assert store.active_schedule_version() is None


def test_tool_reports_refusals(store, tmp_path, small_input):
    v = draft(store)
    import_workbook(store, small_input)
    s = call(build_server(store, export_dir=tmp_path), "approve_draft", {"ref": v}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "INPUT_VERSION_STALE"
    assert s["active_schedule_version"] is None


# ------------------------------------------------------------------ command line
def test_cli_approve(store, capsys):
    v = draft(store)
    db = str(store.engine.url).removeprefix("sqlite:///")
    assert main(["--db", db, "approve", v]) == 0
    assert "APPROVED" in capsys.readouterr().out
    assert main(["--db", db, "approve", v]) == 1
    assert "ALREADY_APPROVED" in capsys.readouterr().out


def test_write_is_refused_if_the_state_changed_after_the_checks(store):
    """Compare-and-set inside the transaction: two approvals checked against the same
    active schedule cannot both be written."""
    from shiftopt.db.store import StaleState
    first, second = draft(store), draft(store, seed=1)
    # Both requests checked while nothing was active; the first one is written...
    store.approve(first, expected_input="IN-0001", expected_active=None)
    # ...the second, still expecting "nothing active", is refused and changes nothing.
    with pytest.raises(StaleState) as ei:
        store.approve(second, expected_input="IN-0001", expected_active=None)
    assert ei.value.code == "ACTIVE_CHANGED"
    assert store.active_schedule_version() == first and active_count(store) == 1
