"""Command-line entry point (SCRUM-17). Runs without the chat client (spec section 4),
so the optimizer can be measured in isolation (spec section 9).

    shiftopt import samples/Input_Small_10.xlsx
    shiftopt solve --time-budget 60 [--workers 8] [--seed 0] [--input FILE]
    shiftopt runs
    shiftopt show-run RUN-0001
    shiftopt validate samples/Input_Small_10.xlsx samples/Output_Small_10.xlsx
    shiftopt export RUN-0001 out.xlsx        (or a schedule version, SV-0001)
    shiftopt approve SV-0001                 (make a draft the active schedule)
    shiftopt log [--limit 20]                (recorded approvals and changes, newest first)
    shiftopt lock E003 2026-11-04            (lock a future shift of the active schedule; also: Wednesday)
    shiftopt unlock E003 2026-11-04
    shiftopt --now 2026-11-03T07:00 ...      (fixed current time for demos; default: the computer's clock)

Exit codes: 0 success, 1 a schedule failed validation, no schedule was produced or an
approval or a lock was refused,
2 the input file is invalid or missing, or the run to export does not exist.
"""
from __future__ import annotations

import argparse
import sys

from shiftopt import clock
from shiftopt.db import Store
from shiftopt.engine.solver import DEFAULT_SEED, DEFAULT_WORKERS
from shiftopt.export import ExportError, export_run
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.service import ApprovalRefused, NoActiveInput, approve_draft, run_initial
from shiftopt.service.locks import LockRefused, set_lock
from shiftopt.service.resolve import Unresolved, resolve_date, resolve_employee
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


def _cmd_export(a) -> int:
    try:
        out = export_run(Store(a.db), a.ref, a.file)
    except ExportError as e:
        print(f"ERROR  {e}")
        return 2
    print(f"OK  exported {a.ref} to {out}")
    return 0


def _cmd_approve(a) -> int:
    try:
        out = approve_draft(Store(a.db), a.version)
    except ApprovalRefused as e:
        print(f"REFUSED  {e.code}: {e.message}")
        for d in e.details:
            print(f"  {d}")
        return 1
    replaced = f", replaces {out.replaced_version}" if out.replaced_version else ""
    print(f"OK  {out.schedule_version} APPROVED and active at {out.approved_at:%Y-%m-%d %H:%M:%S}{replaced}")
    return 0


def _cmd_log(a) -> int:
    entries = Store(a.db).list_audit(limit=a.limit)
    if not entries:
        print("No changes recorded yet.")
        return 0
    for e in entries:
        replaced = f" (replaced {e['replaced_version']})" if e["replaced_version"] else ""
        d = e["details"]
        cost = d.get("total_cost")
        extra = "" if cost is None else f"  cost {cost:,.2f}, shortage {d.get('total_shortage_slots')}"
        if d.get("employee_id"):
            extra = f"  {d['employee_id']} {d['date']} {d.get('shift_id', '')}"
        print(f"{e['entry_id']}  {e['at']:%Y-%m-%d %H:%M:%S}  {e['action']:<9} {e['schedule_version']}{replaced}"
              f"  {e['run_id'] or ''}{extra}")
    return 0


def _cmd_lock(a) -> int:
    store = Store(a.db)
    active = store.active_schedule_version()
    if active is None:
        print("REFUSED  NOT_FOUND: there is no active schedule yet; approve a draft first.")
        return 1
    data = store.load_input(store.schedule_version_info(active)["input_version"])
    locked = a.cmd == "lock"
    try:
        out = set_lock(store, resolve_employee(data, a.employee), resolve_date(data, a.date), locked)
    except Unresolved as e:
        print(f"REFUSED  {e.code}: {e.message}")
        if e.candidates:
            print("  choices: " + ", ".join(e.candidates))
        return 1
    except LockRefused as e:
        print(f"REFUSED  {e.code}: {e.message}")
        return 1
    state = "locked" if out.is_locked else "unlocked"
    note = "" if out.changed else f" (already {state}; nothing recorded)"
    print(f"OK  {out.employee_id} {out.date.isoformat()} {out.shift_id} {out.start}-{out.end} in "
          f"{out.schedule_version} is {state}{note}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shiftopt")
    p.add_argument("--db", default="shiftopt.db", help="SQLite file or SQLAlchemy URL (default shiftopt.db)")
    p.add_argument("--now", default=None, help="fixed current time, e.g. 2026-11-03T07:00 (env SHIFTOPT_NOW; "
                                               "default: the computer's clock)")
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

    pe = sub.add_parser("export", help="write a run or schedule version as an Excel file in the output schema")
    pe.add_argument("ref", help="run ID (RUN-0001) or schedule version (SV-0001)")
    pe.add_argument("file", help="output .xlsx path")
    pe.set_defaults(fn=_cmd_export)

    pa = sub.add_parser("approve", help="approve a draft so that it becomes the active schedule")
    pa.add_argument("version", help="schedule version, e.g. SV-0001")
    pa.set_defaults(fn=_cmd_approve)

    pl = sub.add_parser("log", help="list recorded approvals and changes, newest first")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(fn=_cmd_log)

    for name in ("lock", "unlock"):
        pk = sub.add_parser(name, help=f"{name} a future shift of the active schedule")
        pk.add_argument("employee", help="employee ID or name")
        pk.add_argument("date", help="date (2026-11-04) or day name in the planning week")
        pk.set_defaults(fn=_cmd_lock)

    a = p.parse_args(argv)
    if a.cmd == "solve" and a.time_budget <= 0:
        p.error("--time-budget must be positive")
    if a.now:
        try:
            clock.set_now(a.now)
        except ValueError as e:
            p.error(str(e))
    try:
        return a.fn(a)
    finally:
        if a.now:
            clock.set_now(None)   # a fixed time applies to this command only


if __name__ == "__main__":
    sys.exit(main())
