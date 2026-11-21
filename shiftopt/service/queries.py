"""Read-only questions about a schedule version, answered from the independent checker's
metrics (spec section 4: numeric answers come from the tools). SCRUM-35 coverage and cost;
SCRUM-34 one employee's shifts, hours and cost.

Which version: the one named, else the active (approved) schedule, else the only draft of
the current input; several drafts and no active schedule is AMBIGUOUS. Days and employees
are given as the manager's words and resolved here, so the rules hold whichever model
calls the tool (decision in SCRUM-105).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from shiftopt.domain import InputData, Schedule
from shiftopt.service.resolve import Unresolved, resolve_date, resolve_draft, resolve_employee
from shiftopt.validator.metrics import compute_metrics

_WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class SlotLine:
    date: dt.date
    slot_start: str
    slot_end: str
    required: int
    assigned: int
    shortage: int
    surplus: int


@dataclass
class DayLine:
    date: dt.date
    weekday: str
    required_slots: int          # sum of RequiredEmployees over the day's half-hours
    shortage_slots: int
    surplus_slots: int
    cost: float
    shift_count: int


@dataclass
class ShiftLine:
    employee_id: str
    name: str
    shift_id: str
    start: str
    end: str
    breaks: list[str]
    paid_hours: float
    cost: float


@dataclass
class CoverageReport:
    total_shortage_slots: int
    total_shortage_hours: float
    total_surplus_slots: int
    total_cost: float
    days: list[DayLine]
    shortage_slots: list[SlotLine]                         # every half-hour with a shortage
    day: dt.date | None = None
    day_slots: list[SlotLine] = field(default_factory=list)  # all half-hours of `day`
    day_shifts: list[ShiftLine] = field(default_factory=list)


def coverage_report(data: InputData, schedule: Schedule, *, group_size: int,
                    day: dt.date | None = None) -> CoverageReport:
    m = compute_metrics(data, schedule, group_size=group_size)
    lines = [SlotLine(r.date, hhmm(r.slot_start), hhmm(r.slot_end), r.required, r.assigned, r.shortage, r.surplus)
             for r in m.coverage]
    req, short, surplus = defaultdict(int), defaultdict(int), defaultdict(int)
    for r in m.coverage:
        req[r.date] += r.required
        short[r.date] += r.shortage
        surplus[r.date] += r.surplus
    cost, shifts = defaultdict(float), defaultdict(int)
    for a in schedule.assignments:
        cost[a.date] += a.cost
        shifts[a.date] += 1
    days = [DayLine(d, _WEEKDAY[d.weekday()], req[d], short[d], surplus[d], round(cost[d], 2), shifts[d])
            for d in sorted(set(req) | set(cost))]
    rep = CoverageReport(total_shortage_slots=m.total_shortage_slots, total_shortage_hours=m.total_shortage_slots / 2,
                         total_surplus_slots=m.total_surplus_slots, total_cost=round(m.total_cost, 2), days=days,
                         shortage_slots=[s for s in lines if s.shortage > 0], day=day)
    if day is not None:
        rep.day_slots = [s for s in lines if s.date == day]
        rep.day_shifts = [
            ShiftLine(a.employee_id, data.employees[a.employee_id].name, a.shift_id, hhmm(a.start), hhmm(a.end),
                      [f"{hhmm(b.start)}-{hhmm(b.end)}" for b in a.breaks], a.paid_hours, a.cost)
            for a in sorted(schedule.assignments, key=lambda a: (a.start, a.employee_id)) if a.date == day]
    return rep


def target_version(store, ref: str | None) -> str:
    """The version a question is about: named, else active, else the only draft."""
    if ref:
        return resolve_draft(store, ref)
    active = store.active_schedule_version()
    if active:
        return active
    try:
        return resolve_draft(store, None)
    except Unresolved as e:
        if e.code == "NOT_FOUND":
            raise Unresolved("NOT_FOUND", "There is no schedule yet: no active schedule and no draft. "
                                          "Create a schedule first.", []) from e
        raise Unresolved(e.code, "There is no active schedule and there are several drafts; which one?",
                         e.candidates) from e


def get_coverage(store, ref: str | None = None, day_text: str | None = None) -> tuple[dict, CoverageReport]:
    version = target_version(store, ref)
    info = store.schedule_version_info(version)
    data = store.load_input(info["input_version"])
    day = resolve_date(data, day_text) if day_text else None
    run = store.get_run(info["run_id"])
    return info, coverage_report(data, store.load_schedule(version), group_size=run.group_size, day=day)


@dataclass
class EmployeeShift:
    date: dt.date
    weekday: str
    shift_id: str
    start: str
    end: str
    breaks: list[str]
    paid_hours: float
    cost: float
    is_locked: bool


@dataclass
class EmployeeReport:
    employee_id: str
    name: str
    shift_count: int
    paid_hours: float
    total_cost: float
    preferred_start: str | None
    preferred_count: int | None      # empty for a pair team (spec section 8)
    preference_rate: float | None
    min_shifts_exempt: bool
    unavailable_dates: list[str]     # from the input, within the planning week
    shifts: list[EmployeeShift]


def employee_report(data: InputData, schedule: Schedule, employee_text: str, *, group_size: int) -> EmployeeReport:
    eid = resolve_employee(data, employee_text)
    m = compute_metrics(data, schedule, group_size=group_size)
    row = next(e for e in m.employees if e.employee_id == eid)
    emp = data.employees[eid]
    shifts = [EmployeeShift(a.date, _WEEKDAY[a.date.weekday()], a.shift_id, hhmm(a.start), hhmm(a.end),
                            [f"{hhmm(b.start)}-{hhmm(b.end)}" for b in a.breaks], a.paid_hours, a.cost,
                            bool(a.is_locked))
              for a in sorted(schedule.assignments, key=lambda a: a.date) if a.employee_id == eid]
    return EmployeeReport(
        employee_id=eid, name=emp.name, shift_count=row.shift_count, paid_hours=row.paid_hours,
        total_cost=round(row.total_cost, 2),
        preferred_start=None if row.preferred_start is None else hhmm(row.preferred_start),
        preferred_count=row.preferred_count, preference_rate=row.preference_rate,
        min_shifts_exempt=bool(row.min_shifts_exempt),
        unavailable_dates=sorted(data.date_for_day(d).isoformat() for d in emp.unavailable_days),
        shifts=shifts)


def get_employee(store, employee_text: str, ref: str | None = None) -> tuple[dict, EmployeeReport]:
    version = target_version(store, ref)
    info = store.schedule_version_info(version)
    data = store.load_input(info["input_version"])
    run = store.get_run(info["run_id"])
    return info, employee_report(data, store.load_schedule(version), employee_text, group_size=run.group_size)
