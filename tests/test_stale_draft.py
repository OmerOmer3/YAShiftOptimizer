"""SCRUM-38: have an outdated draft refused (spec sections 4 and 10).

"Before a draft is approved, the server checks that the input and the schedule version it
is based on are still current." Spec section 8: BaseScheduleVersion is empty for an
initial run, so only drafts that have a base (repair drafts, sprint 4) can have a stale
base. Repair drafts are built here directly in the store with a base, the same way the
repair run will store them.

Acceptance test from section 10, "approval of an outdated version": two drafts from the
same base; after the first is approved, approving the second is refused and changes nothing.
"""
from __future__ import annotations

import datetime as dt

import pytest

from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.db.store import StaleState
from shiftopt.engine.solver import SolveResult
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import ApprovalRefused, approve_draft, run_initial
from tests.test_mcp_server import call


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def repair_draft(store: Store, base: str) -> str:
    """A REPAIR draft with the same assignments as `base`, stored with base_version=base."""
    info = store.schedule_version_info(base)
    run = store.get_run(info["run_id"])
    result = SolveResult(status=run.status, schedule=None, time_budget_seconds=1, first_feasible_seconds=0.1,
                         optimization_seconds=0.1, total_shortage_slots=run.total_shortage_slots,
                         total_cost=run.total_cost, coverage_proven=True, cost_proven=True,
                         termination_reason="test repair draft")
    _, version = store.save_run(input_version=store.active_input_version(), run_type="REPAIR", group_size=2,
                                result=result, validation_passed=True, optimization_seconds=0.1,
                                started_at=dt.datetime.now(), schedule=store.load_schedule(base), base_version=base)
    return version


@pytest.fixture
def approved_base(store):
    v = run_initial(store, 10, workers=2).run.schedule_version
    approve_draft(store, v)
    return v


def snapshot(store):
    return store.active_schedule_version(), store.list_schedule_versions(), store.list_audit()


def refused(store, version, code):
    before = snapshot(store)
    with pytest.raises(ApprovalRefused) as ei:
        approve_draft(store, version)
    assert ei.value.code == code, ei.value
    assert snapshot(store) == before                     # a refused approval changes nothing, no log entry
    return ei.value


# ------------------------------------------------------------------ section 10 acceptance test
def test_two_drafts_from_the_same_base(store, approved_base):
    a, b = repair_draft(store, approved_base), repair_draft(store, approved_base)
    assert store.schedule_version_info(b)["base_version"] == approved_base
    approve_draft(store, a)
    e = refused(store, b, "BASE_VERSION_STALE")
    assert approved_base in e.message and a in e.message           # names the base and what is active now
    assert store.active_schedule_version() == a


def test_draft_on_the_current_base_is_approved(store, approved_base):
    a = repair_draft(store, approved_base)
    out = approve_draft(store, a)
    assert out.replaced_version == approved_base and store.active_schedule_version() == a


def test_input_is_checked_before_the_base(store, approved_base, small_input):
    a = repair_draft(store, approved_base)
    import_workbook(store, small_input)                              # new input: draft is stale both ways
    refused(store, a, "INPUT_VERSION_STALE")


def test_initial_drafts_have_no_base_and_may_replace_each_other(store, approved_base):
    """Section 8: an initial run has no base; choosing another initial draft of the same input is allowed."""
    other = run_initial(store, 10, workers=2, seed=1).run.schedule_version
    assert store.schedule_version_info(other)["base_version"] is None
    assert approve_draft(store, other).replaced_version == approved_base


def test_base_is_confirmed_inside_the_transaction(store, approved_base):
    """Compare-and-set: even with the right expected active version, a draft whose base is
    not the active schedule is not written."""
    a, b = repair_draft(store, approved_base), repair_draft(store, approved_base)
    approve_draft(store, a)
    with pytest.raises(StaleState) as ei:
        store.approve(b, expected_input="IN-0001", expected_active=a)
    assert ei.value.code == "BASE_VERSION_STALE"
    assert store.active_schedule_version() == a and len(store.list_audit()) == 2


# ------------------------------------------------------------------ chat tool and command line
def test_tool_reports_base_version_stale(store, approved_base, tmp_path):
    a, b = repair_draft(store, approved_base), repair_draft(store, approved_base)
    srv = build_server(store, export_dir=tmp_path)
    assert call(srv, "approve_draft", {"ref": a}).structured_content["ok"]
    s = call(srv, "approve_draft", {"ref": b}).structured_content
    assert s["ok"] is False and s["error"]["code"] == "BASE_VERSION_STALE"
    assert s["active_schedule_version"] == a


def test_cli_reports_base_version_stale(store, approved_base, capsys):
    a, b = repair_draft(store, approved_base), repair_draft(store, approved_base)
    db = str(store.engine.url).removeprefix("sqlite:///")
    assert main(["--db", db, "approve", a]) == 0
    assert main(["--db", db, "approve", b]) == 1
    assert "BASE_VERSION_STALE" in capsys.readouterr().out
