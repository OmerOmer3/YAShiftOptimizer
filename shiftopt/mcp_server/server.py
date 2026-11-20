"""MCP server for the chat client (spec section 4, SCRUM-32).

The server is a thin layer: each tool checks its parameters, calls the application
services (shiftopt.service, shiftopt.export, ...) and returns their numbers unchanged.
No calculation happens in the language model, and the engine still runs without the
chat client (shiftopt CLI), so optimization can be measured on its own.

Run it for a chat client over stdio:

    shiftopt-mcp --db shiftopt.db --export-dir exports

Tools are added per story: status and export here; import, solve and run status in
SCRUM-33; employee and coverage queries in SCRUM-34/35; approval in SCRUM-37.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from shiftopt.db import Store
from shiftopt.export import ExportError, export_run, resolve_run
from shiftopt.mcp_server.models import ExportResult, SystemStatus, brief

INSTRUCTIONS = """\
YAShiftOptimizer plans a customer-service department's weekly shifts.
Rules for the assistant:
- Every number you report (hours, costs, shortages, counts) must come from a tool result. Do not compute or estimate.
- Do not judge whether a schedule is valid; the server's independent checker does (ValidationPassed).
- If a request does not identify one employee, date or draft, or a tool returns AMBIGUOUS, ask the manager to choose.
- Creating a schedule makes a DRAFT. Nothing becomes the active schedule until the manager explicitly approves it.
- An empty value means 'not available', not zero; say so instead of reporting 0.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _safe_file_name(name: str) -> str | None:
    """A bare *.xlsx file name, so the client cannot write outside the export folder."""
    p = Path(name)
    if p.name != name or name in ("", ".", "..") or p.suffix.lower() != ".xlsx":
        return None
    return name


def build_server(store: Store, *, export_dir: str | Path = "exports") -> MCPServer:
    export_dir = Path(export_dir).resolve()
    mcp = MCPServer("YAShiftOptimizer", instructions=INSTRUCTIONS, version="0.3.0")

    @mcp.tool(annotations=READ_ONLY)
    def get_system_status() -> SystemStatus:
        """Where things stand: the active input version, the active (approved) schedule,
        how many runs exist and a summary of the latest run. Call this first when the
        manager asks a general question or refers to 'the draft' or 'the last run'."""
        runs = store.list_runs()
        return SystemStatus(active_input_version=store.active_input_version(),
                            active_schedule_version=store.active_schedule_version(),
                            run_count=len(runs), latest_run=brief(runs[-1]) if runs else None)

    @mcp.tool(annotations=WRITES_FILE)
    def export_schedule(ref: str, file_name: str | None = None) -> ExportResult:
        """Export a schedule to Excel in the course output schema (Schedule, Coverage,
        EmployeeSummary, RunSummary). ref is a run ID such as RUN-0003 or a schedule
        version such as SV-0003. file_name is optional, a bare name ending in .xlsx; the
        file is written to the server's export folder and its full path is returned.
        A run without a schedule is exported with headers only and its status."""
        name = _safe_file_name(file_name) if file_name else f"{ref.strip().upper()}.xlsx"
        if name is None:
            return ExportResult.fail("INVALID_PARAMETER",
                                     f"'{file_name}' is not a plain .xlsx file name (no folders).")
        try:
            path = export_run(store, ref, export_dir / name)
        except ExportError as e:
            runs = [r.run_id for r in store.list_runs()][-10:]
            msg = str(e)
            return ExportResult.fail("NOT_FOUND", msg[:1].upper() + msg[1:] + ".", candidates=runs)
        run = resolve_run(store, ref)
        return ExportResult(path=str(path), run_id=run.run_id, schedule_version=run.schedule_version,
                            solution_status=run.status,
                            total_cost=run.total_cost if run.schedule_version else None,
                            total_shortage_slots=run.total_shortage_slots if run.schedule_version else None)

    return mcp


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="shiftopt-mcp", description="YAShiftOptimizer MCP server (stdio)")
    p.add_argument("--db", default=os.environ.get("SHIFTOPT_DB", "shiftopt.db"),
                   help="SQLite file or SQLAlchemy URL (env SHIFTOPT_DB; default shiftopt.db)")
    p.add_argument("--export-dir", default=os.environ.get("SHIFTOPT_EXPORT_DIR", "exports"),
                   help="folder for exported workbooks (env SHIFTOPT_EXPORT_DIR; default exports)")
    a = p.parse_args(argv)
    build_server(Store(a.db), export_dir=a.export_dir).run("stdio")


if __name__ == "__main__":
    main()
