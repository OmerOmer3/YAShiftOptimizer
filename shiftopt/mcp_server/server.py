"""MCP server for the chat client (spec section 4, SCRUM-32).

The server is a thin layer: each tool checks its parameters, calls the application
services (shiftopt.service, shiftopt.export, ...) and returns their numbers unchanged.
No calculation happens in the language model, and the engine still runs without the
chat client (shiftopt CLI), so optimization can be measured on its own.

Run it for a chat client over stdio:

    shiftopt-mcp --db shiftopt.db --export-dir exports

Tools are added per story: status and export (SCRUM-32/44); import, solve and run status
(SCRUM-33); resolving employees, dates and drafts (SCRUM-36);
employee and coverage queries (SCRUM-34/35); approval (SCRUM-37); the change log (SCRUM-24);
locking and unlocking a future shift (SCRUM-39); reporting an absence (SCRUM-40); repairing it with a single replacement (SCRUM-41) or by re-optimizing future shifts (SCRUM-42).
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

from shiftopt import clock
from shiftopt.db import Store
from shiftopt.domain import DAY_NAMES, day_of_week
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.export import ExportError, export_run, resolve_run
from shiftopt.ingest import import_workbook, read_input
from shiftopt.mcp_server.models import (
    AbsenceResult,
    ApprovalResult,
    AuditEntry,
    AuditResult,
    CoverageResult,
    DayOut,
    EmployeeResult,
    EmployeeShiftOut,
    ExportResult,
    ImportResult,
    InputIssueOut,
    LockResult,
    RepairResult,
    ReplacementOut,
    ResolveResult,
    RunResult,
    ShiftOut,
    SlotOut,
    SystemStatus,
    brief,
)
from shiftopt.service import NoActiveInput, run_initial
from shiftopt.service.approve import ApprovalRefused, approve_draft as approve_service
from shiftopt.service.absence import AbsenceRefused, report_absence as absence_service
from shiftopt.service.locks import LockRefused, set_lock
from shiftopt.service.repair import run_repair
from shiftopt.service.queries import get_coverage as coverage_service
from shiftopt.service.queries import get_employee as employee_service
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
- When the manager reports that an employee will be absent, call report_absence. It records the absence only; the
  active schedule does not change until a repair draft is approved. Then call repair_schedule to propose the repair,
  and show the manager who replaces whom and the draft's numbers before asking whether to approve it.
- Lock or unlock a shift only when the manager asks to. Locks belong to shifts of the active (approved) schedule
  that have not started; a locked shift is kept as it is by every later repair.
- An empty value means 'not available', not zero; say so instead of reporting 0.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES_FILE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CREATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# Approval replaces the active schedule (the old one is kept in history): flagged so clients confirm.
# Locking changes a flag on the active schedule and is recorded; doing it twice changes nothing more.
CHANGES_LOCK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CHANGES_ACTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def _safe_file_name(name: str) -> str | None:
    """A bare *.xlsx file name, so the client cannot write outside the export folder."""
    p = Path(name)
    if p.name != name or name in ("", ".", "..") or p.suffix.lower() != ".xlsx":
        return None
    return name


def build_server(store: Store, *, export_dir: str | Path = "exports") -> MCPServer:
    export_dir = Path(export_dir).resolve()
    mcp = MCPServer("YAShiftOptimizer", instructions=INSTRUCTIONS, version="0.4.0")

    @mcp.tool(annotations=READ_ONLY)
    def get_system_status() -> SystemStatus:
        """Where things stand: the active input version, the active (approved) schedule,
        how many runs exist and a summary of the latest run. Call this first when the
        manager asks a general question or refers to 'the draft' or 'the last run'."""
        runs = store.list_runs()
        return SystemStatus(now=clock.now().isoformat(timespec="minutes"),
                            active_input_version=store.active_input_version(),
                            active_schedule_version=store.active_schedule_version(),
                            run_count=len(runs), latest_run=brief(runs[-1]) if runs else None,
                            open_absences=[f"{a['absence_id']} {a['employee_id']} {a['date'].isoformat()}"
                                           for a in store.list_absences(status="OPEN")])

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
        the version approved and the version it replaced, and each lock, unlock and reported
        absence with the employee and date.
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
                              total_shortage_slots=e["details"].get("total_shortage_slots"),
                              employee_id=e["details"].get("employee_id"), date=e["details"].get("date"),
                              shift_id=e["details"].get("shift_id"), absence_id=e["details"].get("absence_id"))
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

    @mcp.tool(annotations=READ_ONLY)
    def get_employee_schedule(employee: str, ref: str | None = None) -> EmployeeResult:
        """One employee's week: every shift with its date, times and breaks, paid hours and
        cost per shift and in total, preferred shift start, unavailable dates and whether the
        employee is exempt from the minimum number of shifts. Use this for 'how many hours
        does <employee> have' or 'when does <employee> work'. employee: an ID ('E003', 'E3',
        '3') or a name, resolved by the server; AMBIGUOUS or NOT_FOUND come with candidates:
        ask which. ref: a schedule version or run; empty means the active schedule, or the
        only draft if nothing is active. The answer says which version it is about."""
        try:
            info, r = employee_service(store, employee, ref)
        except Unresolved as e:
            return EmployeeResult.fail(e.code, e.message, candidates=e.candidates)
        return EmployeeResult(
            schedule_version=info["schedule_version"], approval_status=info["approval_status"],
            is_active=info["is_active"], employee_id=r.employee_id, name=r.name, shift_count=r.shift_count,
            paid_hours=r.paid_hours, total_cost=r.total_cost, preferred_start=r.preferred_start,
            preferred_count=r.preferred_count, preference_rate=r.preference_rate,
            min_shifts_exempt=r.min_shifts_exempt, unavailable_dates=r.unavailable_dates,
            shifts=[EmployeeShiftOut(date=x.date.isoformat(), weekday=x.weekday, shift_id=x.shift_id, start=x.start,
                                     end=x.end, breaks=x.breaks, paid_hours=x.paid_hours, cost=x.cost,
                                     is_locked=x.is_locked) for x in r.shifts])

    def _lock(employee: str, date: str, locked: bool) -> LockResult:
        active = store.active_schedule_version()
        if active is None:
            return LockResult.fail("NOT_FOUND", "There is no active schedule yet. Locks apply to the approved "
                                                "schedule; approve a draft first.")
        data = store.load_input(store.schedule_version_info(active)["input_version"])
        try:
            eid = resolve_employee(data, employee)
            d = resolve_date(data, date)
            out = set_lock(store, eid, d, locked)
        except Unresolved as e:
            return LockResult.fail(e.code, e.message, candidates=e.candidates, schedule_version=active)
        except LockRefused as e:
            return LockResult.fail(e.code, e.message, schedule_version=active)
        return LockResult(schedule_version=out.schedule_version, employee_id=eid, name=data.employees[eid].name,
                          date=d.isoformat(), weekday=DAY_NAMES.get(day_of_week(d)), shift_id=out.shift_id,
                          start=out.start, end=out.end, is_locked=out.is_locked, changed=out.changed,
                          now=out.now.isoformat(timespec="minutes"))

    @mcp.tool(annotations=CHANGES_LOCK)
    def lock_shift(employee: str, date: str) -> LockResult:
        """Lock one shift of the active (approved) schedule so that later repairs keep it exactly as
        it is, breaks included. Only when the manager asks to lock it. employee: an ID or a name;
        date: a date or a day name in the planning week; both resolved by the server (AMBIGUOUS
        or NOT_FOUND come with candidates: ask which). Refused with SHIFT_STARTED if the shift
        has already started. Recorded in the change log; no new schedule version is created."""
        return _lock(employee, date, True)

    @mcp.tool(annotations=CHANGES_LOCK)
    def unlock_shift(employee: str, date: str) -> LockResult:
        """Remove the lock from one shift of the active schedule, so that a later repair may change
        it. Only when the manager asks to unlock it. Same parameters and checks as lock_shift."""
        return _lock(employee, date, False)

    @mcp.tool(annotations=CREATES)
    def report_absence(employee: str, date: str) -> AbsenceResult:
        """Record that an employee will be absent for a whole day, before that shift starts. employee:
        an ID or a name; date: a date or a day name in the planning week; both resolved by the server
        (AMBIGUOUS or NOT_FOUND come with candidates: ask which). The shift is removed in the repair
        draft even if it is locked, and the employee becomes exempt from the 4-shift minimum this
        week. If the employee has no shift that day, nothing is recorded and the message says so.
        Refused with SHIFT_STARTED if the shift has started. The active schedule does not change."""
        active = store.active_schedule_version()
        if active is None:
            return AbsenceResult.fail("NOT_FOUND", "There is no active schedule yet; approve a draft first.")
        data = store.load_input(store.schedule_version_info(active)["input_version"])
        try:
            eid = resolve_employee(data, employee)
            d = resolve_date(data, date)
            out = absence_service(store, eid, d)
        except Unresolved as e:
            return AbsenceResult.fail(e.code, e.message, candidates=e.candidates, schedule_version=active)
        except AbsenceRefused as e:
            return AbsenceResult.fail(e.code, e.message, schedule_version=active)
        return AbsenceResult(absence_id=out.absence_id, created=out.created, employee_id=eid,
                             name=data.employees[eid].name, date=d.isoformat(), weekday=DAY_NAMES.get(day_of_week(d)),
                             shift_id=out.shift_id, start=out.start, end=out.end,
                             was_locked=out.was_locked if out.absence_id else None,
                             min_shifts_exempt=True if out.absence_id else None,
                             schedule_version=out.schedule_version, message=out.message)

    @mcp.tool(annotations=CREATES)
    async def repair_schedule(
        time_budget_seconds: Annotated[float, Field(gt=0, le=MAX_TIME_BUDGET_S,
                                                    description="Total seconds for all repair stages")] = 30,
        workers: Annotated[int, Field(ge=1, le=64)] = DEFAULT_WORKERS,
        seed: int = DEFAULT_SEED,
    ) -> RepairResult:
        """Propose a repair of the active schedule for the reported absences (report_absence first).
        First one available employee takes the same shift, keeping every rule (mode
        SINGLE_REPLACEMENT). If nobody can, the future shifts are re-optimized: least shortage,
        then fewest changed (employee, date) pairs, then lowest cost (mode REOPTIMIZED). Started,
        completed and locked shifts never change. The result is checked by the independent checker
        and saved as a REPAIR draft; the active schedule changes only when the manager approves
        it. If no repair exists, nothing changes and the absence stays open."""
        active = store.active_schedule_version()
        try:
            out = await anyio.to_thread.run_sync(
                partial(run_repair, store, time_budget_seconds, workers=workers, seed=seed))
        except AbsenceRefused as e:
            return RepairResult.fail(e.code, e.message, active_schedule_version=active)
        data = store.load_input(out.start.input_version)
        return RepairResult(
            run=None if out.run is None else brief(out.run), mode=out.mode,
            changed_assignments=out.changed_assignments,
            replacements=[ReplacementOut(absent_employee=r.absent_employee, date=r.date.isoformat(),
                                         shift_id=r.shift_id, replacement_employee=r.replacement_employee,
                                         replacement_name=data.employees[r.replacement_employee].name)
                          for r in out.replacements],
            moved_breaks=out.moved_breaks,
            open_absences=[f"{a['absence_id']} {a['employee_id']} {a['date'].isoformat()}" for a in out.start.absences],
            active_schedule_version=store.active_schedule_version(), message=out.message)

    return mcp


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="shiftopt-mcp", description="YAShiftOptimizer MCP server (stdio)")
    p.add_argument("--db", default=os.environ.get("SHIFTOPT_DB", "shiftopt.db"),
                   help="SQLite file or SQLAlchemy URL (env SHIFTOPT_DB; default shiftopt.db)")
    p.add_argument("--export-dir", default=os.environ.get("SHIFTOPT_EXPORT_DIR", "exports"),
                   help="folder for exported workbooks (env SHIFTOPT_EXPORT_DIR; default exports)")
    p.add_argument("--now", default=None,
                   help="fixed current time for demos, e.g. 2026-11-03T07:00 (env SHIFTOPT_NOW; "
                        "default: the computer's clock)")
    a = p.parse_args(argv)
    if a.now:
        clock.set_now(a.now)
    build_server(Store(a.db), export_dir=a.export_dir).run("stdio")


if __name__ == "__main__":
    main()
