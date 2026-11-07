"""Read the Schedule sheet of an output workbook (spec section 7) into a Schedule.

Used to validate externally produced schedules (e.g. the course's Output_Small_10)
with the independent validator, and later to re-import exported versions.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl

from shiftopt.domain import Assignment, Break, Schedule, time_to_minutes


def _minutes(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        v = v.time()
    return time_to_minutes(v)


def read_schedule(path: str | Path) -> Schedule:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Schedule"]
    hdr = [c.value for c in ws[1]]
    col = {name: i for i, name in enumerate(hdr)}
    sched = Schedule()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in row):
            continue
        g = lambda k: row[col[k]]  # noqa: E731
        d = g("Date")
        d = d.date() if isinstance(d, dt.datetime) else d
        breaks = []
        for i in (1, 2):
            s, e = _minutes(g(f"Break{i}Start")), _minutes(g(f"Break{i}End"))
            if s is not None and e is not None:
                breaks.append(Break(s, e))
        sched.assignments.append(Assignment(
            date=d, employee_id=g("EmployeeID"), shift_id=g("ShiftID"),
            start=_minutes(g("StartTime")), end=_minutes(g("EndTime")) or 24 * 60,
            breaks=tuple(breaks), paid_hours=float(g("PaidHours")), cost=float(g("Cost")),
            is_locked=bool(g("IsLocked")),
        ))
    if "EmployeeSummary" in wb.sheetnames:
        ws = wb["EmployeeSummary"]
        h = [c.value for c in ws[1]]
        if "MinShiftsExempt" in h:
            i_id, i_ex = h.index("EmployeeID"), h.index("MinShiftsExempt")
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[i_id] and row[i_ex] is True:
                    sched.min_shifts_exempt.add(row[i_id])
    return sched
