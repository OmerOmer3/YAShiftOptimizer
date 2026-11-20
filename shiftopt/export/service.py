"""Export a stored run or schedule version (SCRUM-44). Used by the CLI and the MCP tool.

Export time is not part of the measured optimization time (spec section 9).
"""
from __future__ import annotations

import re
from pathlib import Path

from shiftopt.db import Store
from shiftopt.domain import Schedule
from shiftopt.export.excel_writer import write_workbook

_REF = re.compile(r"^(RUN|SV)-\d+$")


class ExportError(Exception):
    """The run or schedule version to export does not exist."""


def changed_assignments(base: Schedule, new: Schedule) -> int:
    """Employee-and-date pairs whose shift differs between the base version and the new one:
    added, removed, or moved to another shift template or time."""
    def key(s):
        return {(a.employee_id, a.date): (a.shift_id, a.start, a.end) for a in s.assignments}
    b, n = key(base), key(new)
    return sum(1 for k in b.keys() | n.keys() if b.get(k) != n.get(k))


def _instructions(run, has_schedule: bool) -> list[tuple[str, str]]:
    rows = [("תוכן", f"ריצה {run.run_id}" + (f", גרסת שיבוץ {run.schedule_version}" if run.schedule_version else "")
             + ". זהו צילום מצב: כל הערכים חושבו בשרת ואין בקובץ נוסחאות.")]
    if has_schedule:
        rows.append(("בדיקה", "Coverage ו-EmployeeSummary חושבו מחדש על ידי הבודק העצמאי מתוך השיבוץ עצמו."))
        rows.append(("הפסקות", "התחלה כלולה וסיום אינו כלול. עובד בהפסקה אינו נספר בכיסוי."))
    else:
        rows.append(("אין שיבוץ", f"מצב {run.status}. גיליונות התוצאה מכילים כותרות בלבד. סיבה: {run.termination_reason}"))
    rows.append(("ערכים ריקים", "מדד שאינו זמין או אינו רלוונטי (למשל מדדי העדפה בצוות זוגי) נשאר ריק ולא אפס."))
    rows.append(("זמני ריצה", "OptimizationSeconds כולל בניית מודל, פתרון ובדיקה עצמאית; לא כולל ייבוא וייצוא."))
    return rows


def export_run(store: Store, ref: str, path: str | Path) -> Path:
    """Export the run `ref` (RUN-0001) or the run that produced schedule version `ref` (SV-0001)."""
    ref = ref.strip().upper()
    if not _REF.match(ref):
        raise ExportError(f"'{ref}' is not a run ID (RUN-0001) or a schedule version (SV-0001)")
    if ref.startswith("SV-"):
        info = store.schedule_version_info(ref)
        if info is None:
            raise ExportError(f"schedule version {ref} not found")
        ref = info["run_id"]
    run = store.get_run(ref)
    if run is None:
        raise ExportError(f"run {ref} not found")

    data = store.load_input(run.input_version)
    schedule = store.load_schedule(run.schedule_version) if run.schedule_version else None
    changed = None
    if schedule is not None:
        base = store.load_schedule(run.base_schedule_version) if run.base_schedule_version else None
        changed = 0 if base is None else changed_assignments(base, schedule)

    summary = {
        "RunID": run.run_id, "RunType": run.run_type, "GroupSize": run.group_size,
        "SolutionStatus": run.status, "ApprovalStatus": run.approval_status,
        "ScheduleVersion": run.schedule_version, "BaseScheduleVersion": run.base_schedule_version,
        "TimeBudgetSeconds": run.time_budget_seconds,
        "FirstFeasibleSeconds": None if run.first_feasible_seconds is None else round(run.first_feasible_seconds, 3),
        "OptimizationSeconds": round(run.optimization_seconds, 3),
        "ChangedAssignments": changed,
        "CoverageOptimalityProven": run.coverage_proven, "CostOptimalityProven": run.cost_proven,
        # Stability is an objective of repair runs only, preference of triples only (spec section 8).
        "StabilityOptimalityProven": None,
        "PreferenceOptimalityProven": None,
        "TerminationReason": run.termination_reason, "ValidationPassed": run.validation_passed,
    }
    return write_workbook(path, data, schedule, summary=summary, group_size=run.group_size,
                          instructions=_instructions(run, schedule is not None))
