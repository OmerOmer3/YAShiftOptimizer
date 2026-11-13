"""SCRUM-27: every rule the independent checker knows has a deliberately wrong schedule
that it must reject with that rule (spec section 10: "a result that is wrong on purpose").

Each case starts from the course's valid sample schedule (Output_Small_10), breaks it in
one way, and expects the named rule among the violations. A final test reads the rule
codes out of rules.py, so a new rule without a case here fails the build.
"""
import copy
import datetime as dt
import re
from pathlib import Path

import pytest

from shiftopt.domain import Assignment, Break
from shiftopt.ingest import read_input
from shiftopt.ingest.schedule_reader import read_schedule
from shiftopt.validator import validate

RULES_FILE = Path(__file__).resolve().parent.parent / "shiftopt" / "validator" / "rules.py"


@pytest.fixture(scope="module")
def base():
    samples = Path(__file__).resolve().parent.parent / "samples"
    return read_input(samples / "Input_Small_10.xlsx").data, read_schedule(samples / "Output_Small_10.xlsx")


# ---------------------------------------------------------------- helpers
def first(s, n_breaks=None, eid=None):
    return next(a for a in s.assignments
                if (n_breaks is None or len(a.breaks) == n_breaks) and (eid is None or a.employee_id == eid))


def legal_breaks(d, t):
    r = d.break_rules[t.duration_hours]
    starts = [t.start + r.min_start_gap + i * (r.break_minutes + r.min_between_breaks) for i in range(r.break_count)]
    return tuple(Break(s, s + r.break_minutes) for s in starts)


def shift_on(d, eid, day):
    t = max(d.templates_on(day), key=lambda t: t.duration_hours)
    return Assignment(d.date_for_day(day), eid, t.shift_id, t.start, t.end, legal_breaks(d, t),
                      float(t.duration_hours), t.cost)


# ---------------------------------------------------------------- one mutation per rule
def m_unknown_employee(d, s):
    first(s).employee_id = "E999"


def m_date_out_of_week(d, s):
    first(s).date = dt.date(2026, 11, 7)  # Saturday


def m_unavailable_day(d, s):
    e = next(e for e in d.employees.values() if e.unavailable_days)
    s.assignments.append(shift_on(d, e.employee_id, min(e.unavailable_days)))


def m_unknown_template(d, s):
    first(s).shift_id = "NO_SUCH_TEMPLATE"


def m_template_wrong_day(d, s):
    a = first(s)
    a.date = a.date + dt.timedelta(days=1) if a.date.isoweekday() != 5 else a.date - dt.timedelta(days=1)


def m_template_mismatch(d, s):
    first(s).start += 30


def m_paid_hours_mismatch(d, s):
    first(s).paid_hours += 1


def m_cost_mismatch(d, s):
    first(s).cost += 10


def m_outside_active_hours(d, s):
    first(s).end = 19 * 60  # 18:00 is not in Demand


def m_no_break_rule(d, s):
    d.break_rules.pop(int(first(s).paid_hours))


def m_break_count(d, s):
    a = first(s, n_breaks=2)
    a.breaks = a.breaks[:1]


def m_break_length(d, s):
    a = first(s)
    b = a.breaks[0]
    a.breaks = (Break(b.start, b.start + 60),) + a.breaks[1:]


def m_break_not_aligned(d, s):
    a = first(s)
    b = a.breaks[0]
    a.breaks = (Break(b.start + 15, b.end + 15),) + a.breaks[1:]


def m_break_outside_shift(d, s):
    a = first(s)
    a.breaks = (Break(a.start - 30, a.start),) + a.breaks[1:]


def m_break_too_early(d, s):
    a = first(s)
    a.breaks = (Break(a.start + 30, a.start + 60),) + a.breaks[1:]


def m_break_too_late(d, s):
    a = first(s, n_breaks=1)
    a.breaks = (Break(a.end - 60, a.end - 30),)


def m_breaks_too_close(d, s):
    a = first(s, n_breaks=2)
    b1 = a.breaks[0]
    a.breaks = (b1, Break(b1.end + 30, b1.end + 60))


def m_more_than_one_shift_per_day(d, s):
    s.assignments.append(copy.deepcopy(first(s)))


def m_too_many_shifts(d, s):
    """E001 gets a shift on all six days (blocked days included; other rules may fire too)."""
    eid = "E001"
    s.assignments[:] = [a for a in s.assignments if a.employee_id != eid]
    for day in range(1, 7):
        s.assignments.append(shift_on(d, eid, day))


def m_too_few_shifts(d, s):
    s.assignments.remove(first(s))


def m_over_40_hours(d, s):
    m_too_many_shifts(d, s)  # six shifts of up to 8 h: over 40 paid hours


MUTATIONS = {
    "UNKNOWN_EMPLOYEE": m_unknown_employee,
    "DATE_OUT_OF_WEEK": m_date_out_of_week,
    "UNAVAILABLE_DAY": m_unavailable_day,
    "UNKNOWN_TEMPLATE": m_unknown_template,
    "TEMPLATE_WRONG_DAY": m_template_wrong_day,
    "TEMPLATE_MISMATCH": m_template_mismatch,
    "PAID_HOURS_MISMATCH": m_paid_hours_mismatch,
    "COST_MISMATCH": m_cost_mismatch,
    "OUTSIDE_ACTIVE_HOURS": m_outside_active_hours,
    "NO_BREAK_RULE": m_no_break_rule,
    "BREAK_COUNT": m_break_count,
    "BREAK_LENGTH": m_break_length,
    "BREAK_NOT_ALIGNED": m_break_not_aligned,
    "BREAK_OUTSIDE_SHIFT": m_break_outside_shift,
    "BREAK_TOO_EARLY": m_break_too_early,
    "BREAK_TOO_LATE": m_break_too_late,
    "BREAKS_TOO_CLOSE": m_breaks_too_close,
    "MORE_THAN_ONE_SHIFT_PER_DAY": m_more_than_one_shift_per_day,
    "TOO_MANY_SHIFTS": m_too_many_shifts,
    "TOO_FEW_SHIFTS": m_too_few_shifts,
    "OVER_40_HOURS": m_over_40_hours,
}


def test_sample_is_valid_before_mutation(base):
    d, s = base
    assert validate(d, s).passed


@pytest.mark.parametrize("rule", sorted(MUTATIONS))
def test_wrong_schedule_is_rejected_with_its_rule(base, rule):
    d, s = copy.deepcopy(base[0]), copy.deepcopy(base[1])
    MUTATIONS[rule](d, s)
    rep = validate(d, s)
    assert not rep.passed
    assert rule in rep.rules_broken(), sorted(rep.rules_broken())


def test_every_checker_rule_has_a_mutation():
    """Fails when a rule is added to the checker without a deliberately wrong case here."""
    codes = set(re.findall(r'add\(\s*"([A-Z0-9_]+)"', RULES_FILE.read_text()))
    assert codes, "no rule codes found in rules.py"
    assert codes == set(MUTATIONS), {"missing cases": codes - set(MUTATIONS), "unknown": set(MUTATIONS) - codes}
