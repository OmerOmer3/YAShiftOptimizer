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
    optimization_seconds: float
    schedule_version: str | None
    approval_status: str | None


class SystemStatus(ToolResult):
    active_input_version: str | None = None
    active_schedule_version: str | None = Field(None, description="The approved schedule in force, if any")
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
        validation_passed=run.validation_passed, optimization_seconds=round(run.optimization_seconds, 3),
        schedule_version=run.schedule_version, approval_status=run.approval_status)
