"""Report a full-day absence and prepare the repair (SCRUM-40, spec section 3).

Spec section 3, repair process step 1: "the system loads the active schedule, records the
absence and removes the affected shift from the draft. A report for an employee with no
shift that day returns a message and changes no schedule." The absence covers a whole
day and is reported before the affected shift starts.

report_absence records the absence (and a change-log entry). It never changes the active
schedule. repair_start builds the starting point every repair works from (SCRUM-41,
SCRUM-42):

  * the active schedule without the shift of every OPEN absence of that week, even if
    that shift was locked (spec section 3: "the absent employee's shift is removed from
    the draft even if it was locked; the action is recorded as part of the absence");
  * the absent employees exempt from the 4-shift minimum, in that week only; 5 shifts,
    40 hours and unavailable days still hold for them (spec section 3);
  * the shifts that must stay exactly as they are: started or completed (by the current
    time, shiftopt.clock) and locked;
  * the (employee, date) pairs that must stay empty: the absences themselves.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from shiftopt import clock
from shiftopt.db import Store
from shiftopt.db.store import StaleState
from shiftopt.domain import Assignment, Schedule, fmt_minutes


class AbsenceRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class AbsenceOutcome:
    """The result of a report. absence_id is empty when the employee had no shift that day."""
    employee_id: str
    date: dt.date
    schedule_version: str
    absence_id: str | None
    created: bool                 # False for 'no shift' and for a repeated report
    shift_id: str | None = None
    start: str | None = None
    end: str | None = None
    was_locked: bool = False
    message: str = ""


@dataclass
class RepairStart:
    """What a repair starts from. `schedule` already lacks the absent shifts."""
    base_version: str
    input_version: str
    schedule: Schedule
    absences: list[dict]
    removed: list[Assignment]
    fixed: list[Assignment] = field(default_factory=list)       # started, completed or locked: keep exactly
    blocked: set[tuple[str, dt.date]] = field(default_factory=set)  # absent (employee, date): stay empty
    now: dt.datetime | None = None


def report_absence(store: Store, employee_id: str, date: dt.date, *, now: dt.datetime | None = None) -> AbsenceOutcome:
    active = store.active_schedule_version()
    if active is None:
        raise AbsenceRefused("NOT_FOUND", "There is no active schedule yet. An absence is repaired against the "
                                          "approved schedule; approve a draft first.")
    schedule = store.load_schedule(active)
    shift = next((a for a in schedule.assignments if a.employee_id == employee_id and a.date == date), None)
    if shift is None:
        return AbsenceOutcome(employee_id=employee_id, date=date, schedule_version=active, absence_id=None,
                              created=False, message=f"{employee_id} has no shift on {date.isoformat()} in the "
                                                     f"active schedule {active}; nothing to repair and nothing "
                                                     "was changed.")
    at = now or clock.now()
    if clock.has_started(date, shift.start, at):
        raise AbsenceRefused("SHIFT_STARTED",
                             f"The shift of {employee_id} on {date.isoformat()} ({fmt_minutes(shift.start)}-"
                             f"{fmt_minutes(shift.end)}) started before {at:%Y-%m-%d %H:%M}. An absence must be "
                             "reported before the shift starts; changing a started shift is not part of the "
                             "project (spec section 3).")
    try:
        absence_id, created = store.record_absence(active, employee_id, date)
    except StaleState as e:
        raise AbsenceRefused("CONFLICT", "The active schedule changed meanwhile; nothing was recorded. "
                                         "Try again.") from e
    lock_note = " The shift was locked; it is still removed, as the spec requires for an absence." \
        if shift.is_locked else ""
    message = (f"Absence {absence_id} recorded: {employee_id} on {date.isoformat()}. Shift {shift.shift_id} "
               f"({fmt_minutes(shift.start)}-{fmt_minutes(shift.end)}) will be removed in the repair draft, and "
               f"{employee_id} is exempt from the minimum of 4 shifts this week.{lock_note} The active schedule "
               "has not changed." if created else
               f"This absence was already reported ({absence_id}); nothing new was recorded.")
    return AbsenceOutcome(employee_id=employee_id, date=date, schedule_version=active, absence_id=absence_id,
                          created=created, shift_id=shift.shift_id, start=fmt_minutes(shift.start),
                          end=fmt_minutes(shift.end), was_locked=shift.is_locked, message=message)


def repair_start(store: Store, *, now: dt.datetime | None = None) -> RepairStart:
    """The starting point for repairing every OPEN absence against the active schedule."""
    active = store.active_schedule_version()
    if active is None:
        raise AbsenceRefused("NOT_FOUND", "There is no active schedule to repair.")
    info = store.schedule_version_info(active)
    absences = store.list_absences(status="OPEN", input_version=info["input_version"])
    if not absences:
        raise AbsenceRefused("NOT_FOUND", "No open absence to repair. Report an absence first.")
    at = now or clock.now()
    active_schedule = store.load_schedule(active)
    blocked = {(a["employee_id"], a["date"]) for a in absences}
    kept = [a for a in active_schedule.assignments if (a.employee_id, a.date) not in blocked]
    removed = [a for a in active_schedule.assignments if (a.employee_id, a.date) in blocked]
    schedule = Schedule(assignments=kept,
                        min_shifts_exempt=set(active_schedule.min_shifts_exempt) | {e for e, _ in blocked})
    fixed = [a for a in kept if a.is_locked or clock.has_started(a.date, a.start, at)]
    return RepairStart(base_version=active, input_version=info["input_version"], schedule=schedule,
                       absences=absences, removed=removed, fixed=fixed, blocked=blocked, now=at)


def absent_shifts_kept(store: Store, input_version: str, draft: Schedule) -> list[str]:
    """Shifts in `draft` on a day its employee is reported absent (OPEN absences of the input)."""
    out = []
    for ab in store.list_absences(status="OPEN", input_version=input_version):
        if any(a.employee_id == ab["employee_id"] and a.date == ab["date"] for a in draft.assignments):
            out.append(f"{ab['employee_id']} is reported absent on {ab['date'].isoformat()} ({ab['absence_id']}) "
                       "but has a shift that day")
    return out
