"""Write one result version as an .xlsx in the output schema (spec sections 7-9, SCRUM-44).

Rules taken from the spec and checked by tests/test_export.py:
  * four data sheets, Schedule / Coverage / EmployeeSummary / RunSummary, with the
    column names and order of the course sample Output_Small_10.xlsx; Instructions is optional;
  * every metric is computed here and written as a value, never as an Excel formula;
  * a metric that is not available is left empty, not zero;
  * when there is no schedule the result sheets keep their headers only, and RunSummary
    still reports the status and the termination reason.

Coverage and EmployeeSummary come from the independent checker's metrics
(shiftopt.validator.metrics), not from the optimizer, so the exported numbers are the
ones that were verified.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from shiftopt.domain import InputData, Schedule, minutes_to_time
from shiftopt.validator.metrics import compute_metrics

SCHEMA_VERSION = "1.0"
DATA_SHEETS = ("RunSummary", "Schedule", "Coverage", "EmployeeSummary")

SCHEDULE_COLUMNS = ["Date", "EmployeeID", "ShiftID", "StartTime", "EndTime", "Break1Start", "Break1End",
                    "Break2Start", "Break2End", "PaidHours", "Cost", "IsLocked"]
COVERAGE_COLUMNS = ["Date", "SlotStart", "SlotEnd", "RequiredEmployees", "AssignedEmployees", "Shortage", "Surplus"]
EMPLOYEE_COLUMNS = ["EmployeeID", "ShiftCount", "PaidHours", "TotalCost", "PreferredShiftStart",
                    "PreferredShiftCount", "PreferenceRate", "MinShiftsExempt"]
RUN_SUMMARY_KEYS = ["SchemaVersion", "RunID", "InputFile", "WeekStart", "RunType", "GroupSize", "SolutionStatus",
                    "ApprovalStatus", "ScheduleVersion", "BaseScheduleVersion", "TimeBudgetSeconds",
                    "FirstFeasibleSeconds", "OptimizationSeconds", "TotalCost", "TotalShortageSlots",
                    "ChangedAssignments", "MinPreferenceRate", "CoverageOptimalityProven", "CostOptimalityProven",
                    "StabilityOptimalityProven", "PreferenceOptimalityProven", "TerminationReason",
                    "ValidationPassed"]

DATE_FMT, TIME_FMT, PCT_FMT = "yyyy-mm-dd", "hh:mm", "0%"


def _t(minutes: int | None) -> dt.time | None:
    return None if minutes is None else minutes_to_time(minutes)


def _n(x: float | None):
    """Whole numbers as int (8, not 8.0); money keeps its cents."""
    if x is None:
        return None
    return int(x) if float(x).is_integer() else round(x, 2)


def _sheet(wb, title: str, header: list[str]):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    return ws


def _formats(ws, formats: dict[int, str]) -> None:
    """formats: 1-based column -> number format, applied to every data row."""
    for row in ws.iter_rows(min_row=2):
        for col, fmt in formats.items():
            row[col - 1].number_format = fmt


def _widths(ws) -> None:
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=1), start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(10, len(str(col[0].value)) + 2)


def summary_metrics(data: InputData, schedule: Schedule | None, group_size: int) -> dict:
    """The RunSummary values that come from the schedule itself (empty when there is none)."""
    if schedule is None:
        return {"TotalCost": None, "TotalShortageSlots": None, "MinPreferenceRate": None}
    m = compute_metrics(data, schedule, group_size=group_size)
    return {"TotalCost": _n(m.total_cost), "TotalShortageSlots": m.total_shortage_slots,
            "MinPreferenceRate": m.min_preference_rate}


def write_workbook(path: str | Path, data: InputData, schedule: Schedule | None, *, summary: dict,
                   group_size: int, instructions: list[tuple[str, str]] | None = None) -> Path:
    """Write the workbook. `summary` holds RunSummary values by key; missing keys stay empty,
    except the ones computed from the schedule here (TotalCost, TotalShortageSlots, MinPreferenceRate)."""
    path = Path(path)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    values = {"SchemaVersion": SCHEMA_VERSION, "InputFile": data.source_file or None, "WeekStart": data.week_start,
              "GroupSize": group_size}
    values.update(summary)
    values.update(summary_metrics(data, schedule, group_size))
    ws = _sheet(wb, "RunSummary", ["Metric", "Value"])
    for key in RUN_SUMMARY_KEYS:
        ws.append([key, values.get(key)])
        v = ws.cell(ws.max_row, 2)
        if isinstance(v.value, dt.date):
            v.number_format = DATE_FMT
        elif key == "MinPreferenceRate" and v.value is not None:
            v.number_format = PCT_FMT
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 24

    sched = _sheet(wb, "Schedule", SCHEDULE_COLUMNS)
    cov = _sheet(wb, "Coverage", COVERAGE_COLUMNS)
    emp = _sheet(wb, "EmployeeSummary", EMPLOYEE_COLUMNS)

    if schedule is not None:
        # One row per shift, no row for a day off; ordered like the sample (employee, then date).
        for a in sorted(schedule.assignments, key=lambda a: (a.employee_id, a.date)):
            b = list(a.breaks) + [None, None]
            sched.append([a.date, a.employee_id, a.shift_id, _t(a.start), _t(a.end),
                          _t(b[0] and b[0].start), _t(b[0] and b[0].end),
                          _t(b[1] and b[1].start), _t(b[1] and b[1].end),
                          _n(a.paid_hours), _n(a.cost), bool(a.is_locked)])
        m = compute_metrics(data, schedule, group_size=group_size)
        for r in m.coverage:
            cov.append([r.date, _t(r.slot_start), _t(r.slot_end), r.required, r.assigned, r.shortage, r.surplus])
        for e in m.employees:
            emp.append([e.employee_id, e.shift_count, _n(e.paid_hours), _n(e.total_cost), _t(e.preferred_start),
                        e.preferred_count, e.preference_rate, bool(e.min_shifts_exempt)])

    _formats(sched, {1: DATE_FMT, **{c: TIME_FMT for c in range(4, 10)}})
    _formats(cov, {1: DATE_FMT, 2: TIME_FMT, 3: TIME_FMT})
    _formats(emp, {5: TIME_FMT, 7: PCT_FMT})
    for w in (sched, cov, emp):
        _widths(w)

    if instructions:
        ins = _sheet(wb, "Instructions", ["Topic", "Explanation"])
        for row in instructions:
            ins.append(list(row))
        ins.column_dimensions["A"].width = 16
        ins.column_dimensions["B"].width = 100
        ins.sheet_view.rightToLeft = True

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
