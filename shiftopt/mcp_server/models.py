"""Typed results shared by all MCP tools (SCRUM-32).

Every tool returns a Pydantic model, so the chat client receives structured data with a
published output schema. Business errors are part of the result, not an exception:

    {"ok": false, "error": {"code": "AMBIGUOUS", "message": "...", "candidates": ["E003", "E013"]}}

so the client can show the message or ask the manager to choose (spec section 4,
"ask for clarification"). Malformed parameters never reach a tool: the SDK validates them
against the input schema and returns an MCP error.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ErrorCode = Literal[
    "NOT_FOUND",            # the run, version, employee or date does not exist
    "AMBIGUOUS",            # more than one match; see candidates (SCRUM-36)
    "INVALID_PARAMETER",    # well-typed but not acceptable (e.g. a path outside the export folder)
    "INVALID_INPUT",        # the workbook failed the schema checks (spec section 9)
    "NO_ACTIVE_INPUT",      # nothing has been imported yet
    "BASE_VERSION_STALE",   # approval refused: the draft's base is no longer active (SCRUM-38)
    "INPUT_VERSION_STALE",  # approval refused: a newer input was imported (SCRUM-38)
    "VALIDATION_FAILED",    # the independent checker rejected the schedule
    "ALREADY_APPROVED",     # approval refused: the version is not a draft
    "CONFLICT",             # the active schedule changed while approving; nothing was changed
    "SHIFT_STARTED",        # the shift has started or ended; it can no longer be locked or changed (SCRUM-39)
    "LOCK_VIOLATED",        # approval refused: a repair draft changes a locked shift (SCRUM-39)
    "INTERNAL_ERROR",
]


class ToolErrorInfo(BaseModel):
    code: ErrorCode
    message: str = Field(description="One sentence for the manager, in plain words")
    candidates: list[str] = Field(default_factory=list,
                                  description="When code is AMBIGUOUS or NOT_FOUND: the valid choices")


class ToolResult(BaseModel):
    ok: bool = True
    error: ToolErrorInfo | None = None

    @classmethod
    def fail(cls, code: ErrorCode, message: str, candidates: list[str] | None = None, **fields):
        return cls(ok=False, error=ToolErrorInfo(code=code, message=message, candidates=candidates or []), **fields)


class RunBrief(BaseModel):
    """A run as the client needs it. Empty fields mean 'not available', never zero."""
    run_id: str
    input_version: str
    run_type: str
    status: str = Field(description="OPTIMAL, FEASIBLE, INFEASIBLE, NO_SOLUTION_FOUND, ERROR")
    termination_reason: str
    total_shortage_slots: int | None = Field(description="Half-hours of missing employees; half of it is employee-hours")
    total_cost: float | None
    coverage_proven: bool | None
    cost_proven: bool | None
    validation_passed: bool | None
    time_budget_seconds: float
    first_feasible_seconds: float | None = Field(description="Empty if no solution was found")
    optimization_seconds: float
    schedule_version: str | None = Field(description="The DRAFT created by this run; empty if no schedule")
    approval_status: str | None
    diagnostics: list[str] = Field(default_factory=list,
                                   description="Plain-language reasons, e.g. why a week has no solution")
    solver_info: dict = Field(default_factory=dict, description="Solver version, threads, seed and machine")
    base_schedule_version: str | None = Field(None, description="Repair runs: the schedule this repair is based on")
    stability_proven: bool | None = Field(None, description="Repair runs: number of changes proven minimal")


class SystemStatus(ToolResult):
    now: str | None = Field(None, description="The current time the server uses to decide which shifts have "
                                              "started (ISO); fixed for demos, otherwise the computer's clock")
    active_input_version: str | None = None
    active_schedule_version: str | None = Field(None, description="The approved schedule in force, if any")
    open_absences: list[str] = Field(default_factory=list,
                                     description="Reported absences without an approved repair yet "
                                                 "('AB-0001 E003 2026-11-04')")
    run_count: int = 0
    latest_run: RunBrief | None = None


class ExportResult(ToolResult):
    path: str | None = Field(None, description="Absolute path of the written .xlsx on the server's computer")
    run_id: str | None = None
    schedule_version: str | None = None
    solution_status: str | None = None
    total_cost: float | None = None
    total_shortage_slots: int | None = None


def brief(run) -> RunBrief:
    return RunBrief(
        run_id=run.run_id, input_version=run.input_version, run_type=run.run_type, status=run.status,
        termination_reason=run.termination_reason, total_shortage_slots=run.total_shortage_slots,
        total_cost=run.total_cost, coverage_proven=run.coverage_proven, cost_proven=run.cost_proven,
        validation_passed=run.validation_passed, time_budget_seconds=run.time_budget_seconds,
        first_feasible_seconds=None if run.first_feasible_seconds is None else round(run.first_feasible_seconds, 3),
        optimization_seconds=round(run.optimization_seconds, 3),
        schedule_version=run.schedule_version, approval_status=run.approval_status,
        diagnostics=list(run.diagnostics), solver_info=dict(run.solver_info),
        base_schedule_version=run.base_schedule_version, stability_proven=run.stability_proven)


class InputIssueOut(BaseModel):
    sheet: str
    row: int | None = Field(description="Excel row number; the header is row 1. Empty for sheet-level problems")
    column: str | None
    code: str
    message: str


class ImportResult(ToolResult):
    input_version: str | None = Field(None, description="The new active input version; empty if the file was rejected")
    active_input_version: str | None = Field(None, description="The input version in force after this call")
    file_name: str | None = None
    week_start: str | None = None
    employee_count: int | None = None
    demand_hours: int | None = None
    shift_templates: int | None = None
    issues: list[InputIssueOut] = Field(default_factory=list, description="Every problem found, by sheet, row and column")


class RunResult(ToolResult):
    run: RunBrief | None = None
    active_schedule_version: str | None = Field(None, description="Unchanged by a new run (spec section 4)")


class ResolveResult(ToolResult):
    kind: str | None = None
    value: str | None = Field(None, description="The one matching ID: employee ID, ISO date or schedule version")
    label: str | None = Field(None, description="Human-readable form to confirm with the manager")


class ApprovalResult(ToolResult):
    schedule_version: str | None = None
    run_id: str | None = None
    approved_at: str | None = Field(None, description="ISO time of the approval")
    replaced_version: str | None = Field(None, description="The schedule that was active before; empty if none")
    total_cost: float | None = None
    total_shortage_slots: int | None = None
    active_schedule_version: str | None = Field(None, description="The active schedule after this call")
    details: list[str] = Field(default_factory=list, description="Checker violations when VALIDATION_FAILED")


class AuditEntry(BaseModel):
    entry_id: str
    at: str = Field(description="ISO time of the change")
    action: str = Field(description="APPROVED, LOCKED, UNLOCKED or ABSENCE_REPORTED")
    schedule_version: str = Field(description="The schedule version the change affected")
    replaced_version: str | None = Field(description="For APPROVED: the schedule that was active before")
    input_version: str | None
    run_id: str | None
    total_cost: float | None = None
    total_shortage_slots: int | None = None
    employee_id: str | None = Field(None, description="For LOCKED, UNLOCKED and ABSENCE_REPORTED: whose shift")
    date: str | None = Field(None, description="For LOCKED, UNLOCKED and ABSENCE_REPORTED: the date")
    absence_id: str | None = None
    shift_id: str | None = None


class AuditResult(ToolResult):
    entries: list[AuditEntry] = Field(default_factory=list, description="Newest first")


class SlotOut(BaseModel):
    date: str
    slot_start: str
    slot_end: str
    required: int
    assigned: int = Field(description="Employees working and not on a break")
    shortage: int
    surplus: int


class DayOut(BaseModel):
    date: str
    weekday: str
    required_slots: int = Field(description="Sum of required employees over the day's half-hours")
    shortage_slots: int
    surplus_slots: int
    cost: float
    shift_count: int


class ShiftOut(BaseModel):
    employee_id: str
    name: str
    shift_id: str
    start: str
    end: str
    breaks: list[str]
    paid_hours: float
    cost: float


class CoverageResult(ToolResult):
    schedule_version: str | None = None
    approval_status: str | None = None
    is_active: bool | None = None
    total_shortage_slots: int | None = Field(None, description="Missing employee half-hours over the week")
    total_shortage_hours: float | None = Field(None, description="total_shortage_slots / 2")
    total_surplus_slots: int | None = None
    total_cost: float | None = None
    days: list[DayOut] = Field(default_factory=list)
    shortage_slots: list[SlotOut] = Field(default_factory=list, description="Every half-hour with a shortage")
    day: str | None = Field(None, description="The day asked about, if any")
    day_slots: list[SlotOut] = Field(default_factory=list, description="All half-hours of that day")
    day_shifts: list[ShiftOut] = Field(default_factory=list, description="Who works that day")


class EmployeeShiftOut(BaseModel):
    date: str
    weekday: str
    shift_id: str
    start: str
    end: str
    breaks: list[str] = Field(description="Break times, start-end; an employee on a break is not counted as working")
    paid_hours: float
    cost: float
    is_locked: bool


class EmployeeResult(ToolResult):
    schedule_version: str | None = None
    approval_status: str | None = None
    is_active: bool | None = Field(None, description="True if this is the active (approved) schedule")
    employee_id: str | None = None
    name: str | None = None
    shift_count: int | None = None
    paid_hours: float | None = None
    total_cost: float | None = None
    preferred_start: str | None = None
    preferred_count: int | None = Field(None, description="Empty for a pair team: not relevant")
    preference_rate: float | None = None
    min_shifts_exempt: bool | None = None
    unavailable_dates: list[str] = Field(default_factory=list, description="From the input")
    shifts: list[EmployeeShiftOut] = Field(default_factory=list)


class LockResult(ToolResult):
    schedule_version: str | None = Field(None, description="The active schedule the lock belongs to")
    employee_id: str | None = None
    name: str | None = None
    date: str | None = None
    weekday: str | None = None
    shift_id: str | None = None
    start: str | None = None
    end: str | None = None
    is_locked: bool | None = Field(None, description="The shift's lock state after this call")
    changed: bool | None = Field(None, description="False if the shift already had this state (nothing recorded)")
    now: str | None = Field(None, description="The time used to check that the shift has not started")


class AbsenceResult(ToolResult):
    absence_id: str | None = Field(None, description="Empty when the employee had no shift that day "
                                                     "(nothing recorded)")
    created: bool | None = Field(None, description="False for a repeated report or a day without a shift")
    employee_id: str | None = None
    name: str | None = None
    date: str | None = None
    weekday: str | None = None
    shift_id: str | None = Field(None, description="The shift that will be removed in the repair draft")
    start: str | None = None
    end: str | None = None
    was_locked: bool | None = Field(None, description="The removed shift was locked (removed anyway, spec 3)")
    min_shifts_exempt: bool | None = Field(None, description="The employee is exempt from the 4-shift minimum "
                                                             "this week")
    schedule_version: str | None = Field(None, description="The active schedule, unchanged by the report")
    message: str | None = Field(None, description="What happened, in one or two sentences for the manager")


class ReplacementOut(BaseModel):
    absent_employee: str
    date: str
    shift_id: str
    replacement_employee: str
    replacement_name: str


class RepairResult(ToolResult):
    run: RunBrief | None = Field(None, description="The repair run")
    mode: str | None = Field(None, description="SINGLE_REPLACEMENT, or REOPTIMIZED when no single replacement exists")
    changed_assignments: int | None = Field(None, description="Changed (employee, date) pairs in the draft "
                                                              "(break-only moves not counted)")
    replacements: list[ReplacementOut] = Field(default_factory=list, description="SINGLE_REPLACEMENT only")
    moved_breaks: int | None = Field(None, description="Breaks of other shifts moved (not counted as changes)")
    open_absences: list[str] = Field(default_factory=list, description="The absences this repair answers")
    active_schedule_version: str | None = Field(None, description="Unchanged until a repair draft is approved")
    message: str | None = None
