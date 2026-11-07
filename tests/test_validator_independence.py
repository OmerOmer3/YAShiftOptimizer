"""SCRUM-25 acceptance criterion: the validator must not import any module from the engine.

Spec section 4: the independent checker computes from the result, separately from the
code that builds the solver model.
"""
import ast
from pathlib import Path

VALIDATOR = Path(__file__).resolve().parent.parent / "shiftopt" / "validator"


def test_validator_does_not_import_engine():
    offenders = []
    for py in VALIDATOR.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{py.name}: {n}" for n in names if n == "shiftopt.engine" or n.startswith("shiftopt.engine.")]
    assert not offenders, offenders
