"""Persistence of input versions (SCRUM-21; extended with runs and schedule
versions in SCRUM-22/23).

An input version is immutable once stored. Exactly one input version is active.
A failed import never creates a version and never changes the active one
(spec section 6: "importing a broken file will not replace an existing valid data version").
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from shiftopt.domain import BreakRule, DemandHour, Employee, InputData, ShiftTemplate


class Base(DeclarativeBase):
    pass


class InputVersionRow(Base):
    __tablename__ = "input_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_name: Mapped[str] = mapped_column(String(255))
    schema_version: Mapped[str] = mapped_column(String(16))
    week_start: Mapped[str] = mapped_column(String(10))
    imported_at: Mapped[dt.datetime] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[str] = mapped_column(Text)  # JSON of the typed input


class Store:
    def __init__(self, url_or_path: str | Path = "sqlite:///shiftopt.db"):
        url = str(url_or_path)
        if "://" not in url:
            url = f"sqlite:///{url}"
        self.engine = create_engine(url, future=True)
        Base.metadata.create_all(self.engine)

    # ---- input versions
    def save_input_version(self, data: InputData) -> str:
        with Session(self.engine) as s, s.begin():
            s.execute(update(InputVersionRow).values(is_active=False))
            row = InputVersionRow(file_name=data.source_file, schema_version=data.schema_version,
                                  week_start=data.week_start.isoformat(), imported_at=dt.datetime.now(),
                                  is_active=True, payload=json.dumps(_input_to_json(data)))
            s.add(row)
            s.flush()
            return f"IN-{row.id:04d}"

    def active_input_version(self) -> str | None:
        with Session(self.engine) as s:
            row = s.scalars(select(InputVersionRow).where(InputVersionRow.is_active)).first()
            return None if row is None else f"IN-{row.id:04d}"

    def load_input(self, version: str | None = None) -> InputData | None:
        with Session(self.engine) as s:
            q = select(InputVersionRow)
            q = q.where(InputVersionRow.id == int(version.split("-")[1])) if version else q.where(InputVersionRow.is_active)
            row = s.scalars(q).first()
            return None if row is None else _input_from_json(json.loads(row.payload), row.file_name)

    def count_input_versions(self) -> int:
        with Session(self.engine) as s:
            return len(s.scalars(select(InputVersionRow.id)).all())


# ---------------------------------------------------------------- JSON mapping
def _input_to_json(d: InputData) -> dict:
    return {
        "schema_version": d.schema_version,
        "week_start": d.week_start.isoformat(),
        "employees": [{"id": e.employee_id, "name": e.name, "unavailable": sorted(e.unavailable_days),
                       "preferred_start": e.preferred_start} for e in d.employees.values()],
        "demand": [{"date": k.date.isoformat(), "hour": k.hour_start, "required": k.required} for k in d.demand.values()],
        "templates": [{"id": t.shift_id, "day": t.day_of_week, "start": t.start, "hours": t.duration_hours,
                       "cost": t.cost} for t in d.templates.values()],
        "break_rules": [vars(b) for b in d.break_rules.values()],
    }


def _input_from_json(j: dict, file_name: str) -> InputData:
    emps = {e["id"]: Employee(e["id"], e["name"], frozenset(e["unavailable"]), e["preferred_start"]) for e in j["employees"]}
    demand = {}
    for x in j["demand"]:
        d = dt.date.fromisoformat(x["date"])
        demand[(d, x["hour"])] = DemandHour(d, x["hour"], x["required"])
    temps = {t["id"]: ShiftTemplate(t["id"], t["day"], t["start"], t["hours"], t["cost"]) for t in j["templates"]}
    rules = {b["duration_hours"]: BreakRule(**b) for b in j["break_rules"]}
    return InputData(j["schema_version"], dt.date.fromisoformat(j["week_start"]), emps, demand, temps, rules, file_name)
