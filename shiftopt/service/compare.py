"""What a draft would change, and how the numbers move (SCRUM-43, spec section 3, step 4).

"The system shows the draft, a comparison to the active schedule and the effect on the
metrics. The active schedule changes only after the manager explicitly approves it."

A change (spec section 3) is an (employee, date) pair whose shift template changed, was
added or was removed. A template swap on the same day counts once; removal from the
absent employee and addition to the replacement count as two. A break moved within the
same shift does not count, but is listed separately in the details. ChangedAssignments in
RunSummary is the number of changes.

The numbers before and after are computed by the independent checker (validator.metrics)
from each schedule, never taken from the engine.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from shiftopt.db import Store
from shiftopt.domain import Assignment, Schedule, fmt_minutes
from shiftopt.service.resolve import Unresolved, resolve_draft
from shiftopt.validator.metrics import compute_metrics

KINDS = ("REMOVED", "ADDED", "TEMPLATE_CHANGED")


def _shift_text(a: Assignment | None) -> str | None:
    return None if a is None else f"{a.shift_id} {fmt_minutes(a.start)}-{fmt_minutes(a.end)}"


def _breaks_text(a: Assignment) -> list[str]:
    return [f"{fmt_minutes(b.start)}-{fmt_minutes(b.end)}" for b in a.breaks]


@dataclass
class Change:
    employee_id: str
    date: dt.date
    kind: str                    # REMOVED, ADDED or TEMPLATE_CHANGED
    before: str | None           # "S4 08:00-12:00"
    after: str | None


@dataclass
class BreakMove:
    employee_id: str
    date: dt.date
    shift_id: str
    before: list[str]
    after: list[str]


@dataclass
class ScheduleDiff:
    changes: list[Change] = field(default_factory=list)
    break_moves: list[BreakMove] = field(default_factory=list)

    @property
    def changed_assignments(self) -> int:
        return len(self.changes)


def diff_schedules(before: Schedule, after: Schedule) -> ScheduleDiff:
    b = {(a.employee_id, a.date): a for a in before.assignments}
    n = {(a.employee_id, a.date): a for a in after.assignments}
    out = ScheduleDiff()
    for key in sorted(b.keys() | n.keys(), key=lambda k: (k[1], k[0])):
        old, new = b.get(key), n.get(key)
        if old is None:
            out.changes.append(Change(key[0], key[1], "ADDED", None, _shift_text(new)))
        elif new is None:
            out.changes.append(Change(key[0], key[1], "REMOVED", _shift_text(old), None))
        elif (old.shift_id, old.start, old.end) != (new.shift_id, new.start, new.end):
            out.changes.append(Change(key[0], key[1], "TEMPLATE_CHANGED", _shift_text(old), _shift_text(new)))
        elif old.breaks != new.breaks:
            out.break_moves.append(BreakMove(key[0], key[1], old.shift_id, _breaks_text(old), _breaks_text(new)))
    return out


@dataclass
class Totals:
    shortage_slots: int
    shortage_hours: float
    surplus_slots: int
    cost: float
    shifts: int
    paid_hours: float


@dataclass
class DayDelta:
    date: dt.date
    shortage_before: int
    shortage_after: int
    cost_before: float
    cost_after: float


@dataclass
class Comparison:
    draft: str
    draft_status: str            # DRAFT, APPROVED
    run_type: str
    compared_with: str | None    # the base of a repair draft, else the active schedule
    compared_with_is_active: bool
    before: Totals | None
    after: Totals
    diff: ScheduleDiff
    days: list[DayDelta]
    min_shifts_exempt: list[str]
    open_absences: list[str]
    note: str = ""


def _totals(m, schedule: Schedule) -> Totals:
    return Totals(shortage_slots=m.total_shortage_slots, shortage_hours=m.total_shortage_slots / 2,
                  surplus_slots=m.total_surplus_slots, cost=round(m.total_cost, 2), shifts=len(schedule.assignments),
                  paid_hours=m.total_paid_hours)


def compare(store: Store, ref: str | None = None) -> Comparison:
    """Compare a draft (default: the latest draft, AMBIGUOUS if several) with the schedule it
    would replace: a repair draft with its base, any other draft with the active schedule."""
    version = resolve_draft(store, ref)
    info = store.schedule_version_info(version)
    run = store.get_run(info["run_id"])
    data = store.load_input(info["input_version"])
    after_s = store.load_schedule(version)
    other = info["base_version"] or store.active_schedule_version()
    if other == version:
        raise Unresolved("INVALID_PARAMETER", f"{version} is the active schedule; name a draft to compare with it.",
                         [])
    before_s = store.load_schedule(other) if other else None

    m_after = compute_metrics(data, after_s, group_size=run.group_size)
    m_before = compute_metrics(data, before_s, group_size=run.group_size) if before_s else None

    def day_costs(s: Schedule) -> dict[dt.date, float]:
        out: dict[dt.date, float] = {}
        for a in s.assignments:
            out[a.date] = out.get(a.date, 0.0) + a.cost
        return out

    def day_short(m) -> dict[dt.date, int]:
        out: dict[dt.date, int] = {}
        for r in m.coverage:
            out[r.date] = out.get(r.date, 0) + r.shortage
        return out

    days = []
    if m_before is not None:
        sb, sa, cb, ca = day_short(m_before), day_short(m_after), day_costs(before_s), day_costs(after_s)
        for d in data.week_dates:
            if (sb.get(d, 0), round(cb.get(d, 0.0), 2)) != (sa.get(d, 0), round(ca.get(d, 0.0), 2)):
                days.append(DayDelta(d, sb.get(d, 0), sa.get(d, 0), round(cb.get(d, 0.0), 2),
                                     round(ca.get(d, 0.0), 2)))

    open_abs = [f"{a['absence_id']} {a['employee_id']} {a['date'].isoformat()}"
                for a in store.list_absences(status="OPEN", input_version=info["input_version"])]
    note = ("" if before_s is not None else "There is no active schedule yet, so there is nothing to compare with; "
            "these are the draft's own numbers.")
    return Comparison(draft=version, draft_status=info["approval_status"], run_type=run.run_type,
                      compared_with=other, compared_with_is_active=other is not None and
                      other == store.active_schedule_version(),
                      before=None if m_before is None else _totals(m_before, before_s),
                      after=_totals(m_after, after_s),
                      diff=diff_schedules(before_s, after_s) if before_s else ScheduleDiff(),
                      days=days, min_shifts_exempt=sorted(after_s.min_shifts_exempt), open_absences=open_abs,
                      note=note)
