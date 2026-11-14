"""Excel input import and validation (SCRUM-18, SCRUM-19, SCRUM-20).

Reads an input workbook in schema 1.0 and either returns a fully typed InputData,
or status INVALID_INPUT with every problem found, each with sheet, Excel row
number, column and a readable reason (spec section 6, "input checks").

Infeasibility is NOT checked here: valid input with no possible schedule
(e.g. only three active days) imports successfully and is reported later by the
engine as INFEASIBLE (spec section 6, last paragraph).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from shiftopt.domain import (
    ALLOWED_DURATIONS_HOURS,
    MINUTES_PER_DAY,
    BreakRule,
    DemandHour,
    Employee,
    InputData,
    ShiftTemplate,
    day_of_week,
    fmt_minutes,
    is_slot_aligned,
    time_to_minutes,
)

SCHEMA_VERSION = "1.0"

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "Settings": ["SchemaVersion", "WeekStart"],
    "Employees": ["EmployeeID", "EmployeeName", "UnavailableDay1", "UnavailableDay2", "PreferredShiftStart"],
    "Demand": ["Date", "HourStart", "RequiredEmployees"],
    "Shifts": ["ShiftID", "DayOfWeek", "StartTime", "DurationHours", "Cost"],
    "BreakRules": ["DurationHours", "BreakCount", "BreakMinutes", "MinStartGapMinutes",
                   "MinEndGapMinutes", "MinBetweenBreaksMinutes"],
}

# Fixed values for version 1.0 (spec section 6, BreakRules table).
FIXED_BREAK_RULES = {
    4: dict(BreakCount=1, BreakMinutes=30, MinStartGapMinutes=60, MinEndGapMinutes=60, MinBetweenBreaksMinutes=60),
    6: dict(BreakCount=2, BreakMinutes=30, MinStartGapMinutes=60, MinEndGapMinutes=60, MinBetweenBreaksMinutes=60),
    8: dict(BreakCount=2, BreakMinutes=30, MinStartGapMinutes=60, MinEndGapMinutes=60, MinBetweenBreaksMinutes=60),
}

EXCEL_EPOCH = dt.datetime(1899, 12, 30)


@dataclass
class InputIssue:
    sheet: str
    row: int | None  # Excel row number (header is row 1); None for sheet-level problems
    column: str | None
    message: str
    code: str

    def __str__(self) -> str:
        where = self.sheet
        if self.row is not None:
            where += f" row {self.row}"
        if self.column:
            where += f", column {self.column}"
        return f"{where}: {self.message}"


@dataclass
class ImportResult:
    status: str  # "OK" or "INVALID_INPUT"
    data: InputData | None = None
    errors: list[InputIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class _TypeErr(Exception):
    pass


# ---------------------------------------------------------------- cell coercion
def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _as_text(v) -> str:
    if not isinstance(v, str):
        raise _TypeErr(f"expected text, got {type(v).__name__} {v!r}")
    return v.strip()


def _as_int(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _TypeErr(f"expected a whole number, got {type(v).__name__} {v!r}")
    if isinstance(v, float) and not v.is_integer():
        raise _TypeErr(f"expected a whole number, got {v!r}")
    return int(v)


def _as_number(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _TypeErr(f"expected a number, got {type(v).__name__} {v!r}")
    return float(v)


def _as_time_minutes(v) -> int:
    """Excel time -> minutes. Accepts time cells, the 1899-12-30 datetime Excel
    sometimes yields for times, or a raw day fraction."""
    if isinstance(v, dt.datetime):
        if v.date() not in (dt.date(1899, 12, 30), dt.date(1899, 12, 31), dt.date(1900, 1, 1)):
            raise _TypeErr(f"expected a time, got a date {v.date().isoformat()}")
        v = v.time()
    if isinstance(v, dt.time):
        if v.second or v.microsecond:
            raise _TypeErr(f"time {v.isoformat()} has seconds")
        return time_to_minutes(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not 0 <= v < 1:
            raise _TypeErr(f"expected a time, got number {v!r}")
        total = round(v * MINUTES_PER_DAY * 60)
        if total % 60:
            raise _TypeErr(f"time value {v!r} is not a whole minute")
        return total // 60
    raise _TypeErr(f"expected a time, got {type(v).__name__} {v!r}")


def _as_date(v) -> dt.date:
    if isinstance(v, dt.datetime):
        if v.time() != dt.time(0, 0):
            raise _TypeErr(f"expected a date without time, got {v.isoformat()}")
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer() and v > 1:
        return (EXCEL_EPOCH + dt.timedelta(days=int(v))).date()
    raise _TypeErr(f"expected a date, got {type(v).__name__} {v!r}")


# ---------------------------------------------------------------- sheet reading
class _Sheet:
    """Rows of one sheet as dicts keyed by required column name, with Excel row numbers."""

    def __init__(self, ws, name: str, errors: list[InputIssue]):
        self.name = name
        self.rows: list[tuple[int, dict]] = []
        self.ok = True
        header = [c.value for c in ws[1]] if ws.max_row >= 1 else []
        header = [h.strip() if isinstance(h, str) else h for h in header]
        idx = {}
        for col in REQUIRED_COLUMNS[name]:
            if col in header:
                idx[col] = header.index(col)
            else:
                errors.append(InputIssue(name, 1, col, "required column is missing", "MISSING_COLUMN"))
                self.ok = False
        for rng in ws.merged_cells.ranges:
            errors.append(InputIssue(name, rng.min_row, None, f"merged cells {rng.coord} are not allowed", "MERGED_CELLS"))
        for r_i, row in enumerate(ws.iter_rows(min_row=2), start=2):
            for c in row:
                if c.data_type == "f":
                    errors.append(InputIssue(name, r_i, header[c.column - 1] if c.column - 1 < len(header) else None,
                                             "formulas are not allowed in data sheets", "FORMULA"))
            values = {col: (row[i].value if i < len(row) else None) for col, i in idx.items()}
            if all(_is_empty(v) for v in values.values()):
                continue  # blank line
            self.rows.append((r_i, values))


class _Collector:
    def __init__(self):
        self.errors: list[InputIssue] = []

    def add(self, sheet, row, col, msg, code):
        self.errors.append(InputIssue(sheet, row, col, msg, code))

    def get(self, sheet, row_no, values, col, conv, required=True):
        """Convert one cell; record an error and return None if empty-but-required or wrong type."""
        v = values.get(col)
        if _is_empty(v):
            if required:
                self.add(sheet, row_no, col, "required field is empty", "EMPTY_REQUIRED")
            return None
        try:
            return conv(v)
        except _TypeErr as e:
            self.add(sheet, row_no, col, str(e), "WRONG_TYPE")
            return None


# ---------------------------------------------------------------- public API
def read_input(path: str | Path) -> ImportResult:
    path = Path(path)
    col = _Collector()
    try:
        wb = openpyxl.load_workbook(path, data_only=False)
    except Exception as e:  # not a readable xlsx
        return ImportResult("INVALID_INPUT", errors=[InputIssue("(file)", None, None, f"cannot open workbook: {e}", "UNREADABLE")])

    sheets: dict[str, _Sheet] = {}
    for name in REQUIRED_COLUMNS:
        if name not in wb.sheetnames:
            col.add(name, None, None, "required sheet is missing", "MISSING_SHEET")
        else:
            sheets[name] = _Sheet(wb[name], name, col.errors)

    week_start = _read_settings(sheets.get("Settings"), col)
    schema_version = SCHEMA_VERSION
    employees = _read_employees(sheets.get("Employees"), col)
    demand = _read_demand(sheets.get("Demand"), col, week_start)
    templates = _read_shifts(sheets.get("Shifts"), col, week_start, demand)
    break_rules = _read_break_rules(sheets.get("BreakRules"), col)

    if col.errors:
        return ImportResult("INVALID_INPUT", errors=col.errors)
    data = InputData(schema_version, week_start, employees, demand, templates, break_rules, source_file=path.name)
    return ImportResult("OK", data=data)


# ---------------------------------------------------------------- per-sheet readers
def _read_settings(sh: _Sheet | None, c: _Collector) -> dt.date | None:
    if sh is None or not sh.ok:
        return None
    if len(sh.rows) != 1:
        c.add("Settings", 2, None, f"must contain exactly one data row, found {len(sh.rows)}", "SETTINGS_ROWS")
        if not sh.rows:
            return None
    r, v = sh.rows[0]
    ver = c.get("Settings", r, v, "SchemaVersion", _as_text)
    if ver is not None and ver != SCHEMA_VERSION:
        c.add("Settings", r, "SchemaVersion", f"must be '{SCHEMA_VERSION}', got '{ver}'", "SCHEMA_VERSION")
    ws = c.get("Settings", r, v, "WeekStart", _as_date)
    if ws is not None and day_of_week(ws) != 1:
        c.add("Settings", r, "WeekStart", f"{ws.isoformat()} is not a Sunday", "WEEKSTART_NOT_SUNDAY")
        return None
    return ws


def _read_employees(sh: _Sheet | None, c: _Collector) -> dict[str, Employee]:
    out: dict[str, Employee] = {}
    if sh is None or not sh.ok:
        return out
    seen: dict[str, int] = {}
    for r, v in sh.rows:
        eid = c.get("Employees", r, v, "EmployeeID", _as_text)
        name = c.get("Employees", r, v, "EmployeeName", _as_text)
        days = []
        for colname in ("UnavailableDay1", "UnavailableDay2"):
            d = c.get("Employees", r, v, colname, _as_int, required=False)
            if d is not None and not 1 <= d <= 6:
                c.add("Employees", r, colname, f"day must be between 1 (Sunday) and 6 (Friday), got {d}", "DAY_OUT_OF_RANGE")
                d = None
            days.append(d)
        if days[0] is not None and days[0] == days[1]:
            c.add("Employees", r, "UnavailableDay2", f"same day as UnavailableDay1 ({days[0]})", "DUPLICATE_UNAVAILABLE_DAY")
        pref = c.get("Employees", r, v, "PreferredShiftStart", _as_time_minutes, required=False)
        if pref is not None and not is_slot_aligned(pref):
            c.add("Employees", r, "PreferredShiftStart", f"{fmt_minutes(pref)} is not on a 30-minute boundary", "TIME_NOT_ALIGNED")
        if eid is None:
            continue
        if eid in seen:
            c.add("Employees", r, "EmployeeID", f"duplicate ID '{eid}' (also in row {seen[eid]})", "DUPLICATE_ID")
            continue
        seen[eid] = r
        out[eid] = Employee(eid, name or "", frozenset(d for d in days if d is not None), pref)
    return out


def _check_demand_dates_without_week(sh: _Sheet, c: _Collector) -> None:
    """Date checks that hold whatever WeekStart says (SCRUM-78, decided 14.11.2026).

    When WeekStart is missing or invalid, the dates cannot be compared with it, but two
    rules still apply and are reported in the same pass: every Demand date is a Sunday to
    Friday, and all Demand dates fall in one Sunday-to-Friday week. The week most rows
    belong to is taken as the reference (the earliest on a tie), so one stray row is the
    one reported, not all the others.
    """
    dates = []
    for r, v in sh.rows:
        if v.get("Date") is None:
            continue
        try:
            dates.append((r, _as_date(v.get("Date"))))
        except _TypeErr:
            continue  # the type error is reported by the main pass
    for r, d in dates:
        if day_of_week(d) == 7:
            c.add("Demand", r, "Date", f"{d.isoformat()} is a Saturday; planning days are Sunday to Friday",
                  "DATE_NOT_SUNDAY_TO_FRIDAY")

    def sunday(d):  # the Sunday that starts d's week (d is Sunday..Friday here)
        return d - dt.timedelta(days=day_of_week(d) - 1)

    weeks = Counter(sunday(d) for _, d in dates if day_of_week(d) != 7)
    if len(weeks) <= 1:
        return
    ref = min(weeks, key=lambda w: (-weeks[w], w))
    for r, d in dates:
        if day_of_week(d) != 7 and sunday(d) != ref:
            c.add("Demand", r, "Date", f"{d.isoformat()} is not in the same Sunday-Friday week as the other "
                                       f"Demand dates (week starting {ref.isoformat()})", "DATES_NOT_ONE_WEEK")


def _read_demand(sh: _Sheet | None, c: _Collector, week_start) -> dict:
    out: dict = {}
    if sh is None or not sh.ok:
        return out
    week = {week_start + dt.timedelta(days=i) for i in range(6)} if week_start else None
    seen: dict = {}
    if week is None:
        _check_demand_dates_without_week(sh, c)
    for r, v in sh.rows:
        d = c.get("Demand", r, v, "Date", _as_date)
        h = c.get("Demand", r, v, "HourStart", _as_time_minutes)
        req = c.get("Demand", r, v, "RequiredEmployees", _as_int)
        if d is not None and week is not None and d not in week:
            c.add("Demand", r, "Date", f"{d.isoformat()} is not Sunday-Friday of the week starting {week_start.isoformat()}",
                  "DATE_OUT_OF_WEEK")
            d = None
        if h is not None and h % 60:
            c.add("Demand", r, "HourStart", f"{fmt_minutes(h)} is not a whole hour", "HOUR_NOT_WHOLE")
            h = None
        if req is not None and req < 0:
            c.add("Demand", r, "RequiredEmployees", f"must be zero or more, got {req}", "NEGATIVE_DEMAND")
            req = None
        if d is None or h is None or req is None:
            continue
        key = (d, h)
        if key in seen:
            c.add("Demand", r, "HourStart", f"duplicate key {d.isoformat()} {fmt_minutes(h)} (also in row {seen[key]})",
                  "DUPLICATE_KEY")
            continue
        seen[key] = r
        out[key] = DemandHour(d, h, req)
    return out


def _read_shifts(sh: _Sheet | None, c: _Collector, week_start, demand) -> dict[str, ShiftTemplate]:
    out: dict[str, ShiftTemplate] = {}
    if sh is None or not sh.ok:
        return out
    seen: dict[str, int] = {}
    for r, v in sh.rows:
        sid = c.get("Shifts", r, v, "ShiftID", _as_text)
        day = c.get("Shifts", r, v, "DayOfWeek", _as_int)
        start = c.get("Shifts", r, v, "StartTime", _as_time_minutes)
        dur = c.get("Shifts", r, v, "DurationHours", _as_int)
        cost = c.get("Shifts", r, v, "Cost", _as_number)
        bad = False
        if day is not None and not 1 <= day <= 6:
            c.add("Shifts", r, "DayOfWeek", f"day must be between 1 and 6, got {day}", "DAY_OUT_OF_RANGE"); bad = True
        if start is not None and not is_slot_aligned(start):
            c.add("Shifts", r, "StartTime", f"{fmt_minutes(start)} is not on a 30-minute boundary", "TIME_NOT_ALIGNED"); bad = True
        if dur is not None and dur not in ALLOWED_DURATIONS_HOURS:
            c.add("Shifts", r, "DurationHours", f"must be 4, 6 or 8, got {dur}", "BAD_DURATION"); bad = True
        if cost is not None:
            if cost < 0:
                c.add("Shifts", r, "Cost", f"must not be negative, got {cost}", "NEGATIVE_COST"); bad = True
            elif abs(round(cost, 2) - cost) > 1e-9:
                c.add("Shifts", r, "Cost", f"at most two decimal places, got {cost}", "COST_PRECISION"); bad = True
        if None in (sid, day, start, dur, cost) or bad:
            continue
        end = start + dur * 60
        if end > MINUTES_PER_DAY:
            c.add("Shifts", r, "StartTime", f"{fmt_minutes(start)} + {dur} h crosses midnight", "CROSSES_MIDNIGHT")
            continue
        if week_start is not None and demand:  # skip when Demand itself failed, to avoid noise
            date = week_start + dt.timedelta(days=day - 1)
            allowed = {h for (dd, h) in demand if dd == date}
            missing = sorted({m - m % 60 for m in range(start, end, 30)} - allowed)
            if missing:
                c.add("Shifts", r, "StartTime",
                      f"template {fmt_minutes(start)}-{fmt_minutes(end)} on {date.isoformat()} is outside the active hours "
                      f"(not in Demand: {', '.join(fmt_minutes(m) for m in missing)})", "OUTSIDE_ACTIVE_HOURS")
                continue
        if sid in seen:
            c.add("Shifts", r, "ShiftID", f"duplicate ID '{sid}' (also in row {seen[sid]})", "DUPLICATE_ID")
            continue
        seen[sid] = r
        out[sid] = ShiftTemplate(sid, day, start, dur, round(cost, 2))
    return out


def _read_break_rules(sh: _Sheet | None, c: _Collector) -> dict[int, BreakRule]:
    out: dict[int, BreakRule] = {}
    if sh is None or not sh.ok:
        return out
    seen: dict[int, int] = {}
    for r, v in sh.rows:
        vals = {k: c.get("BreakRules", r, v, k, _as_int) for k in REQUIRED_COLUMNS["BreakRules"]}
        dur = vals["DurationHours"]
        if dur is None:
            continue
        if dur not in FIXED_BREAK_RULES:
            c.add("BreakRules", r, "DurationHours", f"must be 4, 6 or 8, got {dur}", "BAD_DURATION")
            continue
        if dur in seen:
            c.add("BreakRules", r, "DurationHours", f"duplicate row for {dur} h (also in row {seen[dur]})", "DUPLICATE_KEY")
            continue
        seen[dur] = r
        ok = True
        for k, expected in FIXED_BREAK_RULES[dur].items():
            if vals[k] is not None and vals[k] != expected:
                c.add("BreakRules", r, k, f"must be {expected} for {dur} h shifts in schema 1.0, got {vals[k]}", "BREAK_RULE_VALUE")
                ok = False
        if ok and None not in vals.values():
            out[dur] = BreakRule(dur, vals["BreakCount"], vals["BreakMinutes"], vals["MinStartGapMinutes"],
                                 vals["MinEndGapMinutes"], vals["MinBetweenBreaksMinutes"])
    for dur in FIXED_BREAK_RULES:
        if dur not in seen:
            c.add("BreakRules", None, "DurationHours", f"missing the required row for {dur} h shifts", "MISSING_BREAK_RULE")
    return out
