"""Command-line entry point (SCRUM-17). Runs without the chat client (spec section 4),
so the optimizer can be measured in isolation (spec section 9).

    shiftopt import samples/Input_Small_10.xlsx
    shiftopt solve --time-budget 60 [--workers 8] [--seed 0] [--input FILE]
    shiftopt runs
    shiftopt show-run RUN-0001
    shiftopt validate samples/Input_Small_10.xlsx samples/Output_Small_10.xlsx

Exit codes: 0 success, 1 a schedule failed validation or no schedule was produced,
2 the input file is invalid or missing.
Export (SCRUM-44) is added in sprint 3.
"""
from __future__ import annotations

import argparse
import sys

from shiftopt.db import Store
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.service import NoActiveInput, run_initial
from shiftopt.validator import validate


def _flag(v) -> str:
    return "" if v is None else ("TRUE" if v else "FALSE")


def _num(v, fmt="{:g}") -> str:
    """Unavailable metrics are printed empty, not as zero (spec section 9)."""
    return "" if v is None else fmt.format(v)


def _print_import_errors(errors) -> None:
    print("INVALID_INPUT")
    for e in errors:
        print(f"  {e}")


def _cmd_import(a) -> int:
    out = import_workbook(Store(a.db), a.file)
    if out.status != "OK":
        _print_import_errors(out.errors)
        return 2
    print(f"OK  input version {out.input_version} is now active")
    return 0


def _print_run(r) -> None:
    rows = [
        ("RunID", r.run_id), ("InputVersion", r.input_version), ("RunType", r.run_type),
        ("GroupSize", r.group_size), ("SolutionStatus", r.status), ("ApprovalStatus", r.approval_status or ""),
        ("ScheduleVersion", r.schedule_version or ""), ("BaseScheduleVersion", r.base_schedule_version or ""),
        ("TimeBudgetSeconds", _num(r.time_budget_seconds)),
        ("FirstFeasibleSeconds", _num(r.first_feasible_seconds, "{:.2f}")),
        ("OptimizationSeconds", _num(r.optimization_seconds, "{:.2f}")),
        ("TotalShortageSlots", _num(r.total_shortage_slots)),
        ("TotalCost", _num(r.total_cost, "{:,.2f}")),
        ("CoverageOptimalityProven", _flag(r.coverage_proven)),
        ("CostOptimalityProven", _flag(r.cost_proven)),
        ("TerminationReason", r.termination_reason),
        ("ValidationPassed", _flag(r.validation_passed)),
    ]
    for k, v in rows:
        print(f"{k:<26}{v}")
    si = r.solver_info
    print(f"{'Solver':<26}{si.get('solver')} {si.get('solver_version')}, workers={si.get('num_workers')}, "
          f"seed={si.get('random_seed')}")
    print(f"{'Machine':<26}{si.get('platform')}, {si.get('cpu_count')} CPUs")
    for d in r.diagnostics:
        print(f"  ! {d}")


def _cmd_solve(a) -> int:
    store = Store(a.db)
    if a.input:
        out = import_workbook(store, a.input)
        if out.status != "OK":
            _print_import_errors(out.errors)
            return 2
    try:
        outcome = run_initial(store, a.time_budget, workers=a.workers, seed=a.seed)
    except NoActiveInput as e:
        print(f"ERROR  {e}. Run 'shiftopt import FILE' first.")
        return 2
    _print_run(outcome.run)
    for v in outcome.violations:
        print(f"  {v}")
    return 0 if outcome.run.schedule_version else 1


def _cmd_runs(a) -> int:
    runs = Store(a.db).list_runs()
    if not runs:
        print("No runs yet.")
        return 0
    print(f"{'RunID':<10}{'Input':<9}{'Status':<19}{'Shortage':>9}{'Cost':>12}{'Seconds':>9}  Version")
    for r in runs:
        print(f"{r.run_id:<10}{r.input_version:<9}{r.status:<19}{_num(r.total_shortage_slots):>9}"
              f"{_num(r.total_cost, '{:,.2f}'):>12}{r.optimization_seconds:>9.2f}  "
              f"{r.schedule_version or '-'} {r.approval_status or ''}")
    return 0


def _cmd_show_run(a) -> int:
    r = Store(a.db).get_run(a.run_id)
    if r is None:
        print(f"ERROR  run {a.run_id} not found")
        return 2
    _print_run(r)
    return 0


def _cmd_validate(a) -> int:
    res = read_input(a.input)
    if not res.ok:
        _print_import_errors(res.errors)
        return 2
    rep = validate(res.data, read_schedule(a.schedule), group_size=a.group_size)
    m = rep.metrics
    print(f"ValidationPassed    {rep.passed}")
    print(f"Shifts              {m.shift_count}")
    print(f"PaidHours           {m.total_paid_hours:g}")
    print(f"TotalCost           {m.total_cost:,.2f}")
    print(f"TotalShortageSlots  {m.total_shortage_slots}  ({m.shortage_employee_hours:g} employee-hours)")
    if m.min_preference_rate is not None:
        print(f"MinPreferenceRate   {m.min_preference_rate:.0%}")
    for v in rep.violations:
        print(f"  {v}")
    return 0 if rep.passed else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shiftopt")
    p.add_argument("--db", default="shiftopt.db", help="SQLite file or SQLAlchemy URL (default shiftopt.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("import", help="validate an input workbook and store it as the active input version")
    pi.add_argument("file")
    pi.set_defaults(fn=_cmd_import)

    ps = sub.add_parser("solve", help="create a draft schedule for the active input within a time budget")
    ps.add_argument("--time-budget", type=float, required=True, help="seconds shared by all stages")
    ps.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"CP-SAT threads (default {DEFAULT_WORKERS})")
    ps.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    ps.add_argument("--input", help="import this workbook first (import time is not measured)")
    ps.set_defaults(fn=_cmd_solve)

    pr = sub.add_parser("runs", help="list stored runs")
    pr.set_defaults(fn=_cmd_runs)

    psr = sub.add_parser("show-run", help="show one run in RunSummary form")
    psr.add_argument("run_id")
    psr.set_defaults(fn=_cmd_show_run)

    pv = sub.add_parser("validate", help="check a schedule workbook against an input workbook")
    pv.add_argument("input")
    pv.add_argument("schedule")
    pv.add_argument("--group-size", type=int, default=2, choices=[2, 3])
    pv.set_defaults(fn=_cmd_validate)

    a = p.parse_args(argv)
    if a.cmd == "solve" and a.time_budget <= 0:
        p.error("--time-budget must be positive")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
