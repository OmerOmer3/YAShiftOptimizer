"""SCRUM-113: approve only on an explicit request from the manager, tied to one draft version.

Found in the sprint 3 review (SCRUM-107, item 7): with the client in Auto mode, the reply
"the first one" to a clarifying question was taken as an instruction to approve.

Spec section 3 (step 4): the active schedule changes only after the manager explicitly
approves the draft version. Spec section 4: "אשר את הטיוטה שהצגת" is an example request,
so the manager does not type version IDs; an unclear reference gets a clarifying question.

So: the server instructions say when approve_draft may be called (an explicit request; a
choice is not an approval; resolve and confirm first), and the tool itself accepts only an
exact version, so a vague reference can never approve anything.
"""
from __future__ import annotations

import pytest

from shiftopt.db import Store
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.mcp_server.server import INSTRUCTIONS
from shiftopt.service import run_initial
from tests.test_mcp_server import call, list_tools


@pytest.fixture
def store(tmp_path, small_input):
    s = Store(str(tmp_path / "db.sqlite"))
    import_workbook(s, small_input)
    return s


def test_instructions_state_the_approval_rule():
    text = " ".join(INSTRUCTIONS.split())
    assert "Call approve_draft only when the manager's own message asks to approve" in text
    assert "is a choice, not an approval" in text and "wait for a clear yes" in text
    assert "resolve_reference (kind 'draft')" in text and "accepts only an exact ID" in text
    assert "Never on your own initiative" in text


@pytest.mark.parametrize("ref", [None, "", "latest", "the first one", "first", "#1", "הראשונה", "the draft"])
def test_vague_references_never_approve(store, tmp_path, ref):
    v = run_initial(store, 10, workers=2, seed=0).run.schedule_version
    args = {} if ref is None else {"ref": ref}
    s = call(build_server(store, export_dir=tmp_path), "approve_draft", args).structured_content
    assert s["ok"] is False and s["error"]["code"] == "INVALID_PARAMETER"
    assert s["error"]["candidates"] == [v] and "resolve_reference" in s["error"]["message"]
    assert store.active_schedule_version() is None and store.list_audit() == []


@pytest.mark.parametrize("form", ["{sv}", " {sv_lower} ", "{run}"])
def test_an_exact_version_is_approved(store, tmp_path, form):
    run = run_initial(store, 10, workers=2, seed=0).run
    ref = form.format(sv=run.schedule_version, sv_lower=run.schedule_version.lower(), run=run.run_id)
    s = call(build_server(store, export_dir=tmp_path), "approve_draft", {"ref": ref}).structured_content
    assert s["ok"] and s["schedule_version"] == run.schedule_version
    assert store.active_schedule_version() == run.schedule_version


def test_approve_the_draft_you_showed(store, tmp_path):
    """The flow for 'אשר את הטיוטה שהצגת': resolve the reference, confirm, approve the exact ID."""
    v = run_initial(store, 10, workers=2, seed=0).run.schedule_version
    srv = build_server(store, export_dir=tmp_path)
    r = call(srv, "resolve_reference", {"kind": "draft", "text": ""}).structured_content
    assert r["ok"] and r["value"] == v                       # one draft: unambiguous
    s = call(srv, "approve_draft", {"ref": r["value"]}).structured_content
    assert s["ok"] and s["active_schedule_version"] == v


def test_two_drafts_need_a_choice_and_the_choice_alone_approves_nothing(store, tmp_path):
    a = run_initial(store, 10, workers=2, seed=0).run.schedule_version
    b = run_initial(store, 10, workers=2, seed=1).run.schedule_version
    srv = build_server(store, export_dir=tmp_path)
    r = call(srv, "resolve_reference", {"kind": "draft", "text": ""}).structured_content
    assert r["error"]["code"] == "AMBIGUOUS"                  # the assistant must ask which
    r = call(srv, "resolve_reference", {"kind": "draft", "text": "the first one"}).structured_content
    assert r["value"] == a                                    # the manager's choice resolves...
    assert store.active_schedule_version() is None            # ...but nothing is approved by it
    assert call(srv, "approve_draft", {"ref": b}).structured_content["ok"]


def test_tool_description_and_annotation(store, tmp_path):
    tool = {t.name: t for t in list_tools(build_server(store, export_dir=tmp_path))}["approve_draft"]
    assert "a reply that only picks a draft is not an approval" in " ".join(tool.description.split())
    assert tool.annotations.destructive_hint is True          # clients ask before running it
