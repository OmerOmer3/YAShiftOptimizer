"""Approve a draft so that it becomes the active schedule (SCRUM-37, spec section 4).

"The active schedule changes only after the manager explicitly approves the draft
version." Before approving, the server checks that the input the draft is based on is
still current, that the schedule version it is based on (repair drafts) is still the
active one, and that the schedule passes the independent checker; the language
model's opinion is never relied on. Approval is the only operation that changes the
active schedule; it runs in one transaction that also writes the audit-log entry
(SCRUM-24), so there is no approval without a record.

Initial drafts have no base (spec section 8), so choosing another initial draft of the
same input is allowed; BASE_VERSION_STALE applies to drafts that have one (SCRUM-38).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from shiftopt.db import Store
from shiftopt.db.store import StaleState
from shiftopt.validator import validate


class ApprovalRefused(Exception):
    def __init__(self, code: str, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class ApprovalOutcome:
    schedule_version: str
    run_id: str
    approved_at: dt.datetime
    replaced_version: str | None
    total_cost: float | None
    total_shortage_slots: int | None
    validation_details: list[str] = field(default_factory=list)


def approve_draft(store: Store, version: str) -> ApprovalOutcome:
    version = version.strip().upper()
    info = store.schedule_version_info(version) if version.startswith("SV-") else None
    if info is None:
        raise ApprovalRefused("NOT_FOUND", f"Schedule version {version} not found.")
    if info["approval_status"] != "DRAFT":
        state = "the active schedule" if info["is_active"] else "approved earlier and later replaced"
        raise ApprovalRefused("ALREADY_APPROVED", f"{version} is not a draft: it is {state}.")

    active_input = store.active_input_version()
    if info["input_version"] != active_input:
        raise ApprovalRefused("INPUT_VERSION_STALE",
                              f"{version} was made from input {info['input_version']}, but the current input is "
                              f"{active_input}. Create a new schedule for the current input.")

    active_schedule = store.active_schedule_version()
    if info["base_version"] is not None and info["base_version"] != active_schedule:
        now = active_schedule or "no active schedule"
        raise ApprovalRefused("BASE_VERSION_STALE",
                              f"{version} was built on {info['base_version']}, but the active schedule is now {now}. "
                              "Approving it would overwrite a newer schedule; create a new draft instead.")

    run = store.get_run(info["run_id"])
    report = validate(store.load_input(info["input_version"]), store.load_schedule(version),
                      group_size=run.group_size)
    if not report.passed:
        details = [str(v) for v in report.violations[:20]]
        raise ApprovalRefused("VALIDATION_FAILED",
                              f"{version} does not pass the independent checker "
                              f"({len(report.violations)} violation(s)); it was not approved.", details)

    expected_active = active_schedule
    try:
        approved_at, replaced = store.approve(version, expected_input=active_input, expected_active=expected_active)
    except StaleState as e:
        # The state changed between the checks above and the write: refuse, nothing was changed.
        messages = {"INPUT_VERSION_STALE": "A new input was imported while approving; nothing was changed.",
                    "ACTIVE_CHANGED": "The active schedule changed while approving; nothing was changed. Try again.",
                    "ALREADY_APPROVED": f"{version} was approved by another request; nothing was changed.",
                    "BASE_VERSION_STALE": f"The schedule {version} was built on is no longer active; nothing was "
                                          "changed."}
        code = e.code if e.code in ("INPUT_VERSION_STALE", "ALREADY_APPROVED", "BASE_VERSION_STALE") else "CONFLICT"
        raise ApprovalRefused(code, messages[e.code]) from e

    return ApprovalOutcome(schedule_version=version, run_id=info["run_id"], approved_at=approved_at,
                           replaced_version=replaced, total_cost=run.total_cost,
                           total_shortage_slots=run.total_shortage_slots)
