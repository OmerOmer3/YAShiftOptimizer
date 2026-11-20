"""SCRUM-32: the MCP server skeleton, called in-process without a chat client.

Spec section 4: the server exposes defined operations, validates parameters and returns
reliable data; numeric answers come from the tools, never from the language model.
Business errors (not found, ambiguous, stale) are returned as a structured result with a
code, so the client can ask the manager; malformed parameters are rejected by the SDK.
"""
from __future__ import annotations

import anyio
import openpyxl
import pytest
from mcp.client.client import Client

from shiftopt.db import Store
from shiftopt.ingest import import_workbook
from shiftopt.mcp_server import build_server
from shiftopt.service import run_initial


def call(server, name: str, args: dict):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args)
    return anyio.run(go)


def list_tools(server):
    async def go():
        async with Client(server) as c:
            return (await c.list_tools()).tools
    return anyio.run(go)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "db.sqlite"))


@pytest.fixture
def solved(store, small_input):
    assert import_workbook(store, small_input).status == "OK"
    return run_initial(store, 20, workers=2).run


def test_tools_are_listed_with_descriptions_and_schemas(store, tmp_path):
    tools = {t.name: t for t in list_tools(build_server(store, export_dir=tmp_path))}
    assert {"get_system_status", "export_schedule", "resolve_reference"} <= tools.keys()
    for t in tools.values():
        assert t.description and len(t.description) > 20, t.name
        assert t.input_schema["type"] == "object"
        assert t.output_schema is not None, f"{t.name} must return typed, structured data"


def test_status_on_an_empty_database(store, tmp_path):
    r = call(build_server(store, export_dir=tmp_path), "get_system_status", {})
    assert not r.is_error
    s = r.structured_content
    assert s["ok"] is True and s["active_input_version"] is None and s["active_schedule_version"] is None
    assert s["run_count"] == 0


def test_status_numbers_equal_the_service_layer(store, solved, tmp_path):
    s = call(build_server(store, export_dir=tmp_path), "get_system_status", {}).structured_content
    assert s["active_input_version"] == store.active_input_version() == "IN-0001"
    assert s["run_count"] == 1
    last = s["latest_run"]
    assert last["run_id"] == solved.run_id and last["status"] == solved.status
    assert last["total_cost"] == solved.total_cost and last["total_shortage_slots"] == solved.total_shortage_slots
    assert last["schedule_version"] == solved.schedule_version and last["approval_status"] == "DRAFT"
    assert s["active_schedule_version"] is None   # a new run never activates a schedule (spec section 4)


def test_export_schedule_writes_the_file(store, solved, tmp_path):
    r = call(build_server(store, export_dir=tmp_path), "export_schedule", {"ref": solved.run_id})
    s = r.structured_content
    assert s["ok"] is True and s["error"] is None
    wb = openpyxl.load_workbook(s["path"])
    rs = {k: v for k, v in wb["RunSummary"].iter_rows(min_row=2, values_only=True)}
    assert rs["RunID"] == solved.run_id and rs["TotalCost"] == solved.total_cost == s["total_cost"]


def test_export_with_a_file_name(store, solved, tmp_path):
    s = call(build_server(store, export_dir=tmp_path), "export_schedule",
             {"ref": solved.schedule_version, "file_name": "week45.xlsx"}).structured_content
    assert s["ok"] and s["path"].endswith("week45.xlsx")


def test_export_rejects_a_path_outside_the_export_folder(store, solved, tmp_path):
    for bad in ("../x.xlsx", "/etc/x.xlsx", "sub/x.xlsx", "x.txt"):
        s = call(build_server(store, export_dir=tmp_path), "export_schedule",
                 {"ref": solved.run_id, "file_name": bad}).structured_content
        assert s["ok"] is False and s["error"]["code"] == "INVALID_PARAMETER", bad


def test_unknown_run_is_a_structured_not_found(store, solved, tmp_path):
    r = call(build_server(store, export_dir=tmp_path), "export_schedule", {"ref": "RUN-0999"})
    assert not r.is_error
    s = r.structured_content
    assert s["ok"] is False and s["path"] is None
    assert s["error"]["code"] == "NOT_FOUND" and "RUN-0999" in s["error"]["message"]


def test_malformed_parameters_are_rejected_before_the_tool_runs(store, tmp_path):
    srv = build_server(store, export_dir=tmp_path)
    assert call(srv, "export_schedule", {"file_name": 5}).is_error   # wrong type
    assert call(srv, "export_schedule", {"ref": 17}).is_error        # wrong type
    assert call(srv, "no_such_tool", {}).is_error


def test_server_instructions_keep_the_model_on_the_tools(store, tmp_path):
    """SCRUM-105: found in the demo. The assistant must not read files to answer, must resolve
    references with resolve_reference, and must say when a capability does not exist yet."""
    text = build_server(store, export_dir=tmp_path).instructions.lower()
    assert "resolve_reference" in text
    assert "do not open, read or ask for access to" in text
    assert "not available yet" in text
    tools = {t.name: t for t in list_tools(build_server(store, export_dir=tmp_path))}
    assert "before" in tools["resolve_reference"].description.lower()
