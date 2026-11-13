"""Optimization engine (CP-SAT): shortage first, then cost (SCRUM-28..31)."""
from shiftopt.engine.solver import SolveResult, StageReport, infeasibility_reasons, solve

__all__ = ["solve", "SolveResult", "StageReport", "infeasibility_reasons"]
