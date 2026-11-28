"""Repair the active schedule after a reported absence (SCRUM-41, SCRUM-42).

Spec section 3, repair process:
  1. load the active schedule, record the absence, remove the affected shift (SCRUM-40,
     service/absence.py: repair_start);
  2. first look for one replacement for the same shift template (this module, SCRUM-41);
  3. if there is none, re-optimize the future shifts: shortage, then changes, then cost
     (SCRUM-42), with the time left in the same budget;
  4. show the draft; the active schedule changes only after explicit approval.

As with initial runs (service/runs.py), the engine's answer is re-checked by the
independent checker, the engine's totals must match the checker's, and only then is the
schedule saved, as a REPAIR draft whose base is the active schedule. Timing follows spec
section 9: model building, solving and checking, all within one time budget.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

from shiftopt import clock
from shiftopt.db.store import RunRecord, Store
from shiftopt.engine.repair import Replacement, reoptimize, single_replacement
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.service.absence import RepairStart, repair_start
from shiftopt.service.approve import locked_shifts_not_kept
from shiftopt.service.runs import GROUP_SIZE
from shiftopt.validator import validate


@dataclass
class RepairOutcome:
    start: RepairStart
    run: RunRecord | None
    mode: str = "SINGLE_REPLACEMENT"   # or REOPTIMIZED
    replacements: list[Replacement] = field(default_factory=list)
    changed_assignments: int | None = None
    moved_breaks: int | None = None
    message: str = ""
    violations: list[str] = field(default_factory=list)


def run_repair(store: Store, time_budget_s: float, *, workers: int = DEFAULT_WORKERS, seed: int = DEFAULT_SEED,
               now: dt.datetime | None = None) -> RepairOutcome:
    start = repair_start(store, now=now)                 # raises AbsenceRefused if there is nothing to repair
    data = store.load_input(start.input_version)
    started_at = dt.datetime.now()
    t0 = time.perf_counter()
    fixed = {(a.employee_id, a.date) for a in start.fixed}
    result = single_replacement(data, start.schedule.assignments, start.removed, fixed, start.blocked,
                                start.schedule.min_shifts_exempt, time_budget_s, workers=workers, seed=seed)

    mode = "SINGLE_REPLACEMENT"
    if result.status == "NO_REPLACEMENT":
        # Step 3 (spec section 3): change future shifts, with the rest of the same budget.
        mode = "REOPTIMIZED"
        at = start.now or clock.now()
        started = {t.shift_id for t in data.templates.values()
                   if clock.has_started(data.date_for_day(t.day_of_week), t.start, at)}
        active = start.schedule.assignments + start.removed
        spent = time.perf_counter() - t0
        result = reoptimize(data, active, fixed, start.blocked, started, start.schedule.min_shifts_exempt,
                            max(time_budget_s - spent, 0.0), workers=workers, seed=seed)
        result.time_budget_seconds = time_budget_s

    status, reason, diagnostics = result.status, result.termination_reason, list(result.diagnostics)
    passed, violations, schedule = None, [], None
    if result.schedule is not None:
        report = validate(data, result.schedule, group_size=GROUP_SIZE)
        passed = report.passed
        engine = (result.total_shortage_slots, round(result.total_cost or 0, 2))
        checker = (report.metrics.total_shortage_slots, round(report.metrics.total_cost, 2))
        broken = locked_shifts_not_kept(store.load_schedule(start.base_version), result.schedule)
        if passed and engine != checker:
            status, reason = "ERROR", "METRICS_MISMATCH"
            diagnostics.append(f"Engine totals (shortage {engine[0]}, cost {engine[1]:,.2f}) differ from the "
                               f"independent checker (shortage {checker[0]}, cost {checker[1]:,.2f}).")
        elif passed and broken:
            status, reason = "ERROR", "LOCK_VIOLATED"
            diagnostics.extend(broken)
        elif passed:
            schedule = result.schedule
        else:
            violations = [str(v) for v in report.violations]
            status, reason = "ERROR", "VALIDATION_FAILED"
            diagnostics.append(f"The independent checker rejected the repair ({len(violations)} violations).")
    optimization_seconds = time.perf_counter() - t0

    run_id, _ = store.save_run(input_version=start.input_version, run_type="REPAIR", group_size=GROUP_SIZE,
                               result=result, validation_passed=passed, optimization_seconds=optimization_seconds,
                               started_at=started_at, status=status, termination_reason=reason,
                               diagnostics=diagnostics, schedule=schedule, base_version=start.base_version,
                               stability_proven=result.stability_proven)
    run = store.get_run(run_id)
    base = store.load_schedule(start.base_version)
    if run.schedule_version and mode == "REOPTIMIZED":
        n = result.changed_assignments
        message = (f"No single replacement exists, so the future shifts were re-optimized. Repair draft "
                   f"{run.schedule_version} (base {start.base_version}) changes {n} (employee, date) pair(s) "
                   f"and moves {result.moved_breaks or 0} break(s) of other shifts (not counted as changes); "
                   "started, completed and locked shifts are unchanged. The active schedule is unchanged until the "
                   "manager approves the draft.")
    elif run.schedule_version:
        who = "; ".join(f"{r.replacement_employee} takes {r.shift_id} on {r.date.isoformat()} instead of "
                        f"{r.absent_employee}" for r in result.replacements)
        message = (f"Repair draft {run.schedule_version} (base {start.base_version}): {who}. "
                   f"{result.moved_breaks or 0} break(s) of other shifts moved. The active schedule is unchanged "
                   "until the manager approves the draft.")
    elif run.status in ("INFEASIBLE", "NO_SOLUTION_FOUND"):
        message = (f"No repair was found ({run.status}). The active schedule {start.base_version} stays and the "
                   "absence has no approved answer yet. " + " ".join(run.diagnostics[:1]))
    else:
        message = f"The repair run ended with {run.status} ({run.termination_reason}); no draft was saved."
    changed = None
    if schedule is not None:
        from shiftopt.export.service import changed_assignments
        changed = changed_assignments(base, schedule)
    return RepairOutcome(start=start, run=run, mode=mode, replacements=result.replacements if schedule else [],
                         moved_breaks=result.moved_breaks if schedule else None, message=message,
                         violations=violations, changed_assignments=changed)
