"""SCRUM-17: import, solve, list runs and validate from the command line."""
from shiftopt.cli import main


def _run(capsys, *args):
    code = main(list(args))
    return code, capsys.readouterr().out


def test_import_solve_runs(tmp_path, capsys, small_input):
    db = str(tmp_path / "db.sqlite")
    code, out = _run(capsys, "--db", db, "import", str(small_input))
    assert code == 0 and "IN-0001 is now active" in out

    code, out = _run(capsys, "--db", db, "solve", "--time-budget", "20", "--workers", "2")
    assert code == 0
    for key in ("SolutionStatus", "TotalShortageSlots", "TotalCost", "FirstFeasibleSeconds",
                "OptimizationSeconds", "CoverageOptimalityProven", "CostOptimalityProven", "ValidationPassed"):
        assert key in out
    assert "SV-0001" in out and "DRAFT" in out

    code, out = _run(capsys, "--db", db, "runs")
    assert code == 0 and "RUN-0001" in out

    code, out = _run(capsys, "--db", db, "show-run", "RUN-0001")
    assert code == 0 and "ValidationPassed" in out


def test_solve_infeasible_prints_empty_metrics(tmp_path, capsys, infeasible_input):
    db = str(tmp_path / "db.sqlite")
    code, out = _run(capsys, "--db", db, "solve", "--time-budget", "10", "--workers", "2", "--input", str(infeasible_input))
    assert code == 1
    lines = dict(l.split(None, 1) if len(l.split(None, 1)) == 2 else (l.strip(), "") for l in out.splitlines()
                 if l and not l.startswith(" "))
    assert lines["SolutionStatus"].strip() == "INFEASIBLE"
    assert lines["TotalCost"] == "" and lines["FirstFeasibleSeconds"] == ""  # empty, not zero (spec 9)
    assert "needs at least 4 shifts" in out


def test_solve_without_input(tmp_path, capsys):
    code, out = _run(capsys, "--db", str(tmp_path / "db.sqlite"), "solve", "--time-budget", "5")
    assert code == 2 and "import" in out


def test_import_invalid_file(tmp_path, capsys, mutate):
    bad = mutate(lambda wb: wb["Employees"].__setitem__("A5", "E003"))
    code, out = _run(capsys, "--db", str(tmp_path / "db.sqlite"), "import", str(bad))
    assert code == 2 and out.startswith("INVALID_INPUT") and "Employees row 5" in out


def test_validate_sample(capsys, small_input, small_output):
    code, out = _run(capsys, "validate", str(small_input), str(small_output))
    assert code == 0 and "ValidationPassed    True" in out
