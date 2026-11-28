"""Single-replacement repair (SCRUM-41, spec section 3, repair step 2).

"First look for an available replacement who can take the same shift template while
meeting every constraint. Breaks may be adjusted in future shifts that are not locked.
Among the replacements, choose by the repair priorities": shortage, then the number of
changed (employee, date) pairs, then cost.

The model is the weekly model (engine/model.py) with the rest of the week held in place:

  * every kept shift stays on its template (x = 1); shifts that are started, completed or
    locked also keep their breaks (b fixed);
  * for every removed (absent) shift exactly one other employee takes the same template
    that day; the absent employee gets nothing that day;
  * no other shift is added or removed, so a feasible solution is a single replacement.

Every replacement changes exactly two pairs per absence (the absent employee's shift
removed, the replacement's added) and costs the same template cost, so among valid
candidates only the shortage differs, through who replaces and where breaks go. Stage 1
minimizes shortage; stage 2, never worse on shortage, moves as few existing breaks as
possible (break moves are listed but do not count as changes, spec section 3).

The proof covers the single-replacement scope only (spec section 9): it says that no
other single replacement is better, not that no broader repair is.

Like model.py, this module must stay independent of shiftopt.validator.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from shiftopt.domain import Assignment, InputData, Schedule
from shiftopt.engine.model import ScheduleModel
from shiftopt.engine.solver import (
    DEFAULT_SEED,
    DEFAULT_WORKERS,
    MIN_STAGE_SECONDS,
    _FirstSolution,
    _new_solver,
    _shortage_of,
    solver_info,
)

SCOPE_NOTE = "Optimality is proven within the single-replacement scope only (spec section 9)."


@dataclass
class Replacement:
    absent_employee: str
    date: dt.date
    shift_id: str
    replacement_employee: str


@dataclass
class RepairResult:
    """Same fields as engine.solver.SolveResult, plus what a repair adds."""
    status: str                         # OPTIMAL, FEASIBLE, NO_REPLACEMENT, NO_SOLUTION_FOUND, ERROR
    schedule: Schedule | None
    time_budget_seconds: float
    first_feasible_seconds: float | None
    optimization_seconds: float
    total_shortage_slots: int | None
    total_cost: float | None
    coverage_proven: bool | None
    cost_proven: bool | None
    stability_proven: bool | None
    termination_reason: str
    replacements: list[Replacement] = field(default_factory=list)
    moved_breaks: int | None = None
    diagnostics: list[str] = field(default_factory=list)
    solver_info: dict = field(default_factory=dict)


def single_replacement(data: InputData, kept: list[Assignment], removed: list[Assignment],
                       fixed: set[tuple[str, dt.date]], blocked: set[tuple[str, dt.date]],
                       min_shifts_exempt: set[str], time_budget_s: float, *,
                       workers: int = DEFAULT_WORKERS, seed: int = DEFAULT_SEED) -> RepairResult:
    t0 = time.perf_counter()
    info = solver_info(workers, seed)
    elapsed = lambda: time.perf_counter() - t0  # noqa: E731
    remaining = lambda: time_budget_s - elapsed()  # noqa: E731

    def finish(**kw) -> RepairResult:
        for k in ("schedule", "first_feasible_seconds", "total_shortage_slots", "total_cost", "coverage_proven",
                  "cost_proven", "stability_proven"):
            kw.setdefault(k, None)
        return RepairResult(time_budget_seconds=time_budget_s, optimization_seconds=elapsed(), solver_info=info, **kw)

    try:
        sm = ScheduleModel(data, frozenset(min_shifts_exempt))
        m = sm.model
        kept_keys = {(a.employee_id, a.shift_id) for a in kept}
        for a in kept:
            m.Add(sm.x[(a.employee_id, a.shift_id)] == 1)
            if (a.employee_id, a.date) in fixed:
                for br in a.breaks:
                    m.Add(sm.b[(a.employee_id, a.shift_id, br.start)] == 1)
        candidate_keys: set[tuple[str, str]] = set()
        candidates_of: dict[str, list[tuple[str, str]]] = {}
        working = {(a.employee_id, a.date) for a in kept}
        absent_on = defaultdict(set)
        for r in removed:
            absent_on[r.shift_id].add(r.employee_id)
        # One new shift per removed shift. Two absences on the same template need two
        # different replacements, so the count is per template.
        for sid, absent in absent_on.items():
            date = next(r.date for r in removed if r.shift_id == sid)
            # A candidate is free that day: an employee who already works it would satisfy
            # the count with his own kept shift and add no one.
            cands = [(e, sid) for e in data.employees
                     if e not in absent and (e, date) not in blocked and (e, date) not in working and (e, sid) in sm.x]
            candidates_of[sid] = cands
            candidate_keys.update(cands)
            m.Add(sum(sm.x[k] for k in cands) == len(absent))
        for k, xv in sm.x.items():
            if k not in kept_keys and k not in candidate_keys:
                m.Add(xv == 0)
    except Exception as e:  # a bug, not a statement about the week
        return finish(status="ERROR", termination_reason=f"MODEL_BUILD_FAILED: {e}")

    # ---- stage 1: shortage
    m.Minimize(sm.total_shortage)
    s1 = _new_solver(remaining(), workers, seed)
    cb = _FirstSolution(t0)
    st1 = s1.Solve(m, cb)
    if st1 == cp_model.INFEASIBLE:
        return finish(status="NO_REPLACEMENT", termination_reason="NO_SINGLE_REPLACEMENT",
                      diagnostics=["No single employee can take the absent shift without breaking a rule "
                                   "(availability, one shift a day, 5 shifts, 40 hours, or the shift is not free)."])
    if st1 == cp_model.MODEL_INVALID:
        return finish(status="ERROR", termination_reason="MODEL_INVALID")
    if st1 == cp_model.UNKNOWN:
        return finish(status="NO_SOLUTION_FOUND", termination_reason="TIME_LIMIT_NO_SOLUTION")

    best_shortage = round(s1.ObjectiveValue())
    coverage_proven = st1 == cp_model.OPTIMAL
    solver_used, first = s1, cb.first

    # ---- stage 2: keep the shortage, move as few existing breaks as possible
    moves = [1 - sm.b[(a.employee_id, a.shift_id, br.start)]
             for a in kept if (a.employee_id, a.date) not in fixed for br in a.breaks]
    moved = int(sum(s1.Value(v) for v in moves)) if moves else 0
    if moves and remaining() >= MIN_STAGE_SECONDS:
        m.Add(sm.total_shortage <= best_shortage)
        sm.hint_from(s1)
        m.Minimize(sum(moves))
        s2 = _new_solver(remaining(), workers, seed)
        st2 = s2.Solve(m)
        if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE) and round(s2.ObjectiveValue()) <= moved:
            solver_used, moved = s2, round(s2.ObjectiveValue())

    schedule = sm.extract(solver_used)
    locked = {(a.employee_id, a.date) for a in kept if a.is_locked}
    for a in schedule.assignments:
        a.is_locked = (a.employee_id, a.date) in locked
    replacements = []
    chosen = {sid: [e for (e, _) in cands if solver_used.Value(sm.x[(e, sid)])] for sid, cands in candidates_of.items()}
    for r in removed:
        replacements.append(Replacement(r.employee_id, r.date, r.shift_id, chosen[r.shift_id].pop(0)))

    status = "OPTIMAL" if coverage_proven else "FEASIBLE"
    return finish(status=status, schedule=schedule, first_feasible_seconds=first,
                  total_shortage_slots=_shortage_of(sm, solver_used),
                  total_cost=round(sum(a.cost for a in schedule.assignments), 2),
                  # Changes (2 per absence) and cost (same template) are equal for every candidate.
                  coverage_proven=coverage_proven, cost_proven=coverage_proven, stability_proven=coverage_proven,
                  termination_reason="ALL_OBJECTIVES_PROVEN_SINGLE_REPLACEMENT" if coverage_proven
                  else "TIME_LIMIT_COVERAGE_NOT_PROVEN_SINGLE_REPLACEMENT",
                  replacements=replacements, moved_breaks=moved, diagnostics=[SCOPE_NOTE])
