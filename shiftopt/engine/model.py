"""CP-SAT model of the weekly schedule (SCRUM-28, SCRUM-29).

Decision variables (architecture report, section 8.2):
  x[e, t]     = 1 if employee e works shift template t (each template belongs to one day)
  b[e, t, s]  = 1 if a break of that shift starts at minute s

Hard rules (spec sections 1-2), all posted as constraints:
  * at most one shift per employee per day;
  * 4 to 5 shifts per employee per week (the minimum is lifted only for employees in
    `min_shifts_exempt`, i.e. after a reported absence, spec section 3);
  * at most 40 paid hours per week, breaks included;
  * no shift on a declared unavailable day (such x variables are never created);
  * only templates from the input, so start, duration and day are always legal, and the
    import already guarantees every template lies inside the active hours of its day;
  * break count, length, earliest start, latest end and spacing from BreakRules.

Coverage is counted per 30-minute slot. An employee is active in a slot of his shift
unless a break covers that slot:
    active[d, s] = sum x[e, t] over shifts covering s  -  sum b[e, t, s'] over breaks covering s
    shortage[d, s] >= required[d, s] - active[d, s],  shortage >= 0
Surplus is not modelled: it never offsets shortage elsewhere (spec section 2).

This module only builds the model. Solving in lexicographic stages is in solver.py.
It must stay independent of shiftopt.validator: the validator re-checks the result.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from shiftopt.domain import (
    MAX_PAID_MINUTES_PER_WEEK,
    MAX_SHIFTS_PER_WEEK,
    MIN_SHIFTS_PER_WEEK,
    SLOT_MINUTES,
    Assignment,
    Break,
    InputData,
    Schedule,
    ShiftTemplate,
)


def money_to_units(cost: float) -> int:
    """CP-SAT needs integer coefficients. Costs have at most two decimals (spec section 6),
    so whole cents are exact."""
    return round(cost * 100)


def break_start_options(t: ShiftTemplate, rule) -> list[int]:
    """Every legal start minute for one break of template t:
    start >= shift start + MinStartGap and end <= shift end - MinEndGap, on 30-minute steps."""
    first = t.start + rule.min_start_gap
    last = t.end - rule.min_end_gap - rule.break_minutes
    return list(range(first, last + 1, SLOT_MINUTES)) if last >= first else []


@dataclass
class ScheduleModel:
    data: InputData
    min_shifts_exempt: frozenset[str] = frozenset()
    model: cp_model.CpModel = field(default_factory=cp_model.CpModel)
    x: dict[tuple[str, str], cp_model.IntVar] = field(default_factory=dict)
    b: dict[tuple[str, str, int], cp_model.IntVar] = field(default_factory=dict)
    shortage: dict[tuple[dt.date, int], cp_model.IntVar] = field(default_factory=dict)
    total_shortage: cp_model.LinearExpr | None = None
    total_cost_units: cp_model.LinearExpr | None = None

    def __post_init__(self):
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        d, m = self.data, self.model

        # Shift variables. Unavailable days are excluded by never creating the variable.
        for e in d.employees.values():
            for t in d.templates.values():
                if t.day_of_week in e.unavailable_days:
                    continue
                self.x[(e.employee_id, t.shift_id)] = m.NewBoolVar(f"x[{e.employee_id},{t.shift_id}]")

        # Break variables and the break rules of each shift.
        for (eid, sid), xv in self.x.items():
            t = d.templates[sid]
            rule = d.break_rules[t.duration_hours]
            options = break_start_options(t, rule)
            bs = []
            for s in options:
                bv = m.NewBoolVar(f"b[{eid},{sid},{s}]")
                self.b[(eid, sid, s)] = bv
                bs.append((s, bv))
            # Exactly BreakCount breaks if the shift is worked, none otherwise.
            m.Add(sum(bv for _, bv in bs) == rule.break_count * xv)
            # Two breaks must leave a full MinBetweenBreaks of work between them,
            # which also keeps them from overlapping.
            gap = rule.break_minutes + rule.min_between_breaks
            for i, (si, bi) in enumerate(bs):
                for sj, bj in bs[i + 1:]:
                    if sj - si < gap:
                        m.AddBoolOr([bi.Not(), bj.Not()])

        # Per-employee limits.
        by_emp: dict[str, list[tuple[ShiftTemplate, cp_model.IntVar]]] = defaultdict(list)
        for (eid, sid), xv in self.x.items():
            by_emp[eid].append((d.templates[sid], xv))
        for eid in d.employees:
            shifts = by_emp.get(eid, [])
            per_day = defaultdict(list)
            for t, xv in shifts:
                per_day[t.day_of_week].append(xv)
            for xs in per_day.values():
                m.AddAtMostOne(xs)
            n = sum(xv for _, xv in shifts)
            m.Add(n <= MAX_SHIFTS_PER_WEEK)
            if eid not in self.min_shifts_exempt:
                m.Add(n >= MIN_SHIFTS_PER_WEEK)
            m.Add(sum(t.paid_minutes * xv for t, xv in shifts) <= MAX_PAID_MINUTES_PER_WEEK)

        # Coverage per demand slot.
        working = defaultdict(list)   # (date, slot) -> x vars of shifts covering the slot
        on_break = defaultdict(list)  # (date, slot) -> b vars of breaks covering the slot
        for (eid, sid), xv in self.x.items():
            t = d.templates[sid]
            date = d.date_for_day(t.day_of_week)
            for s in range(t.start, t.end, SLOT_MINUTES):
                working[(date, s)].append(xv)
        for (eid, sid, s), bv in self.b.items():
            t = d.templates[sid]
            date = d.date_for_day(t.day_of_week)
            rule = d.break_rules[t.duration_hours]
            for c in range(s, s + rule.break_minutes, SLOT_MINUTES):
                on_break[(date, c)].append(bv)

        for (date, hour), dh in d.demand.items():
            for s in (hour, hour + SLOT_MINUTES):
                req = dh.required
                sv = m.NewIntVar(0, req, f"short[{date},{s}]")
                active = sum(working[(date, s)]) - sum(on_break[(date, s)])
                m.Add(sv >= req - active)
                self.shortage[(date, s)] = sv

        self.total_shortage = sum(self.shortage.values())
        self.total_cost_units = sum(money_to_units(d.templates[sid].cost) * xv for (eid, sid), xv in self.x.items())

    # ------------------------------------------------------------------ helpers
    def size(self) -> dict[str, int]:
        return {"shift_vars": len(self.x), "break_vars": len(self.b), "coverage_slots": len(self.shortage)}

    def hint_from(self, solver: cp_model.CpSolver) -> None:
        """Start the next stage from the best solution of the previous one."""
        self.model.ClearHints()
        for v in list(self.x.values()) + list(self.b.values()):
            self.model.AddHint(v, solver.Value(v))

    def extract(self, solver: cp_model.CpSolver) -> Schedule:
        d = self.data
        breaks_of = defaultdict(list)
        for (eid, sid, s), bv in self.b.items():
            if solver.Value(bv):
                rule = d.break_rules[d.templates[sid].duration_hours]
                breaks_of[(eid, sid)].append(Break(s, s + rule.break_minutes))
        sched = Schedule(min_shifts_exempt=set(self.min_shifts_exempt))
        for (eid, sid), xv in self.x.items():
            if solver.Value(xv):
                t = d.templates[sid]
                sched.assignments.append(Assignment(
                    date=d.date_for_day(t.day_of_week), employee_id=eid, shift_id=sid,
                    start=t.start, end=t.end, breaks=tuple(sorted(breaks_of[(eid, sid)], key=lambda b: b.start)),
                    paid_hours=float(t.duration_hours), cost=t.cost))
        sched.assignments.sort(key=lambda a: (a.date, a.employee_id))
        return sched
