"""MCP server for the chat client (spec section 4, SCRUM-32).

The server is a thin layer: each tool checks its parameters, calls the application
services (shiftopt.service, shiftopt.export, ...) and returns their numbers unchanged.
No calculation happens in the language model, and the engine still runs without the
chat client (shiftopt CLI), so optimization can be measured on its own.

Run it for a chat client over stdio:

    shiftopt-mcp --db shiftopt.db --export-dir exports

Tools are added per story: status and export (SCRUM-32/44); import, solve and run status
(SCRUM-33); resolving employees, dates and drafts (SCRUM-36);
employee and coverage queries (SCRUM-34/35); approval (SCRUM-37); the change log (SCRUM-24).
"""
from __future__ import annotations

import argparse
import os
import re
from functools import partial
from pathlib import Path
from typing import Annotated, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from shiftopt.db import Store
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.export import ExportError, export_run, resolve_run
from shiftopt.ingest import import_workbook, read_input
from shiftopt.mcp_server.models import (
    ApprovalResult,
    AuditEntry,
    AuditResult,
    CoverageResult,
    DayOut,
    ExportResult,
    ImportResult,
    InputIssueOut,
    ResolveResult,
    RunResult,
    ShiftOut,
    SlotOut,
    SystemStatus,
    brief,
)
from shiftopt.service import NoActiveInput, run_initial
from shiftopt.service.approve import ApprovalRefused, approve_draft as approve_service
from shiftopt.service.queries import get_coverage as coverage_service
from shiftopt.service.resolve import Unresolved, date_label, resolve_date, resolve_draft, resolve_employee

MAX_TIME_BUDGET_S = 3600
_RUN_ID = re.compile(r"^RUN-\d+$")

INSTRUCTIONS = """\
YAShiftOptimizer plans a customer-service department's weekly shifts.
Rules for the assistant:
- Every number you report (hours, costs, shortages, counts) must come from a tool result. Do not compute or estimate.
- Do not judge whether a schedule is valid; the server's independent checker does (ValidationPassed).
- Whenever the manager names an employee, a date or day, or a draft, call resolve_reference first. If it returns
  AMBIGUOUS or NOT_FOUND, show the candidates and ask the manager to choose. Never pick one yourself.
- Do not open, read or ask for access to the input or exported Excel files to answer questions; the tools are the
  only source. If no tool answers a question, say that this is not available yet instead of looking elsewhere.
- Creating a schedule makes a DRAFT. Nothing becomes the active schedule until the manager explicitly approves it.
  Call approve_draft only when the manager has clearly asked to approve that draft; never on your own initiative.
- An empty value means 'not available', not zero; say so instead of reporting 0.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CREATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# Approval replaces the active schedule (the old one is kept in history): flagged so clients confirm.
CHANGES_ACTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


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
    def export_schedule(ref: str | None = None, file_name: str | None = None) -> ExportResult:
        """Export a schedule to Excel in the course output schema (Schedule, Coverage,
        EmployeeSummary, RunSummary). ref is a schedule version (SV-0003), a run ID
        (RUN-0003) or 'latest'; leave it empty for 'the draft'. If that is ambiguous
        (several drafts) the answer is AMBIGUOUS with the drafts to choose from, and no
        file is written. file_name is optional, a bare name ending in .xlsx; the file is
        written to the server's export folder and its full path is returned."""
        if file_name and _safe_file_name(file_name) is None:
            return ExportResult.fail("INVALID_PARAMETER",
                                     f"'{file_name}' is not a plain .xlsx file name (no folders).")
        try:
            # A run ID is exported as that run, even if it produced no schedule (headers only).
            target = ref.strip().upper() if ref and _RUN_ID.match(ref.strip().upper()) else resolve_draft(store, ref)
            run = resolve_run(store, target)
            name = file_name or f"{target}.xlsx"
            path = export_run(store, target, export_dir / name)
        except Unresolved as e:
            return ExportResult.fail(e.code, e.message, candidates=e.candidates)
        except ExportError as e:
            msg = str(e)
            return ExportResult.fail("NOT_FOUND", msg[:1].upper() + msg[1:] + ".",
                                     candidates=[r.run_id for r in store.list_runs()][-10:])
        return ExportResult(path=str(path), run_id=run.run_id, schedule_version=run.schedule_version,
                            solution_status=run.status,
                            total_cost=run.total_cost if run.schedule_version else None,
                            total_shortage_slots=run.total_shortage_slots if run.schedule_version else None)

    @mcp.tool(annotations=READ_ONLY)
    def resolve_reference(kind: Literal["employee", "date", "draft"], text: str) -> ResolveResult:
        """Call this before acting whenever the manager names an employee, a date or a draft.
        kind 'employee': an ID or a name; 'date': a date or a day name such as 'Wednesday' or 'רביעי', resolved only
        within the planning week; 'draft': SV-0003, RUN-0003, 'latest', a number from the
        list of drafts ('#1', 'first'; oldest first) or empty. Returns
        exactly one value, or AMBIGUOUS / NOT_FOUND with candidates: then ask the manager
        to choose and do not guess. Changes nothing."""
        try:
            if kind == "draft":
                v = resolve_draft(store, text)
                info = store.schedule_version_info(v)
                return ResolveResult(kind=kind, value=v, label=f"{v} ({info['run_id']}, {info['approval_status']})")
            data = store.load_input()
            if data is None:
                return ResolveResult.fail("NO_ACTIVE_INPUT", "No input has been imported yet.")
            if kind == "employee":
                e = resolve_employee(data, text)
                return ResolveResult(kind=kind, value=e, label=f"{e} ({data.employees[e].name})")
            d = resolve_date(data, text)
            return ResolveResult(kind=kind, value=d.isoformat(), label=date_label(d))
        except Unresolved as e:
            return ResolveResult.fail(e.code, e.message, candidates=e.candidates, kind=kind)

    @mcp.tool(annotations=CREATES)
    def import_input(file_path: str) -> ImportResult:
        """Import the week's input workbook (schema 1.0) from a path on this computer and make
        it the active input. The whole file is checked first; if anything is wrong the result
        is INVALID_INPUT with every problem by sheet, row and column, nothing is stored, and the
        previously active input stays in force. Ask the manager for the full path if unsure."""
        path = Path(file_path).expanduser()
        if path.suffix.lower() != ".xlsx":
            return ImportResult.fail("INVALID_PARAMETER", f"'{path.name}' is not an .xlsx workbook.",
                                     active_input_version=store.active_input_version())
        if not path.is_file():
            return ImportResult.fail("NOT_FOUND", f"No file at {path}.",
                                     active_input_version=store.active_input_version())
        out = import_workbook(store, path)
        if out.status != "OK":
            issues = [InputIssueOut(sheet=e.sheet, row=e.row, column=e.column, code=e.code, message=e.message)
                      for e in out.errors]
            return ImportResult.fail("INVALID_INPUT",
                                     f"The file has {len(issues)} problem(s); nothing was imported.",
                                     active_input_version=store.active_input_version(),
                                     file_name=path.name, issues=issues)
        data = store.load_input(out.input_version)
        return ImportResult(input_version=out.input_version, active_input_version=out.input_version,
                            file_name=path.name, week_start=data.week_start.isoformat(),
                            employee_count=len(data.employees), demand_hours=len(data.demand),
                            shift_templates=len(data.templates))

    @mcp.tool(annotations=CREATES)
    async def create_schedule(
        time_budget_seconds: Annotated[float, Field(gt=0, le=MAX_TIME_BUDGET_S,
                                                    description="Total seconds for all solver stages")],
        workers: Annotated[int, Field(ge=1, le=64)] = DEFAULT_WORKERS,
        seed: int = DEFAULT_SEED,
    ) -> RunResult:
        """Create a schedule for the active input within the given time budget. Coverage
        shortage is minimized first, then cost. The result is checked by the independent
        checker and saved as a DRAFT; the active schedule does not change until the manager
        approves. A week with no possible schedule returns status INFEASIBLE with reasons
        in diagnostics; that is a valid result, not a tool error. Returns the run ID."""
        try:
            outcome = await anyio.to_thread.run_sync(
                partial(run_initial, store, time_budget_seconds, workers=workers, seed=seed))
        except NoActiveInput as e:
            return RunResult.fail("NO_ACTIVE_INPUT", f"{e}. Import the week's file first (import_input).")
        return RunResult(run=brief(outcome.run), active_schedule_version=store.active_schedule_version())

    @mcp.tool(annotations=READ_ONLY)
    def get_run_status(run_id: str | None = None) -> RunResult:
        """Status and result of a run: solution status, shortage, cost, which objectives were
        proven optimal, timings, the draft it created and any diagnostics. Without run_id,
        the latest run."""
        runs = store.list_runs()
        if not runs:
            return RunResult.fail("NOT_FOUND", "No runs yet. Create a schedule first (create_schedule).")
        if run_id is None:
            run = runs[-1]
        else:
            ref = run_id.strip().upper()
            run = store.get_run(ref) if _RUN_ID.match(ref) else None
            if run is None:
                return RunResult.fail("NOT_FOUND", f"Run {run_id} not found.",
                                      candidates=[r.run_id for r in runs][-10:])
        return RunResult(run=brief(run), active_schedule_version=store.active_schedule_version())

    @mcp.tool(annotations=CHANGES_ACTIVE)
    def approve_draft(ref: str | None = None) -> ApprovalResult:
        """Approve a draft so that it becomes the active schedule. Only when the manager has
        clearly asked to approve it. ref: SV-0003, RUN-0003, 'latest', a number from the
        list of drafts, or empty for 'the draft' (AMBIGUOUS if there are several: ask which).
        The server first checks that the draft's input is still the current input and that
        the schedule passes the independent checker; otherwise nothing changes and the
        reason is returned. The previously active schedule is kept in history."""
        try:
            version = resolve_draft(store, ref)
            out = approve_service(store, version)
        except Unresolved as e:
            return ApprovalResult.fail(e.code, e.message, candidates=e.candidates,
                                       active_schedule_version=store.active_schedule_version())
        except ApprovalRefused as e:
            return ApprovalResult.fail(e.code, e.message, details=e.details,
                                       active_schedule_version=store.active_schedule_version())
        return ApprovalResult(schedule_version=out.schedule_version, run_id=out.run_id,
                              approved_at=out.approved_at.isoformat(timespec="seconds"),
                              replaced_version=out.replaced_version, total_cost=out.total_cost,
                              total_shortage_slots=out.total_shortage_slots,
                              active_schedule_version=store.active_schedule_version())

    @mcp.tool(annotations=READ_ONLY)
    def list_changes(limit: Annotated[int, Field(ge=1, le=200)] = 20,
                     schedule_version: str | None = None) -> AuditResult:
        """The record of changes to the schedule, newest first: each approval with its time,
        the version approved and the version it replaced (later also locks and absences).
        Optionally only the entries that affected one schedule version (SV-0003 or RUN-0003)."""
        version = None
        if schedule_version:
            try:
                version = resolve_draft(store, schedule_version)
            except Unresolved as e:
                return AuditResult.fail(e.code, e.message, candidates=e.candidates)
        entries = [AuditEntry(entry_id=e["entry_id"], at=e["at"].isoformat(timespec="seconds"), action=e["action"],
                              schedule_version=e["schedule_version"], replaced_version=e["replaced_version"],
                              input_version=e["input_version"], run_id=e["run_id"],
                              total_cost=e["details"].get("total_cost"),
                              total_shortage_slots=e["details"].get("total_shortage_slots"))
                   for e in store.list_audit(limit=limit, schedule_version=version)]
        return AuditResult(entries=entries)

    @mcp.tool(annotations=READ_ONLY)
    def get_coverage(ref: str | None = None, day: str | None = None) -> CoverageResult:
        """Where a schedule is understaffed and what it costs: total shortage (half-hours and
        hours), surplus and cost; shortage, surplus and cost per day; every understaffed
        half-hour. With day (a date or a day name such as 'Wednesday' or 'רביעי'): also every
        half-hour of that day and who works that day, with shifts and breaks. Use this for
        'what is planned on <day>'. Days outside the planning week are refused with the valid
        dates. ref: a schedule version or run; empty means the active schedule, or the only
        draft if nothing is active (AMBIGUOUS if several: ask which)."""
        try:
            info, r = coverage_service(store, ref, day)
        except Unresolved as e:
            return CoverageResult.fail(e.code, e.message, candidates=e.candidates)

        def slot(x):
            return SlotOut(date=x.date.isoformat(), slot_start=x.slot_start, slot_end=x.slot_end,
                           required=x.required, assigned=x.assigned, shortage=x.shortage, surplus=x.surplus)
        return CoverageResult(
            schedule_version=info["schedule_version"], approval_status=info["approval_status"],
            is_active=info["is_active"], total_shortage_slots=r.total_shortage_slots,
            total_shortage_hours=r.total_shortage_hours, total_surplus_slots=r.total_surplus_slots,
            total_cost=r.total_cost,
            days=[DayOut(date=d.date.isoformat(), weekday=d.weekday, required_slots=d.required_slots,
                         shortage_slots=d.shortage_slots, surplus_slots=d.surplus_slots, cost=d.cost,
                         shift_count=d.shift_count) for d in r.days],
            shortage_slots=[slot(x) for x in r.shortage_slots],
            day=None if r.day is None else r.day.isoformat(),
            day_slots=[slot(x) for x in r.day_slots],
            day_shifts=[ShiftOut(employee_id=x.employee_id, name=x.name, shift_id=x.shift_id, start=x.start,
                                 end=x.end, breaks=x.breaks, paid_hours=x.paid_hours, cost=x.cost)
                        for x in r.day_shifts])

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
