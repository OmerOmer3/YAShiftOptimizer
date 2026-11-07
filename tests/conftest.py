from pathlib import Path

import openpyxl
import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def small_input() -> Path:
    return SAMPLES / "Input_Small_10.xlsx"


@pytest.fixture
def large_input() -> Path:
    return SAMPLES / "Input_Large_50.xlsx"


@pytest.fixture
def infeasible_input() -> Path:
    return SAMPLES / "Input_Infeasible.xlsx"


@pytest.fixture
def small_output() -> Path:
    return SAMPLES / "Output_Small_10.xlsx"


@pytest.fixture
def mutate(tmp_path, small_input):
    """mutate(fn) -> path of a copy of Input_Small_10 after fn(workbook) edits it."""
    counter = {"n": 0}

    def _do(fn, source: Path = small_input) -> Path:
        wb = openpyxl.load_workbook(source)
        fn(wb)
        counter["n"] += 1
        out = tmp_path / f"mutated_{counter['n']}.xlsx"
        wb.save(out)
        return out

    return _do
