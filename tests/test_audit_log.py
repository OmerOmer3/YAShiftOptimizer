"""SCRUM-24: a record of every approval and change (spec section 4: "record approvals and changes").

  * each approval is recorded with time, schedule version and the version it replaced;
  * each entry references the schedule version it affected;
  * the entry is written in the same transaction as the approval: no approval without a
    record, and a refused approval leaves no record;
  * the record can be listed from the chat and from the command line.
Locks and absences (sprint 4) use the same table with their own action names.
"""
from __future__ import annotations

import pytest

from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.db.store import StaleState
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import ApprovalRefused, approve_draft, run_initial
from tests.test_mcp_server import call


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    assert import_workbook(s, small_input).status == "OK"
    return s


def draft(store, seed=0) -> str:
    return run_initial(store, 10, workers=2, seed=seed).run.schedule_version


def test_empty_log(store):
    assert store.list_audit() == []


def test_approval_is_recorded(store):
    v = draft(store)
    out = approve_draft(store, v)
    [e] = store.list_audit()
    assert e["entry_id"] == "LOG-0001" and e["action"] == "APPROVED"
    assert e["schedule_version"] == v and e["replaced_version"] is None
    assert e["at"] == out.approved_at                                   # same moment as the approval
    assert e["input_version"] == "IN-0001" and e["run_id"] == out.run_id
    assert e["details"]["total_cost"] == out.total_cost
    assert e["details"]["total_shortage_slots"] == out.total_shortage_slots


def test_second_approval_records_the_replaced_version_newest_first(store):
    first, second = draft(store), draft(store, seed=1)
    approve_draft(store, first)
    approve_draft(store, second)
    log = store.list_audit()
    assert [(e["schedule_version"], e["replaced_version"]) for e in log] == [(second, first), (first, None)]
    assert log[0]["at"] >= log[1]["at"]


def test_refused_approval_leaves_no_record(store, small_input):
    v = draft(store)
    import_workbook(store, small_input)
    with pytest.raises(ApprovalRefused):
        approve_draft(store, v)
    assert store.list_audit() == []


def test_failed_compare_and_set_leaves_no_record(store):
    first, second = draft(store), draft(store, seed=1)
    store.approve(first, expected_input="IN-0001", expected_active=None)
    with pytest.raises(StaleState):
        store.approve(second, expected_input="IN-0001", expected_active=None)
    assert [e["schedule_version"] for e in store.list_audit()] == [first]


def test_filter_and_limit(store):
    a, b, c = draft(store), draft(store, seed=1), draft(store, seed=2)
    for v in (a, b, c):
        approve_draft(store, v)
    assert [e["schedule_version"] for e in store.list_audit(limit=2)] == [c, b]
    assert [e["schedule_version"] for e in store.list_audit(schedule_version=b)] == [b]
    # The filter is by the version an entry affected; the replaced version is shown, not matched.
    assert [e["replaced_version"] for e in store.list_audit(schedule_version=c)] == [b]


def test_chat_tool_lists_changes(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    empty = call(srv, "list_changes", {}).structured_content
    assert empty["ok"] and empty["entries"] == []
    first, second = draft(store), draft(store, seed=1)
    call(srv, "approve_draft", {"ref": first})
    call(srv, "approve_draft", {"ref": second})
    s = call(srv, "list_changes", {}).structured_content
    assert [(e["action"], e["schedule_version"], e["replaced_version"]) for e in s["entries"]] == \
        [("APPROVED", second, first), ("APPROVED", first, None)]
    assert s["entries"][0]["at"] and s["entries"][0]["total_cost"] is not None
    one = call(srv, "list_changes", {"schedule_version": first}).structured_content
    assert [e["schedule_version"] for e in one["entries"]] == [first]
    bad = call(srv, "list_changes", {"schedule_version": "SV-0099"}).structured_content
    assert bad["ok"] is False and bad["error"]["code"] == "NOT_FOUND"
    assert call(srv, "list_changes", {"limit": 0}).is_error


def test_cli_log(store, capsys):
    v = draft(store)
    db = str(store.engine.url).removeprefix("sqlite:///")
    assert main(["--db", db, "log"]) == 0
    assert "no changes" in capsys.readouterr().out.lower()
    approve_draft(store, v)
    assert main(["--db", db, "log"]) == 0
    out = capsys.readouterr().out
    assert "LOG-0001" in out and "APPROVED" in out and v in out


def test_existing_database_gets_the_table(tmp_path, small_input):
    """A database created before this table existed is upgraded on open (create_all)."""
    import sqlite3
    path = tmp_path / "old.sqlite"
    s = Store(str(path))
    import_workbook(s, small_input)
    s.engine.dispose()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE audit_log")
    con.commit()
    con.close()
    reopened = Store(str(path))
    assert reopened.list_audit() == []
