"""SCRUM-26: metrics recomputed from the schedule reproduce the course sample output
cell by cell (spec section 10: 40 shifts, 240 h, cost 11,540, zero shortage, min rate 25%)."""
import datetime as dt

import openpyxl
import pytest

from shiftopt.domain import time_to_minutes
from shiftopt.ingest import read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.validator import validate


@pytest.fixture
def report(small_input, small_output):
    # The sample was built for a triple (GroupSize 3), so preference metrics are compared with group_size=3.
    return validate(read_input(small_input).data, read_schedule(small_output), group_size=3)


def _rows(path, sheet):
    ws = openpyxl.load_workbook(path, data_only=True)[sheet]
    hdr = [c.value for c in ws[1]]
    return [dict(zip(hdr, r)) for r in ws.iter_rows(min_row=2, values_only=True) if any(v is not None for v in r)]


def test_sample_schedule_passes(report):
    assert report.passed, [str(v) for v in report.violations]


def test_headline_numbers_match_spec(report):
    m = report.metrics
    assert m.shift_count == 40
    assert m.total_paid_hours == 240
    assert m.total_cost == 11540
    assert m.total_shortage_slots == 0
    assert m.min_preference_rate == pytest.approx(0.25)


def test_coverage_matches_sample_cell_by_cell(report, small_output):
    expected = _rows(small_output, "Coverage")
    got = report.metrics.coverage
    assert len(got) == len(expected) == 116
    for e, g in zip(expected, got):
        assert g.date == e["Date"].date()
        assert g.slot_start == time_to_minutes(e["SlotStart"])
        assert g.slot_end == time_to_minutes(e["SlotEnd"])
        assert (g.required, g.assigned, g.shortage, g.surplus) == (
            e["RequiredEmployees"], e["AssignedEmployees"], e["Shortage"], e["Surplus"]), e


def test_employee_summary_matches_sample(report, small_output):
    expected = {r["EmployeeID"]: r for r in _rows(small_output, "EmployeeSummary")}
    for g in report.metrics.employees:
        e = expected[g.employee_id]
        assert (g.shift_count, g.paid_hours, g.total_cost) == (e["ShiftCount"], e["PaidHours"], e["TotalCost"])
        assert g.min_shifts_exempt == e["MinShiftsExempt"]
        if e["PreferenceRate"] is None:
            assert g.preference_rate is None
        else:
            assert g.preference_rate == pytest.approx(e["PreferenceRate"])
            assert g.preferred_count == e["PreferredShiftCount"]


def test_run_summary_totals_match(report, small_output):
    rs = {r["Metric"]: r["Value"] for r in _rows(small_output, "RunSummary")}
    assert float(rs["TotalCost"]) == report.metrics.total_cost
    assert int(rs["TotalShortageSlots"]) == report.metrics.total_shortage_slots


def test_pair_leaves_preference_metrics_empty(small_input, small_output):
    """Spec section 8: preference columns keep their structure for a pair but stay empty."""
    rep = validate(read_input(small_input).data, read_schedule(small_output), group_size=2)
    assert all(r.preference_rate is None and r.preferred_count is None for r in rep.metrics.employees)
    assert rep.metrics.min_preference_rate is None
    assert rep.metrics.employees[1].preferred_start == 600  # still echoed from input
