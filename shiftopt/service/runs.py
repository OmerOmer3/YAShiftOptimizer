"""Run service: solve, check independently, store the run and its draft (SCRUM-23).

This is the one place where the optimizer and the independent validator meet. The
engine's answer is never trusted on its own: every schedule is re-checked, and a schedule
that fails the check is not saved as a draft (spec section 4, "do not rely on ... that a
schedule is valid"; ValidationPassed in RunSummary).

Timing follows spec section 9: measurement starts when optimization is entered on data
that was already imported and validated, and includes model building, solving and the
independent check. Import, export and the language model are excluded.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

from shiftopt.db.store import RunRecord, Store
from shiftopt.engine import solve
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.validator import validate

GROUP_SIZE = 2  # pair team: no preference objective (spec, "scope" table)


class NoActiveInput(Exception):
    """There is no imported input to plan on."""


@dataclass
class RunOutcome:
    run: RunRecord
    violations: list[str]


def run_initial(store: Store, time_budget_s: float, *, input_version: str | None = None,
                workers: int = DEFAULT_WORKERS, seed: int = DEFAULT_SEED) -> RunOutcome:
    version = input_version or store.active_input_version()
    data = store.load_input(version) if version else None
    if data is None:
        raise NoActiveInput("No input has been imported yet" if version is None else f"Input version {version} not found")

    started_at = dt.datetime.now()
    t0 = time.perf_counter()
    result = solve(data, time_budget_s, workers=workers, seed=seed)
    status, reason, diagnostics = result.status, result.termination_reason, list(result.diagnostics)
    passed, violations, schedule = None, [], None
    if result.schedule is not None:
        report = validate(data, result.schedule, group_size=GROUP_SIZE)
        passed = report.passed
        if passed:
            schedule = result.schedule
        else:
            # A schedule the checker rejects is a bug in our code, not a property of the week.
            violations = [str(v) for v in report.violations]
            status, reason = "ERROR", "VALIDATION_FAILED"
            diagnostics.append(f"The independent checker rejected the solver's schedule ({len(violations)} violations).")
    optimization_seconds = time.perf_counter() - t0

    run_id, _ = store.save_run(input_version=version, run_type="INITIAL", group_size=GROUP_SIZE, result=result,
                               validation_passed=passed, optimization_seconds=optimization_seconds,
                               started_at=started_at, status=status, termination_reason=reason,
                               diagnostics=diagnostics, schedule=schedule)
    return RunOutcome(store.get_run(run_id), violations)
