"""Independent validator (SCRUM-25).

Checks a schedule against every hard rule in spec section 1-2 using only the
input data and the schedule itself. It deliberately shares no code with the
solver model in shiftopt.engine, so a modelling bug cannot hide itself.
A test fails the build if this package imports shiftopt.engine.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from shiftopt.domain import (
    MAX_PAID_MINUTES_PER_WEEK,
    MAX_SHIFTS_PER_WEEK,
    MIN_SHIFTS_PER_WEEK,
    InputData,
    Schedule,
    day_of_week,
    fmt_minutes,
    is_slot_aligned,
)
from shiftopt.validator.metrics import Metrics, compute_metrics


@dataclass
class Violation:
    rule: str
    employee_id: str | None
    date: dt.date | None
    message: str

    def __str__(self) -> str:
        who = self.employee_id or "-"
        when = self.date.isoformat() if self.date else "-"
        return f"[{self.rule}] {who} {when}: {self.message}"


@dataclass
class ValidationReport:
    violations: list[Violation] = field(default_factory=list)
    metrics: Metrics | None = None

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def total_shortage_slots(self) -> int:
        return self.metrics.total_shortage_slots

    @property
    def total_cost(self) -> float:
        return self.metrics.total_cost

    def rules_broken(self) -> set[str]:
        return {v.rule for v in self.violations}


def validate(data: InputData, schedule: Schedule, group_size: int = 2) -> ValidationReport:
    rep = ValidationReport()
    add = lambda rule, eid, d, msg: rep.violations.append(Violation(rule, eid, d, msg))  # noqa: E731
    week = set(data.week_dates)

    per_emp_day = defaultdict(list)
    for a in schedule.assignments:
        eid, d = a.employee_id, a.date
        per_emp_day[(eid, d)].append(a)

        emp = data.employees.get(eid)
        if emp is None:
            add("UNKNOWN_EMPLOYEE", eid, d, "employee is not in the input")
            continue
        if d not in week:
            add("DATE_OUT_OF_WEEK", eid, d, "date is not Sunday-Friday of the planning week")
            continue
        dow = day_of_week(d)
        if dow in emp.unavailable_days:
            add("UNAVAILABLE_DAY", eid, d, f"works on declared unavailable day {dow}")

        t = data.templates.get(a.shift_id)
        if t is None:
            add("UNKNOWN_TEMPLATE", eid, d, f"shift template '{a.shift_id}' does not exist")
            continue
        if t.day_of_week != dow:
            add("TEMPLATE_WRONG_DAY", eid, d, f"template {t.shift_id} is for day {t.day_of_week}, date is day {dow}")
        if (a.start, a.end) != (t.start, t.end):
            add("TEMPLATE_MISMATCH", eid, d,
                f"shift {fmt_minutes(a.start)}-{fmt_minutes(a.end)} differs from template "
                f"{t.shift_id} {fmt_minutes(t.start)}-{fmt_minutes(t.end)}")
        if abs(a.paid_hours - t.duration_hours) > 1e-9:
            add("PAID_HOURS_MISMATCH", eid, d, f"paid hours {a.paid_hours} but template is {t.duration_hours} h")
        if abs(a.cost - t.cost) > 0.005:
            add("COST_MISMATCH", eid, d, f"cost {a.cost} but template cost is {t.cost}")

        # Whole shift (breaks included) inside hours listed in Demand for that date.
        allowed = data.allowed_hours(d)
        missing = sorted({s - s % 60 for s in range(a.start, a.end, 30)} - allowed)
        if missing:
            add("OUTSIDE_ACTIVE_HOURS", eid, d, f"shift covers hours not in Demand: {', '.join(map(fmt_minutes, missing))}")

        _check_breaks(data, a, t.duration_hours, lambda rule, msg: add(rule, eid, d, msg))

    for (eid, d), lst in per_emp_day.items():
        if len(lst) > 1:
            add("MORE_THAN_ONE_SHIFT_PER_DAY", eid, d, f"{len(lst)} shifts on the same day")

    by_emp = defaultdict(list)
    for a in schedule.assignments:
        by_emp[a.employee_id].append(a)
    for eid in data.employees:
        shifts = by_emp.get(eid, [])
        n = len(shifts)
        if n > MAX_SHIFTS_PER_WEEK:
            add("TOO_MANY_SHIFTS", eid, None, f"{n} shifts in the week, maximum is {MAX_SHIFTS_PER_WEEK}")
        if n < MIN_SHIFTS_PER_WEEK and eid not in schedule.min_shifts_exempt:
            add("TOO_FEW_SHIFTS", eid, None, f"{n} shifts in the week, minimum is {MIN_SHIFTS_PER_WEEK}")
        paid_min = round(sum(a.paid_hours for a in shifts) * 60)
        if paid_min > MAX_PAID_MINUTES_PER_WEEK:
            add("OVER_40_HOURS", eid, None, f"{paid_min / 60:g} paid hours (breaks included), maximum is 40")

    rep.metrics = compute_metrics(data, schedule, group_size)
    return rep


def _check_breaks(data: InputData, a, duration_hours: int, add) -> None:
    rule = data.break_rules.get(duration_hours)
    if rule is None:
        add("NO_BREAK_RULE", f"no break rule for {duration_hours} h shifts")
        return
    br = sorted(a.breaks, key=lambda b: b.start)
    if len(br) != rule.break_count:
        add("BREAK_COUNT", f"{len(br)} breaks, a {duration_hours} h shift needs {rule.break_count}")
    for b in br:
        if b.end - b.start != rule.break_minutes:
            add("BREAK_LENGTH", f"break {fmt_minutes(b.start)}-{fmt_minutes(b.end)} is not {rule.break_minutes} minutes")
        if not (is_slot_aligned(b.start) and is_slot_aligned(b.end)):
            add("BREAK_NOT_ALIGNED", f"break {fmt_minutes(b.start)} is not on a 30-minute boundary")
        if b.start < a.start or b.end > a.end:
            add("BREAK_OUTSIDE_SHIFT", f"break {fmt_minutes(b.start)}-{fmt_minutes(b.end)} is outside the shift")
    if not br:
        return
    if br[0].start < a.start + rule.min_start_gap:
        add("BREAK_TOO_EARLY", f"first break starts {fmt_minutes(br[0].start)}, "
                               f"earliest allowed is {fmt_minutes(a.start + rule.min_start_gap)}")
    if br[-1].end > a.end - rule.min_end_gap:
        add("BREAK_TOO_LATE", f"last break ends {fmt_minutes(br[-1].end)}, "
                              f"latest allowed is {fmt_minutes(a.end - rule.min_end_gap)}")
    for prev, nxt in zip(br, br[1:]):
        if nxt.start - prev.end < rule.min_between_breaks:
            add("BREAKS_TOO_CLOSE", f"breaks {fmt_minutes(prev.start)} and {fmt_minutes(nxt.start)} have less than "
                                    f"{rule.min_between_breaks} minutes of work between them")
