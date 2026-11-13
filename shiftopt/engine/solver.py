"""Lexicographic solve under one shared time budget (SCRUM-28..31).

Stage 1 minimizes total shortage (half-hours of missing employees).
Stage 2 keeps shortage at most the stage-1 value and minimizes total cost.
No weighted sum is used, so cost can never buy shortage (spec section 2).

Both stages share one wall-clock budget that starts when solve() is entered, so model
building counts too (spec section 9). If stage 1 runs out of time before proving its
optimum, stage 2 still runs, bounded by the best shortage found, and the result is
marked FEASIBLE with CoverageOptimalityProven = False (spec section 2, last paragraph).

Statuses (spec section 9):
  OPTIMAL            valid schedule, both objectives proven
  FEASIBLE           valid schedule, not every objective proven
  INFEASIBLE         proven that no schedule satisfies the hard rules
  NO_SOLUTION_FOUND  time ran out with no schedule and no infeasibility proof
  ERROR              technical failure (e.g. invalid model); says nothing about feasibility
INVALID_INPUT is decided earlier, at import, and never reaches the engine.
"""
from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass, field

import ortools
from ortools.sat.python import cp_model

from shiftopt.domain import DAY_NAMES, MIN_SHIFTS_PER_WEEK, InputData, Schedule
from shiftopt.engine.model import ScheduleModel, money_to_units

DEFAULT_WORKERS = 8
DEFAULT_SEED = 0
MIN_STAGE_SECONDS = 0.05  # below this a stage is not started


@dataclass
class StageReport:
    name: str           # "coverage" or "cost"
    cp_status: str      # raw CP-SAT status name
    objective: float | None
    best_bound: float | None
    proven: bool
    seconds: float


@dataclass
class SolveResult:
    status: str
    schedule: Schedule | None
    time_budget_seconds: float
    first_feasible_seconds: float | None
    optimization_seconds: float
    total_shortage_slots: int | None
    total_cost: float | None
    coverage_proven: bool | None
    cost_proven: bool | None
    termination_reason: str
    stages: list[StageReport] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    solver_info: dict = field(default_factory=dict)
    model_size: dict = field(default_factory=dict)

    @property
    def has_schedule(self) -> bool:
        return self.schedule is not None


class _FirstSolution(cp_model.CpSolverSolutionCallback):
    """Records the wall time of the first feasible solution, measured from solve() entry."""

    def __init__(self, t0: float):
        super().__init__()
        self.t0 = t0
        self.first: float | None = None

    def on_solution_callback(self):
        if self.first is None:
            self.first = time.perf_counter() - self.t0


def solver_info(workers: int, seed: int) -> dict:
    """What spec section 9 asks to document with every measured run."""
    return {
        "solver": "OR-Tools CP-SAT",
        "solver_version": ortools.__version__,
        "num_workers": workers,
        "random_seed": seed,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def _new_solver(seconds: float, workers: int, seed: int) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = max(seconds, 0.0)
    s.parameters.num_workers = workers
    s.parameters.random_seed = seed
    return s


def infeasibility_reasons(data: InputData, min_shifts_exempt=frozenset()) -> list[str]:
    """Plain-language reasons that make the week impossible, for the manager.

    Only a necessary-condition check: each non-exempt employee needs at least four
    different days on which some template exists and he is available. When the solver
    proves infeasibility for another reason this returns an empty list and the generic
    message is shown. It never changes the rules (spec section 1: the minimum is not
    lowered automatically)."""
    days_with_templates = {t.day_of_week for t in data.templates.values()}
    out = []
    for e in data.employees.values():
        if e.employee_id in min_shifts_exempt:
            continue
        days = sorted(days_with_templates - set(e.unavailable_days))
        if len(days) < MIN_SHIFTS_PER_WEEK:
            names = ", ".join(DAY_NAMES[x] for x in days) or "none"
            out.append(f"{e.employee_id} can work on only {len(days)} day(s) ({names}) "
                       f"but needs at least {MIN_SHIFTS_PER_WEEK} shifts")
    return out


def solve(data: InputData, time_budget_s: float, *, workers: int = DEFAULT_WORKERS, seed: int = DEFAULT_SEED,
          min_shifts_exempt: frozenset[str] = frozenset()) -> SolveResult:
    t0 = time.perf_counter()
    info = solver_info(workers, seed)
    elapsed = lambda: time.perf_counter() - t0  # noqa: E731
    remaining = lambda: time_budget_s - elapsed()  # noqa: E731

    def finish(**kw) -> SolveResult:
        kw.setdefault("schedule", None)
        kw.setdefault("first_feasible_seconds", None)
        kw.setdefault("total_shortage_slots", None)
        kw.setdefault("total_cost", None)
        kw.setdefault("coverage_proven", None)
        kw.setdefault("cost_proven", None)
        return SolveResult(time_budget_seconds=time_budget_s, optimization_seconds=elapsed(),
                           solver_info=info, **kw)

    try:
        sm = ScheduleModel(data, frozenset(min_shifts_exempt))
    except Exception as e:  # a bug, not a statement about the week
        return finish(status="ERROR", termination_reason=f"MODEL_BUILD_FAILED: {e}")
    size = sm.size()

    # ---- stage 1: coverage
    sm.model.Minimize(sm.total_shortage)
    s1 = _new_solver(remaining(), workers, seed)
    cb = _FirstSolution(t0)
    st1_start = elapsed()
    st1 = s1.Solve(sm.model, cb)
    rep1 = StageReport("coverage", s1.StatusName(st1),
                       s1.ObjectiveValue() if st1 in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
                       s1.BestObjectiveBound() if st1 in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
                       st1 == cp_model.OPTIMAL, elapsed() - st1_start)
    first = cb.first

    if st1 == cp_model.INFEASIBLE:
        reasons = infeasibility_reasons(data, frozenset(min_shifts_exempt))
        return finish(status="INFEASIBLE", termination_reason="INFEASIBILITY_PROVEN", stages=[rep1],
                      diagnostics=reasons or ["The solver proved that no schedule satisfies all hard rules."],
                      model_size=size)
    if st1 == cp_model.MODEL_INVALID:
        return finish(status="ERROR", termination_reason="MODEL_INVALID", stages=[rep1], model_size=size)
    if st1 == cp_model.UNKNOWN:
        # Spec section 9: running out of time is never reported as INFEASIBLE.
        return finish(status="NO_SOLUTION_FOUND", termination_reason="TIME_LIMIT_NO_SOLUTION", stages=[rep1],
                      model_size=size)

    best_shortage = round(s1.ObjectiveValue())
    coverage_proven = st1 == cp_model.OPTIMAL
    best_schedule = sm.extract(s1)
    best_cost_units = round(sum(s1.Value(v) * c for v, c in _cost_terms(sm)))
    cost_proven = False
    stages = [rep1]

    # ---- stage 2: cost, never worse on shortage
    if remaining() >= MIN_STAGE_SECONDS:
        sm.model.Add(sm.total_shortage <= best_shortage)
        sm.hint_from(s1)
        sm.model.Minimize(sm.total_cost_units)
        s2 = _new_solver(remaining(), workers, seed)
        st2_start = elapsed()
        st2 = s2.Solve(sm.model)
        found = st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        stages.append(StageReport("cost", s2.StatusName(st2), s2.ObjectiveValue() / 100 if found else None,
                                  s2.BestObjectiveBound() / 100 if found else None, st2 == cp_model.OPTIMAL,
                                  elapsed() - st2_start))
        if found and round(s2.ObjectiveValue()) <= best_cost_units:
            best_schedule = sm.extract(s2)
            best_cost_units = round(s2.ObjectiveValue())
            # Cost is optimal only if it was optimized against the proven best shortage.
            cost_proven = st2 == cp_model.OPTIMAL and coverage_proven
        if found and first is None:
            first = elapsed()
        stage2_note = s2.StatusName(st2)
    else:
        stage2_note = "SKIPPED_NO_TIME_LEFT"

    status = "OPTIMAL" if coverage_proven and cost_proven else "FEASIBLE"
    if status == "OPTIMAL":
        reason = "ALL_OBJECTIVES_PROVEN"
    elif not coverage_proven:
        reason = "TIME_LIMIT_COVERAGE_NOT_PROVEN"
    else:
        reason = f"TIME_LIMIT_COST_NOT_PROVEN (cost stage: {stage2_note})"
    return finish(status=status, schedule=best_schedule, first_feasible_seconds=first,
                  total_shortage_slots=best_shortage, total_cost=best_cost_units / 100,
                  coverage_proven=coverage_proven, cost_proven=cost_proven, termination_reason=reason,
                  stages=stages, model_size=size)


def _cost_terms(sm: ScheduleModel):
    return [(xv, money_to_units(sm.data.templates[sid].cost)) for (eid, sid), xv in sm.x.items()]
