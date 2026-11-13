"""Persistence (SCRUM-21, SCRUM-22, SCRUM-23).

Spec section 4: the database stores employees, demand, templates, break rules, runs,
versions, locks and absences. This module covers the first six; locks, absences and
the audit log arrive with sprints 3-4.

  input_versions      one row per successful import; exactly one is active
  employees, demand_hours, shift_templates, break_rules
                      the typed input, keyed by input version (immutable once stored)
  runs                one row per optimization run, whatever its outcome
  schedule_versions   one row per schedule produced; a new run creates a DRAFT and never
                      changes the active schedule (spec section 4, "a new run does not
                      change the active schedule")
  assignments         the shifts of a schedule version, breaks included
  schedule_exemptions employees exempt from the 4-shift minimum in that version

Money is stored in whole cents so totals are exact.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from shiftopt.domain import (
    Assignment,
    Break,
    BreakRule,
    DemandHour,
    Employee,
    InputData,
    Schedule,
    ShiftTemplate,
)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- input
class InputVersionRow(Base):
    __tablename__ = "input_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String(255))
    schema_version: Mapped[str] = mapped_column(String(16))
    week_start: Mapped[dt.date] = mapped_column(Date)
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class EmployeeRow(Base):
    __tablename__ = "employees"
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    unavailable_day1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unavailable_day2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # minutes


class DemandRow(Base):
    __tablename__ = "demand_hours"
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    hour_start: Mapped[int] = mapped_column(Integer, primary_key=True)  # minutes
    required: Mapped[int] = mapped_column(Integer)


class TemplateRow(Base):
    __tablename__ = "shift_templates"
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"), primary_key=True)
    shift_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_of_week: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)  # minutes
    duration_hours: Mapped[int] = mapped_column(Integer)
    cost_cents: Mapped[int] = mapped_column(Integer)


class BreakRuleRow(Base):
    __tablename__ = "break_rules"
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"), primary_key=True)
    duration_hours: Mapped[int] = mapped_column(Integer, primary_key=True)
    break_count: Mapped[int] = mapped_column(Integer)
    break_minutes: Mapped[int] = mapped_column(Integer)
    min_start_gap: Mapped[int] = mapped_column(Integer)
    min_end_gap: Mapped[int] = mapped_column(Integer)
    min_between_breaks: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- runs and schedules
class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"))
    run_type: Mapped[str] = mapped_column(String(16))  # INITIAL or REPAIR
    group_size: Mapped[int] = mapped_column(Integer)
    time_budget_seconds: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    termination_reason: Mapped[str] = mapped_column(Text)
    first_feasible_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    optimization_seconds: Mapped[float] = mapped_column(Float)
    total_shortage_slots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_proven: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cost_proven: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validation_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    solver_info: Mapped[str] = mapped_column(Text)   # JSON: version, workers, seed, hardware
    diagnostics: Mapped[str] = mapped_column(Text)   # JSON list of messages for the manager
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)


class ScheduleVersionRow(Base):
    __tablename__ = "schedule_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    input_version_id: Mapped[int] = mapped_column(ForeignKey("input_versions.id"))
    base_version_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_versions.id"), nullable=True)
    approval_status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT or APPROVED
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AssignmentRow(Base):
    __tablename__ = "assignments"
    schedule_version_id: Mapped[int] = mapped_column(ForeignKey("schedule_versions.id"), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    shift_id: Mapped[str] = mapped_column(String(64))
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    break1_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break1_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break2_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break2_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid_hours: Mapped[float] = mapped_column(Float)
    cost_cents: Mapped[int] = mapped_column(Integer)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)


class ExemptionRow(Base):
    __tablename__ = "schedule_exemptions"
    schedule_version_id: Mapped[int] = mapped_column(ForeignKey("schedule_versions.id"), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), primary_key=True)


# ---------------------------------------------------------------- ids shown to users
def _code(prefix: str, n: int) -> str:
    return f"{prefix}-{n:04d}"


def _num(code: str) -> int:
    return int(code.split("-")[1])


def cents(x: float) -> int:
    return round(x * 100)


@dataclass
class RunRecord:
    """A run as stored, in the shape the CLI, the MCP tools and the export need."""
    run_id: str
    input_version: str
    run_type: str
    group_size: int
    time_budget_seconds: float
    status: str
    termination_reason: str
    first_feasible_seconds: float | None
    optimization_seconds: float
    total_shortage_slots: int | None
    total_cost: float | None
    coverage_proven: bool | None
    cost_proven: bool | None
    validation_passed: bool | None
    solver_info: dict
    diagnostics: list[str]
    started_at: dt.datetime
    schedule_version: str | None
    approval_status: str | None
    base_schedule_version: str | None


class Store:
    def __init__(self, url_or_path: str | Path = "sqlite:///shiftopt.db"):
        url = str(url_or_path)
        if "://" not in url:
            url = f"sqlite:///{url}"
        self.engine = create_engine(url, future=True)
        if url.startswith("sqlite"):
            # SQLite enforces foreign keys only when asked to.
            event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)

    # ================================================================ input versions
    def save_input_version(self, data: InputData) -> str:
        """Store a validated input and make it the active version, in one transaction."""
        with Session(self.engine) as s, s.begin():
            s.execute(update(InputVersionRow).values(is_active=False))
            iv = InputVersionRow(file_name=data.source_file, schema_version=data.schema_version,
                                 week_start=data.week_start, imported_at=dt.datetime.now(), is_active=True)
            s.add(iv)
            s.flush()
            for e in data.employees.values():
                days = sorted(e.unavailable_days) + [None, None]
                s.add(EmployeeRow(input_version_id=iv.id, employee_id=e.employee_id, name=e.name,
                                  unavailable_day1=days[0], unavailable_day2=days[1], preferred_start=e.preferred_start))
            for dh in data.demand.values():
                s.add(DemandRow(input_version_id=iv.id, date=dh.date, hour_start=dh.hour_start, required=dh.required))
            for t in data.templates.values():
                s.add(TemplateRow(input_version_id=iv.id, shift_id=t.shift_id, day_of_week=t.day_of_week,
                                  start=t.start, duration_hours=t.duration_hours, cost_cents=cents(t.cost)))
            for r in data.break_rules.values():
                s.add(BreakRuleRow(input_version_id=iv.id, duration_hours=r.duration_hours, break_count=r.break_count,
                                   break_minutes=r.break_minutes, min_start_gap=r.min_start_gap,
                                   min_end_gap=r.min_end_gap, min_between_breaks=r.min_between_breaks))
            return _code("IN", iv.id)

    def active_input_version(self) -> str | None:
        with Session(self.engine) as s:
            row = s.scalars(select(InputVersionRow).where(InputVersionRow.is_active)).first()
            return None if row is None else _code("IN", row.id)

    def count_input_versions(self) -> int:
        with Session(self.engine) as s:
            return s.scalar(select(func.count()).select_from(InputVersionRow))

    def load_input(self, version: str | None = None) -> InputData | None:
        with Session(self.engine) as s:
            q = select(InputVersionRow)
            q = q.where(InputVersionRow.id == _num(version)) if version else q.where(InputVersionRow.is_active)
            iv = s.scalars(q).first()
            if iv is None:
                return None
            vid = iv.id
            emps = {}
            for r in s.scalars(select(EmployeeRow).where(EmployeeRow.input_version_id == vid)):
                days = frozenset(d for d in (r.unavailable_day1, r.unavailable_day2) if d is not None)
                emps[r.employee_id] = Employee(r.employee_id, r.name, days, r.preferred_start)
            demand = {(r.date, r.hour_start): DemandHour(r.date, r.hour_start, r.required)
                      for r in s.scalars(select(DemandRow).where(DemandRow.input_version_id == vid))}
            temps = {r.shift_id: ShiftTemplate(r.shift_id, r.day_of_week, r.start, r.duration_hours, r.cost_cents / 100)
                     for r in s.scalars(select(TemplateRow).where(TemplateRow.input_version_id == vid))}
            rules = {r.duration_hours: BreakRule(r.duration_hours, r.break_count, r.break_minutes, r.min_start_gap,
                                                 r.min_end_gap, r.min_between_breaks)
                     for r in s.scalars(select(BreakRuleRow).where(BreakRuleRow.input_version_id == vid))}
            return InputData(iv.schema_version, iv.week_start, emps, demand, temps, rules, iv.file_name)

    # ================================================================ runs and schedule versions
    def save_run(self, *, input_version: str, run_type: str, group_size: int, result, validation_passed: bool | None,
                 optimization_seconds: float, started_at: dt.datetime, status: str | None = None,
                 termination_reason: str | None = None, diagnostics: list[str] | None = None,
                 schedule: Schedule | None = None, base_version: str | None = None) -> tuple[str, str | None]:
        """Store one run and, if it produced a schedule, a new DRAFT schedule version.
        Returns (run_id, schedule_version or None). The active schedule is never touched."""
        with Session(self.engine) as s, s.begin():
            run = RunRow(
                input_version_id=_num(input_version), run_type=run_type, group_size=group_size,
                time_budget_seconds=result.time_budget_seconds, status=status or result.status,
                termination_reason=termination_reason or result.termination_reason,
                first_feasible_seconds=result.first_feasible_seconds, optimization_seconds=optimization_seconds,
                total_shortage_slots=result.total_shortage_slots,
                total_cost_cents=None if result.total_cost is None else cents(result.total_cost),
                coverage_proven=result.coverage_proven, cost_proven=result.cost_proven,
                validation_passed=validation_passed, solver_info=json.dumps(result.solver_info),
                diagnostics=json.dumps(diagnostics if diagnostics is not None else result.diagnostics),
                started_at=started_at)
            s.add(run)
            s.flush()
            version = None
            if schedule is not None:
                sv = ScheduleVersionRow(run_id=run.id, input_version_id=run.input_version_id,
                                        base_version_id=_num(base_version) if base_version else None,
                                        approval_status="DRAFT", is_active=False, created_at=dt.datetime.now())
                s.add(sv)
                s.flush()
                for a in schedule.assignments:
                    br = list(a.breaks) + [None, None]
                    s.add(AssignmentRow(
                        schedule_version_id=sv.id, employee_id=a.employee_id, date=a.date, shift_id=a.shift_id,
                        start=a.start, end=a.end,
                        break1_start=br[0].start if br[0] else None, break1_end=br[0].end if br[0] else None,
                        break2_start=br[1].start if br[1] else None, break2_end=br[1].end if br[1] else None,
                        paid_hours=a.paid_hours, cost_cents=cents(a.cost), is_locked=a.is_locked))
                for eid in sorted(schedule.min_shifts_exempt):
                    s.add(ExemptionRow(schedule_version_id=sv.id, employee_id=eid))
                version = _code("SV", sv.id)
            return _code("RUN", run.id), version

    def get_run(self, run_id: str) -> RunRecord | None:
        with Session(self.engine) as s:
            run = s.get(RunRow, _num(run_id))
            return None if run is None else self._record(s, run)

    def list_runs(self) -> list[RunRecord]:
        with Session(self.engine) as s:
            return [self._record(s, r) for r in s.scalars(select(RunRow).order_by(RunRow.id))]

    def _record(self, s: Session, run: RunRow) -> RunRecord:
        sv = s.scalars(select(ScheduleVersionRow).where(ScheduleVersionRow.run_id == run.id)).first()
        return RunRecord(
            run_id=_code("RUN", run.id), input_version=_code("IN", run.input_version_id), run_type=run.run_type,
            group_size=run.group_size, time_budget_seconds=run.time_budget_seconds, status=run.status,
            termination_reason=run.termination_reason, first_feasible_seconds=run.first_feasible_seconds,
            optimization_seconds=run.optimization_seconds, total_shortage_slots=run.total_shortage_slots,
            total_cost=None if run.total_cost_cents is None else run.total_cost_cents / 100,
            coverage_proven=run.coverage_proven, cost_proven=run.cost_proven,
            validation_passed=run.validation_passed, solver_info=json.loads(run.solver_info),
            diagnostics=json.loads(run.diagnostics), started_at=run.started_at,
            schedule_version=None if sv is None else _code("SV", sv.id),
            approval_status=None if sv is None else sv.approval_status,
            base_schedule_version=None if sv is None or sv.base_version_id is None else _code("SV", sv.base_version_id))

    def load_schedule(self, version: str) -> Schedule | None:
        with Session(self.engine) as s:
            sv = s.get(ScheduleVersionRow, _num(version))
            if sv is None:
                return None
            sched = Schedule()
            rows = s.scalars(select(AssignmentRow).where(AssignmentRow.schedule_version_id == sv.id)
                             .order_by(AssignmentRow.date, AssignmentRow.employee_id))
            for r in rows:
                breaks = tuple(Break(b, e) for b, e in ((r.break1_start, r.break1_end), (r.break2_start, r.break2_end))
                               if b is not None)
                sched.assignments.append(Assignment(r.date, r.employee_id, r.shift_id, r.start, r.end, breaks,
                                                    r.paid_hours, r.cost_cents / 100, r.is_locked))
            sched.min_shifts_exempt = set(s.scalars(select(ExemptionRow.employee_id)
                                                    .where(ExemptionRow.schedule_version_id == sv.id)))
            return sched

    def schedule_version_info(self, version: str) -> dict | None:
        with Session(self.engine) as s:
            sv = s.get(ScheduleVersionRow, _num(version))
            if sv is None:
                return None
            return {"schedule_version": _code("SV", sv.id), "run_id": _code("RUN", sv.run_id),
                    "input_version": _code("IN", sv.input_version_id), "approval_status": sv.approval_status,
                    "is_active": sv.is_active,
                    "base_version": None if sv.base_version_id is None else _code("SV", sv.base_version_id)}

    def active_schedule_version(self) -> str | None:
        """The approved, active schedule. Only approval (sprint 3, SCRUM-37) sets it."""
        with Session(self.engine) as s:
            sv = s.scalars(select(ScheduleVersionRow).where(ScheduleVersionRow.is_active)).first()
            return None if sv is None else _code("SV", sv.id)
