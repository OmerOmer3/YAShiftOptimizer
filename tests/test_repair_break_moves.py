"""SCRUM-128: re-optimization moves as few breaks as possible (spec section 3).

Spec section 3 allows moving the breaks of future unlocked shifts and does not count a
break-only move as a change, but every move is listed in the comparison the manager reads.
A fourth stage, after shortage, changes and cost (the spec order, section 3 step 3),
minimizes the moved breaks with all three bounded:

  * shortage, changes and cost are the same as without the stage;
  * the number of moved breaks is never larger, and is reported in the repair result;
  * a break whose position makes no difference to coverage stays where it was.
"""
from __future__ import annotations

import datetime as dt

import pytest

from shiftopt import clock
from shiftopt.db import Store
from shiftopt.domain import Assignment, Break, Schedule
from shiftopt.engine.repair import reoptimize
from shiftopt.ingest import import_workbook
from shiftopt.service import approve_draft, run_initial
from shiftopt.service.absence import repair_start, report_absence
from shiftopt.service.compare import diff_schedules
from shiftopt.service.repair import run_repair
from shiftopt.validator import validate
from tests.builders import WEEK, flat_demand, h, make_input
from tests.test_repair_reoptimize import approved

MONDAY_7AM = dt.datetime(2026, 11, 2, 7, 0)
WED = WEEK + dt.timedelta(days=3)


@pytest.fixture(autouse=True)
def fixed_now():
    clock.set_now(MONDAY_7AM)
    yield
    clock.set_now(None)


def moved_break_count(before: Schedule, after: Schedule) -> int:
    """Original breaks that are no longer in the same shift (same template kept)."""
    new = {(a.employee_id, a.date): a for a in after.assignments}
    n = 0
    for a in before.assignments:
        b = new.get((a.employee_id, a.date))
        if b is not None and b.shift_id == a.shift_id:
            n += sum(1 for br in a.breaks if br not in b.breaks)
    return n


def engine_inputs(store):
    """The arguments run_repair passes to reoptimize."""
    start = repair_start(store)
    data = store.load_input(start.input_version)
    started = {t.shift_id for t in data.templates.values()
               if clock.has_started(data.date_for_day(t.day_of_week), t.start, MONDAY_7AM)}
    fixed = {(a.employee_id, a.date) for a in start.fixed}
    return start, data, (data, start.schedule.assignments + start.removed, fixed, start.blocked, started,
                         start.schedule.min_shifts_exempt)


def test_the_stage_keeps_shortage_changes_and_cost_and_moves_no_more_breaks(tmp_path, small_input):
    store = Store(str(tmp_path / "s.sqlite"))
    import_workbook(store, small_input)
    approve_draft(store, run_initial(store, 10, workers=2, seed=0).run.schedule_version)
    base = store.load_schedule(store.active_schedule_version())
    absent = next(a for a in base.assignments if a.date == WED)
    report_absence(store, absent.employee_id, WED)
    _, data, args = engine_inputs(store)

    off = reoptimize(*args, 20, workers=2, seed=0, minimize_break_moves=False)
    on = reoptimize(*args, 20, workers=2, seed=0)
    assert off.coverage_proven and off.stability_proven and off.cost_proven
    assert on.coverage_proven and on.stability_proven and on.cost_proven
    assert (on.total_shortage_slots, on.changed_assignments, on.total_cost) == \
        (off.total_shortage_slots, off.changed_assignments, off.total_cost)
    assert on.moved_breaks == moved_break_count(base, on.schedule)
    assert moved_break_count(base, on.schedule) <= moved_break_count(base, off.schedule)
    assert validate(data, on.schedule, group_size=2).passed
    assert any("break-moves stage" in d for d in on.diagnostics)


def test_a_break_that_does_not_matter_stays_where_it_was(tmp_path):
    """Demand only on Wednesday morning. A works Sun-Wed mornings, B four afternoons and is
    unavailable on Wednesday, so nobody can replace A and the future shifts are re-optimized.
    No shift outside Wednesday morning affects coverage, so the only change is A's removed
    shift (shortage 8 on Wednesday cannot be avoided) and no break moves. The breaks are
    deliberately not in the first allowed position."""
    store = Store(str(tmp_path / "db.sqlite"))
    demand = flat_demand(range(1, 7), 8, 16, 0)
    demand[4].update({hr: 1 for hr in range(8, 12)})
    data = make_input(employees=[("A", ()), ("B", (4,))], demand=demand,
                      templates=[(f"M{d}", d, h(8), 4, 100.0) for d in range(1, 7)] +
                                [(f"P{d}", d, h(12), 4, 100.0) for d in range(1, 7)])

    def shift(eid, day, start):
        return Assignment(WEEK + dt.timedelta(days=day - 1), eid, f"{'M' if start == h(8) else 'P'}{day}", start,
                          start + 240, (Break(start + 150, start + 180),), 4.0, 100.0)

    base_v = approved(store, data, Schedule([shift("A", d, h(8)) for d in (1, 2, 3, 4)] +
                                            [shift("B", d, h(12)) for d in (1, 2, 3, 5)]))
    report_absence(store, "A", WED)
    out = run_repair(store, 10, workers=2)
    assert out.mode == "REOPTIMIZED" and out.run.status == "OPTIMAL"
    draft = store.load_schedule(out.run.schedule_version)
    base = store.load_schedule(base_v)
    assert out.changed_assignments == 1 and out.run.total_shortage_slots == 8
    assert out.moved_breaks == 0 and diff_schedules(base, draft).break_moves == []
    assert "moves 0 break(s)" in out.message
