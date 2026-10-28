import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from neware8 import MeasurementStore, RunState, StateError, StorageError
from helpers import PROFILE, XML, batch, history_payload


@pytest.fixture
def store(tmp_path):
    result = MeasurementStore(tmp_path)
    result.bind(b"\x01" * 16)
    yield result
    result.close()


def test_record_only_selected_runs_and_deduplicate(store):
    run = store.begin(PROFILE, 1, XML)
    assert store.append(batch(history_payload(test_id=2)), (run.id,)) == ()
    assert store.append(batch(history_payload()), ()) == ()
    changes = store.append(batch(history_payload()), (run.id,))
    assert tuple(change.run_id for change in changes) == (run.id,)
    assert not changes[0].backfill
    assert store.append(batch(history_payload()), (run.id,)) == ()
    assert len(store.read(run.id)) == 1
    assert store.read(run.id, after_sequence=1) == ()


def test_finalized_records_survive_restart_without_native_bytes(tmp_path):
    store = MeasurementStore(tmp_path)
    store.bind(b"1" * 16)
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload(sequence=1)), (run.id,))
    store.append(batch(history_payload(sequence=3)), (run.id,))
    with store._transaction() as db:
        db.execute("INSERT INTO opaque_records VALUES (?, ?)", (run.id, b"auxiliary bytes"))
    before = tuple(replace(point, raw=b"") for point in store.read(run.id))
    store.set_state(run.id, RunState.FINISHED)
    result = store.finalize(run.id)
    from neware8._bdf import SCHEMA

    assert pq.read_schema(result.path) == SCHEMA
    assert result.missing_sequence_ranges == ((2, 2),)
    with sqlite3.connect(tmp_path / "measurements.sqlite") as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM opaque_records").fetchone()[0] == 0
    store.close()
    store = MeasurementStore(tmp_path)
    try:
        assert store.read(run.id) == before
        assert store.read(run.id, after_sequence=1, limit=1) == before[1:]
        assert store.get(run.id).record_count == 2
        with patch.object(store, "_points", side_effect=AssertionError("must use Parquet")):
            assert store.export(run.id).path == result.path
            assert store.finalize(run.id).record_count == 2
            copy = store.export(run.id, tmp_path / "copy.bdf.parquet")
            assert copy.path.read_bytes() == result.path.read_bytes()
        for sequence in (2, 3):
            with pytest.raises(StorageError, match="after finalization"):
                store.append(batch(history_payload(sequence=sequence)), (run.id,))
        assert store.get(run.id).record_count == 2
        assert [point.sequence for point in store.read(run.id)] == [1, 3]
        store.clear(PROFILE.address)
        assert not result.path.exists()
        assert not copy.path.exists()
    finally:
        store.close()


def test_failed_finalization_keeps_database_records(store):
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload()), (run.id,))
    with store._transaction() as db:
        db.execute("INSERT INTO opaque_records VALUES (?, ?)", (run.id, b"auxiliary bytes"))
    store.set_state(run.id, RunState.FINISHED)
    with patch("neware8.storage.os.replace", side_effect=OSError("publication failed")):
        with pytest.raises(OSError, match="publication failed"):
            store.finalize(run.id)
    assert len(store.read(run.id)) == 1
    with store._transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM opaque_records").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM archives").fetchone()[0] == 0


def test_archived_run_rejects_late_records_without_reopening(store):
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload()), (run.id,))
    store.set_state(run.id, RunState.FINISHED)
    store.finalize(run.id)
    combined = (
        b"\x02\x00\x00\x00\x00" + history_payload(sequence=2)[5:] + history_payload(current=124)[5:]
    )
    with pytest.raises(StorageError, match="after finalization"):
        store.append(batch(combined), (run.id,))
    assert store.get(run.id).record_count == 1
    with store._transaction() as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM archives").fetchone()[0] == 1


def test_changes_identify_backfill_after_store_reopens(tmp_path):
    first = MeasurementStore(tmp_path)
    first.bind(b"\x01" * 16)
    run = first.begin(PROFILE, 1, XML)
    change = first.append(batch(history_payload(sequence=3)), (run.id,))[0]
    assert change.first_sequence == change.last_sequence == 3
    assert not change.backfill
    first.close()
    second = MeasurementStore(tmp_path)
    try:
        change = second.append(batch(history_payload(sequence=2)), (run.id,))[0]
        assert change.backfill and change.first_sequence == 2
        assert not second.append(batch(history_payload(sequence=4)), (run.id,))[0].backfill
    finally:
        second.close()


def test_conflict_rolls_back_entire_batch(store):
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload()), (run.id,))
    combined = (
        b"\x02\x00\x00\x00\x00" + history_payload(sequence=2)[5:] + history_payload(current=124)[5:]
    )
    with pytest.raises(StorageError, match="conflicting"):
        store.append(batch(combined), (run.id,))
    assert store.get(run.id).record_count == 1


def test_restart_preserves_raw_data_and_controller_binding(tmp_path):
    store = MeasurementStore(tmp_path)
    store.bind(b"\x01" * 16)
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload()), (run.id,))
    store.close()
    reopened = MeasurementStore(tmp_path)
    try:
        reopened.bind(b"\x01" * 16)
        assert reopened.read(run.id)[0].raw == history_payload()[5:]
        with pytest.raises(StorageError, match="different controller"):
            reopened.bind(b"\x02" * 16)
    finally:
        reopened.close()


def test_bdf_export_retention_and_replacement(store):
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload()), (run.id,))
    store.set_state(run.id, RunState.FINISHED)
    result = store.export(run.id)
    table = pq.read_table(result.path)
    assert set(table.column_names) == {
        "test_time_second",
        "voltage_volt",
        "current_ampere",
        "cycle_count",
        "step_id",
        "record_index",
        "step_time_second",
        "step_type",
        "step_record_index",
        "power_watt",
    } | {
        prefix + name
        for prefix in ("", "step_", "cycle_")
        for name in (
            "charging_capacity_ah",
            "discharging_capacity_ah",
            "cumulative_capacity_ah",
            "net_capacity_ah",
            "charging_energy_wh",
            "discharging_energy_wh",
            "cumulative_energy_wh",
            "net_energy_wh",
        )
    }
    assert table.schema.metadata is None
    assert set(pq.read_metadata(result.path).metadata) == {b"ARROW:schema"}
    assert store.read(run.id)[0].raw == history_payload()[5:]
    new = store.begin(PROFILE, 2, XML)
    assert not result.path.exists()
    assert new.record_count == 0
    with pytest.raises(KeyError):
        store.read(run.id)
    with pytest.raises(StateError):
        store.begin(PROFILE, 2, XML)


def test_gaps_and_late_records_remain_explicit(store):
    run = store.begin(PROFILE, 1, XML)
    store.append(batch(history_payload(sequence=3)), (run.id,))
    store.set_state(run.id, RunState.FINISHED)
    exported = store.export(run.id)
    assert exported.missing_sequence_ranges == ((1, 2),)
    store.append(batch(history_payload(sequence=1)), (run.id,))
    assert store.export(run.id).missing_sequence_ranges == ((2, 2),)


def test_newer_schema_is_rejected(tmp_path):
    with sqlite3.connect(tmp_path / "measurements.sqlite") as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(StorageError, match="schema"):
        MeasurementStore(tmp_path)


def test_replacement_removes_all_managed_exports_but_other_channels_survive(store, tmp_path):
    from neware8 import ChannelAddress, ChannelProfile

    first = store.begin(PROFILE, 1, XML)
    second = store.begin(ChannelProfile(ChannelAddress(1, 2), 1, 5, 0.01, 10000), 2, XML)
    default = store.export(first.id).path
    custom = store.export(first.id, tmp_path / "custom.bdf.parquet").path
    other = store.export(second.id).path
    store.begin(PROFILE, 3, XML)
    assert not default.exists()
    assert not custom.exists()
    assert other.exists()
    assert store.get(second.id).test_id == 2
