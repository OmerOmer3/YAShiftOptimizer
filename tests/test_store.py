"""SCRUM-21: a failed import leaves the previous valid input active."""
from shiftopt.db import Store
from shiftopt.ingest import import_workbook, read_input


def test_invalid_input_keeps_previous_version(tmp_path, small_input, mutate):
    store = Store(tmp_path / "db.sqlite")
    ok = import_workbook(store, small_input)
    assert ok.status == "OK"
    assert store.active_input_version() == ok.input_version

    bad_path = mutate(lambda wb: wb["Employees"].__setitem__("A5", "E003"))  # duplicate ID
    bad = import_workbook(store, bad_path)
    assert bad.status == "INPUT_INVALID"
    assert bad.input_version is None
    assert bad.errors[0].sheet == "Employees" and bad.errors[0].row == 5
    assert store.active_input_version() == ok.input_version
    assert store.count_input_versions() == 1


def test_new_valid_import_becomes_active(tmp_path, small_input, large_input):
    store = Store(tmp_path / "db.sqlite")
    a = import_workbook(store, small_input)
    b = import_workbook(store, large_input)
    assert a.input_version != b.input_version
    assert store.active_input_version() == b.input_version


def test_stored_input_round_trips(tmp_path, small_input):
    store = Store(tmp_path / "db.sqlite")
    v = import_workbook(store, small_input).input_version
    loaded = store.load_input(v)
    original = read_input(small_input).data
    assert loaded.employees == original.employees
    assert loaded.demand == original.demand
    assert loaded.templates == original.templates
    assert loaded.break_rules == original.break_rules
    assert loaded.week_start == original.week_start
