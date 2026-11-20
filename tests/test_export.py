"""SCRUM-44: export a schedule version to Excel in the output schema (spec sections 7-9).

The course's Output_Small_10.xlsx is the reference for sheet names, column names and
column order. Two kinds of checks:
  * layout: our headers and RunSummary keys equal the sample's, in the same order;
  * values: writing the sample's own schedule reproduces the sample's Coverage and
    EmployeeSummary sheets cell by cell, and an exported run read back passes the
    independent checker with the same totals as the run.
"""
from __future__ import annotations

import openpyxl
import pytest

from shiftopt.cli import main
from shiftopt.db import Store
from shiftopt.export import DATA_SHEETS, ExportError, export_run, write_workbook
from shiftopt.ingest import import_workbook, read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.service import run_initial
from shiftopt.validator import validate


def _rows(path, sheet):
    ws = openpyxl.load_workbook(path)[sheet]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _summary(path) -> dict:
    return {k: v for k, v in _rows(path, "RunSummary")[1:]}


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    """One solved Small_10 run and one infeasible run in the same database."""
    from tests.conftest import SAMPLES
    tmp = tmp_path_factory.mktemp("export")
    store = Store(str(tmp / "db.sqlite"))
    assert import_workbook(store, SAMPLES / "Input_Small_10.xlsx").status == "OK"
    ok = run_initial(store, 20, workers=2).run
    assert ok.schedule_version is not None
    assert import_workbook(store, SAMPLES / "Input_Infeasible.xlsx").status == "OK"
    bad = run_initial(store, 10, workers=2).run
    assert bad.status == "INFEASIBLE"
    return store, ok, bad, tmp


@pytest.fixture(scope="module")
def exported(solved):
    store, ok, _, tmp = solved
    return export_run(store, ok.run_id, tmp / "out_ok.xlsx")


# ------------------------------------------------------------------ layout
def test_sheets_and_headers_match_the_sample(exported, small_output):
    ours = openpyxl.load_workbook(exported)
    theirs = openpyxl.load_workbook(small_output)
    assert ours.sheetnames[:4] == theirs.sheetnames[:4] == list(DATA_SHEETS)
    for name in ("Schedule", "Coverage", "EmployeeSummary"):
        assert _rows(exported, name)[0] == _rows(small_output, name)[0], name
    # RunSummary: same two columns and the same Metric keys in the same order.
    assert [r[0] for r in _rows(exported, "RunSummary")] == [r[0] for r in _rows(small_output, "RunSummary")]


def test_values_only_no_formulas(exported):
    wb = openpyxl.load_workbook(exported)  # not data_only: a formula would show as type "f"
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", f"{ws.title}!{c.coordinate}"


def test_display_formats(exported):
    wb = openpyxl.load_workbook(exported)
    s = wb["Schedule"]
    assert s["A2"].number_format == "yyyy-mm-dd"
    assert all(s.cell(2, c).number_format == "hh:mm" for c in range(4, 8))
    cov = wb["Coverage"]
    assert cov["A2"].number_format == "yyyy-mm-dd" and cov["B2"].number_format == "hh:mm"
    rs = {r[0].value: r[1] for r in wb["RunSummary"].iter_rows(min_row=2)}
    assert rs["WeekStart"].number_format == "yyyy-mm-dd"


def test_two_coverage_rows_per_demand_row(exported, small_input):
    data = read_input(small_input).data
    cov = _rows(exported, "Coverage")[1:]
    assert len(cov) == 2 * len(data.demand)
    zero_demand = [r for r in cov if r[3] == 0]
    assert zero_demand, "zero-demand hours must still be listed"


# ------------------------------------------------------------------ values
def test_sample_schedule_reproduces_sample_sheets(tmp_path, small_input, small_output):
    """Writing the course's own schedule must give the course's Coverage and EmployeeSummary."""
    data = read_input(small_input).data
    schedule = read_schedule(small_output)
    out = write_workbook(tmp_path / "again.xlsx", data, schedule, summary={}, group_size=3)
    for name in ("Schedule", "Coverage", "EmployeeSummary"):
        ours, theirs = _rows(out, name), _rows(small_output, name)
        assert len(ours) == len(theirs), name
        for i, (a, b) in enumerate(zip(ours, theirs)):
            assert a == pytest.approx(b) if name == "EmployeeSummary" else a == b, f"{name} row {i + 1}: {a} != {b}"


def test_round_trip_passes_the_checker_with_the_same_numbers(solved, exported, small_input):
    _, ok, _, _ = solved
    data = read_input(small_input).data
    report = validate(data, read_schedule(exported), group_size=ok.group_size)
    assert report.passed, [str(v) for v in report.violations]
    rs = _summary(exported)
    assert report.total_cost == rs["TotalCost"] == ok.total_cost
    assert report.total_shortage_slots == rs["TotalShortageSlots"] == ok.total_shortage_slots
    assert sum(r[5] for r in _rows(exported, "Coverage")[1:]) == rs["TotalShortageSlots"]


def test_run_summary_values(solved, exported):
    _, ok, _, _ = solved
    rs = _summary(exported)
    assert rs["SchemaVersion"] == "1.0"
    assert rs["RunID"] == ok.run_id and rs["ScheduleVersion"] == ok.schedule_version
    assert rs["InputFile"] == "Input_Small_10.xlsx"
    assert rs["RunType"] == "INITIAL" and rs["GroupSize"] == 2
    assert rs["SolutionStatus"] == ok.status and rs["ApprovalStatus"] == "DRAFT"
    assert rs["BaseScheduleVersion"] is None          # initial run (spec section 8)
    assert rs["ChangedAssignments"] == 0
    assert rs["MinPreferenceRate"] is None            # pair team: not relevant
    assert rs["StabilityOptimalityProven"] is None and rs["PreferenceOptimalityProven"] is None
    assert rs["ValidationPassed"] is True
    assert rs["TimeBudgetSeconds"] == 20 and rs["OptimizationSeconds"] > 0


def test_pair_leaves_preference_metrics_empty(exported, small_input):
    data = read_input(small_input).data
    rows = _rows(exported, "EmployeeSummary")[1:]
    assert [r[0] for r in rows] == list(data.employees)  # one row per input employee, input order
    with_pref = [r for r in rows if r[4] is not None]
    assert with_pref, "PreferredShiftStart is copied from the input even for a pair"
    assert all(r[5] is None and r[6] is None for r in rows)


def test_no_solution_exports_headers_only(solved, small_output):
    store, _, bad, tmp = solved
    out = export_run(store, bad.run_id, tmp / "out_bad.xlsx")
    for name in ("Schedule", "Coverage", "EmployeeSummary"):
        rows = _rows(out, name)
        assert rows == [_rows(small_output, name)[0]], name
    rs = _summary(out)
    assert rs["SolutionStatus"] == "INFEASIBLE" and rs["TerminationReason"]
    for k in ("TotalCost", "TotalShortageSlots", "FirstFeasibleSeconds", "ChangedAssignments",
              "ScheduleVersion", "ApprovalStatus"):
        assert rs[k] is None, k                        # empty, not zero (spec section 9)


def test_export_by_schedule_version(solved):
    store, ok, _, tmp = solved
    out = export_run(store, ok.schedule_version, tmp / "by_version.xlsx")
    assert _summary(out)["RunID"] == ok.run_id


def test_unknown_reference(solved):
    store, _, _, tmp = solved
    with pytest.raises(ExportError):
        export_run(store, "RUN-0999", tmp / "x.xlsx")
    with pytest.raises(ExportError):
        export_run(store, "hello", tmp / "x.xlsx")


# ------------------------------------------------------------------ command line
def test_cli_export(solved, capsys):
    store, ok, _, tmp = solved
    db = str(store.engine.url).removeprefix("sqlite:///")
    out = tmp / "cli.xlsx"
    assert main(["--db", db, "export", ok.run_id, str(out)]) == 0
    assert out.exists() and str(out) in capsys.readouterr().out
    assert main(["--db", db, "export", "RUN-0999", str(tmp / "no.xlsx")]) == 2
    assert "not found" in capsys.readouterr().out
