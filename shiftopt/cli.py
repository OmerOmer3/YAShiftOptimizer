"""Command-line entry point. Runs without the chat client (spec section 4).

Sprint 1 provides `import` and `validate`; `solve` and `export` arrive with
SCRUM-17 in sprint 2.

    shiftopt import samples/Input_Small_10.xlsx --db shiftopt.db
    shiftopt validate samples/Input_Small_10.xlsx samples/Output_Small_10.xlsx
"""
from __future__ import annotations

import argparse
import sys

from shiftopt.db import Store
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.validator import validate


def _cmd_import(a) -> int:
    out = import_workbook(Store(a.db), a.file)
    if out.status != "OK":
        print("INVALID_INPUT")
        for e in out.errors:
            print(f"  {e}")
        return 2
    print(f"OK  input version {out.input_version} is now active")
    return 0


def _cmd_validate(a) -> int:
    res = read_input(a.input)
    if not res.ok:
        print("INVALID_INPUT")
        for e in res.errors:
            print(f"  {e}")
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
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("import", help="validate an input workbook and store it as the active input version")
    pi.add_argument("file")
    pi.add_argument("--db", default="shiftopt.db")
    pi.set_defaults(fn=_cmd_import)
    pv = sub.add_parser("validate", help="check a schedule workbook against an input workbook")
    pv.add_argument("input")
    pv.add_argument("schedule")
    pv.add_argument("--group-size", type=int, default=2, choices=[2, 3])
    pv.set_defaults(fn=_cmd_validate)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
