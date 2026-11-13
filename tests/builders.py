"""Small hand-made inputs for engine tests, built directly as InputData.

Each helper keeps the spec's fixed break rules (spec section 6, BreakRules), so a test
only states what it is about: employees, demand and templates.
"""
from __future__ import annotations

import datetime as dt

from shiftopt.domain import BreakRule, DemandHour, Employee, InputData, ShiftTemplate

WEEK = dt.date(2026, 11, 1)  # a Sunday

BREAK_RULES = {
    4: BreakRule(4, 1, 30, 60, 60, 60),
    6: BreakRule(6, 2, 30, 60, 60, 60),
    8: BreakRule(8, 2, 30, 60, 60, 60),
}


def h(hh: int, mm: int = 0) -> int:
    return hh * 60 + mm


def make_input(employees, demand, templates) -> InputData:
    """employees: [(id, unavailable_days)]
    demand: {day: {hour: required}} with day 1..6 and whole hours
    templates: [(shift_id, day, start_minutes, hours, cost)]"""
    emps = {eid: Employee(eid, eid, frozenset(un)) for eid, un in employees}
    dem = {}
    for day, hours in demand.items():
        date = WEEK + dt.timedelta(days=day - 1)
        for hour, req in hours.items():
            dem[(date, h(hour))] = DemandHour(date, h(hour), req)
    temps = {sid: ShiftTemplate(sid, day, start, hours, cost) for sid, day, start, hours, cost in templates}
    return InputData("1.0", WEEK, emps, dem, temps, dict(BREAK_RULES))


def flat_demand(days, first_hour, last_hour, required=1):
    """Same demand on every listed day for hours first_hour .. last_hour-1."""
    return {d: {hr: required for hr in range(first_hour, last_hour)} for d in days}
