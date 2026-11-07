"""SCRUM-19 (structural) and SCRUM-20 (business-rule) input errors.

Each test edits a copy of Input_Small_10 and checks that the exact sheet, row,
column and error code are reported (spec section 6, "input checks").
"""
import datetime as dt

import pytest

from shiftopt.ingest import read_input


def _codes(path):
    r = read_input(path)
    assert r.status == "INPUT_INVALID"
    return {(e.sheet, e.row, e.column, e.code) for e in r.errors}, r


# ---------------------------------------------------------------- SCRUM-19 structural
def test_missing_sheet(mutate):
    p = mutate(lambda wb: wb.remove(wb["BreakRules"]))
    codes, _ = _codes(p)
    assert ("BreakRules", None, None, "MISSING_SHEET") in codes


def test_missing_column(mutate):
    p = mutate(lambda wb: wb["Demand"].delete_cols(3))
    codes, _ = _codes(p)
    assert ("Demand", 1, "RequiredEmployees", "MISSING_COLUMN") in codes


def test_empty_required_field(mutate):
    def f(wb):
        wb["Employees"]["B4"] = None  # EmployeeName of E003
    codes, _ = _codes(mutate(f))
    assert ("Employees", 4, "EmployeeName", "EMPTY_REQUIRED") in codes


@pytest.mark.parametrize("sheet,cell,value,column", [
    ("Demand", "B2", "08:00", "HourStart"),  # time stored as text
    ("Demand", "C2", "3", "RequiredEmployees"),  # number stored as text
    ("Employees", "A2", 1, "EmployeeID"),  # number where text is required
    ("Shifts", "D2", 4.5, "DurationHours"),  # not a whole number
])
def test_wrong_type(mutate, sheet, cell, value, column):
    def f(wb):
        wb[sheet][cell] = value
    codes, _ = _codes(mutate(f))
    assert (sheet, 2, column, "WRONG_TYPE") in codes


def test_formula_rejected(mutate):
    def f(wb):
        wb["Demand"]["C2"] = "=1+2"
    codes, _ = _codes(mutate(f))
    assert ("Demand", 2, "RequiredEmployees", "FORMULA") in codes


def test_merged_cells_rejected(mutate):
    codes, _ = _codes(mutate(lambda wb: wb["Employees"].merge_cells("B2:B3")))
    assert any(c[3] == "MERGED_CELLS" for c in codes)


def test_all_errors_reported_in_one_pass(mutate):
    def f(wb):
        wb["Employees"]["B4"] = None
        wb["Demand"]["C5"] = -1
        wb["Shifts"]["D3"] = 5
    _, r = _codes(mutate(f))
    assert len(r.errors) >= 3


# ---------------------------------------------------------------- SCRUM-20 business rules
def test_duplicate_employee_id(mutate):
    def f(wb):
        wb["Employees"]["A5"] = "E003"
    codes, r = _codes(mutate(f))
    assert ("Employees", 5, "EmployeeID", "DUPLICATE_ID") in codes
    assert "row 4" in next(e.message for e in r.errors if e.code == "DUPLICATE_ID")


def test_duplicate_shift_id(mutate):
    def f(wb):
        wb["Shifts"]["A3"] = wb["Shifts"]["A2"].value
    codes, _ = _codes(mutate(f))
    assert ("Shifts", 3, "ShiftID", "DUPLICATE_ID") in codes


def test_duplicate_demand_key(mutate):
    def f(wb):
        ws = wb["Demand"]
        ws["B3"] = ws["B2"].value
    codes, _ = _codes(mutate(f))
    assert ("Demand", 3, "HourStart", "DUPLICATE_KEY") in codes


def test_unavailable_day_out_of_range(mutate):
    def f(wb):
        wb["Employees"]["C2"] = 7  # Saturday
    codes, _ = _codes(mutate(f))
    assert ("Employees", 2, "UnavailableDay1", "DAY_OUT_OF_RANGE") in codes


def test_same_unavailable_day_twice(mutate):
    def f(wb):
        wb["Employees"]["D2"] = wb["Employees"]["C2"].value
    codes, _ = _codes(mutate(f))
    assert ("Employees", 2, "UnavailableDay2", "DUPLICATE_UNAVAILABLE_DAY") in codes


def test_shift_start_not_aligned(mutate):
    def f(wb):
        wb["Shifts"]["C2"] = dt.time(8, 15)
    codes, _ = _codes(mutate(f))
    assert ("Shifts", 2, "StartTime", "TIME_NOT_ALIGNED") in codes


def test_hour_start_not_whole_hour(mutate):
    def f(wb):
        wb["Demand"]["B2"] = dt.time(8, 30)
    codes, _ = _codes(mutate(f))
    assert ("Demand", 2, "HourStart", "HOUR_NOT_WHOLE") in codes


def test_negative_demand(mutate):
    def f(wb):
        wb["Demand"]["C2"] = -1
    codes, _ = _codes(mutate(f))
    assert ("Demand", 2, "RequiredEmployees", "NEGATIVE_DEMAND") in codes


def test_demand_date_outside_week(mutate):
    def f(wb):
        wb["Demand"]["A2"] = dt.datetime(2026, 11, 7)  # Saturday
    codes, _ = _codes(mutate(f))
    assert ("Demand", 2, "Date", "DATE_OUT_OF_WEEK") in codes


def test_week_start_must_be_sunday(mutate):
    def f(wb):
        wb["Settings"]["B2"] = dt.datetime(2026, 11, 2)
    codes, _ = _codes(mutate(f))
    assert ("Settings", 2, "WeekStart", "WEEKSTART_NOT_SUNDAY") in codes


def test_schema_version(mutate):
    def f(wb):
        wb["Settings"]["A2"] = "2.0"
    codes, _ = _codes(mutate(f))
    assert ("Settings", 2, "SchemaVersion", "SCHEMA_VERSION") in codes


def test_settings_single_row(mutate):
    def f(wb):
        wb["Settings"].append(["1.0", dt.datetime(2026, 11, 8)])
    codes, _ = _codes(mutate(f))
    assert any(c[3] == "SETTINGS_ROWS" for c in codes)


def test_bad_duration(mutate):
    def f(wb):
        wb["Shifts"]["D2"] = 5
    codes, _ = _codes(mutate(f))
    assert ("Shifts", 2, "DurationHours", "BAD_DURATION") in codes


@pytest.mark.parametrize("value,code", [(-10, "NEGATIVE_COST"), (200.125, "COST_PRECISION")])
def test_cost_rules(mutate, value, code):
    def f(wb):
        wb["Shifts"]["E2"] = value
    codes, _ = _codes(mutate(f))
    assert ("Shifts", 2, "Cost", code) in codes


def test_template_outside_active_hours(mutate):
    # Sunday is active 08:00-18:00; a 10:00 8 h template would end at 18:00 -> OK,
    # but a 12:00 8 h template ends at 20:00 and must be reported, not dropped silently.
    def f(wb):
        ws = wb["Shifts"]
        ws.append(["D1_12_8", 1, dt.time(12, 0), 8, 400])
    p = mutate(f)
    codes, r = _codes(p)
    row = r.errors[0].row
    assert ("Shifts", row, "StartTime", "OUTSIDE_ACTIVE_HOURS") in codes


def test_template_crossing_midnight(mutate):
    def f(wb):
        wb["Shifts"].append(["D1_20_8", 1, dt.time(20, 0), 8, 400])
    codes, _ = _codes(mutate(f))
    assert any(c[3] == "CROSSES_MIDNIGHT" for c in codes)


def test_break_rules_fixed_values(mutate):
    def f(wb):
        wb["BreakRules"]["C2"] = 45  # BreakMinutes for 4 h
    codes, _ = _codes(mutate(f))
    assert ("BreakRules", 2, "BreakMinutes", "BREAK_RULE_VALUE") in codes


def test_break_rules_missing_row(mutate):
    codes, _ = _codes(mutate(lambda wb: wb["BreakRules"].delete_rows(4)))
    assert ("BreakRules", None, "DurationHours", "MISSING_BREAK_RULE") in codes
