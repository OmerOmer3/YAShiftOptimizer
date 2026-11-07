"""Metrics recomputed from a schedule alone (SCRUM-26).

Definitions follow spec sections 2 and 7:
  * Coverage is per 30-minute slot. An employee counts only while actually working
    (breaks removed). Hourly demand applies to both halves of the hour.
  * Shortage = max(0, required - active); Surplus = max(0, active - required).
  * TotalShortageSlots = sum of Shortage over every Coverage row; employee-hours short = half of it.
  * Two Coverage rows per Demand row, including zero-demand hours.

This module must not import anything from shiftopt.engine (independence test).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from shiftopt.domain import SLOT_MINUTES, InputData, Schedule


@dataclass
class CoverageRow:
    date: dt.date
    slot_start: int
    slot_end: int
    required: int
    assigned: int

    @property
    def shortage(self) -> int:
        return max(0, self.required - self.assigned)

    @property
    def surplus(self) -> int:
        return max(0, self.assigned - self.required)


@dataclass
class EmployeeSummaryRow:
    employee_id: str
    shift_count: int
    paid_hours: float
    total_cost: float
    preferred_start: int | None
    preferred_count: int | None  # None for a pair team or when not relevant
    preference_rate: float | None
    min_shifts_exempt: bool


@dataclass
class Metrics:
    coverage: list[CoverageRow] = field(default_factory=list)
    employees: list[EmployeeSummaryRow] = field(default_factory=list)
    total_shortage_slots: int = 0
    total_surplus_slots: int = 0
    total_cost: float = 0.0
    total_paid_hours: float = 0.0
    shift_count: int = 0
    min_preference_rate: float | None = None

    @property
    def shortage_employee_hours(self) -> float:
        return self.total_shortage_slots / 2


def compute_metrics(data: InputData, schedule: Schedule, group_size: int = 2) -> Metrics:
    m = Metrics()

    active: Counter = Counter()
    for a in schedule.assignments:
        for s in a.working_slots():
            active[(a.date, s)] += 1

    for (d, hour), dh in sorted(data.demand.items()):
        for s in (hour, hour + SLOT_MINUTES):
            m.coverage.append(CoverageRow(d, s, s + SLOT_MINUTES, dh.required, active[(d, s)]))
    m.total_shortage_slots = sum(r.shortage for r in m.coverage)
    m.total_surplus_slots = sum(r.surplus for r in m.coverage)

    by_emp = defaultdict(list)
    for a in schedule.assignments:
        by_emp[a.employee_id].append(a)
    rates = []
    for eid, emp in data.employees.items():
        shifts = by_emp.get(eid, [])
        paid = sum(a.paid_hours for a in shifts)
        cost = round(sum(a.cost for a in shifts), 2)
        pc = rate = None
        # Preference metrics exist only for a triple (spec section 2); a pair leaves them empty (section 8).
        if group_size == 3 and emp.preferred_start is not None and shifts:
            pc = sum(1 for a in shifts if a.start == emp.preferred_start)
            rate = pc / len(shifts)
            rates.append(rate)
        m.employees.append(EmployeeSummaryRow(eid, len(shifts), paid, cost, emp.preferred_start, pc, rate,
                                              eid in schedule.min_shifts_exempt))
    m.total_cost = round(sum(a.cost for a in schedule.assignments), 2)
    m.total_paid_hours = sum(a.paid_hours for a in schedule.assignments)
    m.shift_count = len(schedule.assignments)
    m.min_preference_rate = min(rates) if rates else None
    return m
