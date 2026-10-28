import asyncio
import sqlite3
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from neware8 import Client, MeasurementStore, RunState, StorageError
from neware8._bdf import PendingRecordTime
from helpers import PROFILE, XML, batch, history_payload


@pytest.fixture
def store(tmp_path):
    result = MeasurementStore(tmp_path)
    result.bind(b"\x01" * 16)
    yield result
    result.close()


def append(store, run, sequence, total, *, step=1, elapsed=None, cycle=1, step_type=2):
    payload = history_payload(
        sequence=sequence,
        time_ms=total,
        step_id=step,
        step_type=step_type,
        step_change_count=step,
        step_time_ms=total if elapsed is None else elapsed,
        cycle=cycle,
    )
    store.append(batch(payload), (run.id,))
    return payload[5:]


def test_boundary_time_is_anchored_to_step_clock_and_raw_is_preserved(store):
    run = store.begin(PROFILE, 1, XML)
    append(store, run, 1, 10000)
    raw = append(store, run, 2, 0, step=2, elapsed=0)
    append(store, run, 3, 15000, step=2, elapsed=4000)
    points = store.read(run.id)
    assert [p.test_time_second for p in points] == [10, 11, 15]
    assert points[1].raw == raw
    assert store.read(run.id, after_sequence=1, limit=1)[0].test_time_second == 11
    assert store.read(run.id, after_sequence=2, limit=1)[0].test_time_second == 15
    table = pq.read_table(store.export(run.id).path)
    assert table["test_time_second"].to_pylist() == [10, 11, 15]
    assert table["step_time_second"].to_pylist() == [10, 0, 4]
    assert table["record_index"].to_pylist() == [1, 2, 3]
    assert table["step_record_index"].to_pylist() == [1, 1, 2]
    assert table["step_type"].to_pylist() == ["CC_DISCHARGE"] * 3
    for row in table.to_pylist():
        assert row["power_watt"] == pytest.approx(row["voltage_volt"] * row["current_ampere"])


def test_pending_boundary_waits_for_next_record_and_survives_restart(tmp_path):
    with_store = MeasurementStore(tmp_path)
    with_store.bind(b"\x01" * 16)
    run = with_store.begin(PROFILE, 1, XML)
    append(with_store, run, 1, 10000)
    old = with_store.export(run.id)
    previous = old.path.read_bytes()
    append(with_store, run, 2, 0, step=2, elapsed=0)
    assert [p.sequence for p in with_store.read(run.id)] == [1]
    assert with_store.read(run.id, after_sequence=1) == ()
    with pytest.raises(PendingRecordTime):
        with_store.export(run.id)
    assert old.path.read_bytes() == previous
    with_store.close()
    reopened = MeasurementStore(tmp_path)
    try:
        append(reopened, run, 3, 12000, step=2, elapsed=1000)
        assert [p.test_time_second for p in reopened.read(run.id, after_sequence=1)] == [11, 12]
        assert reopened.export(run.id).record_count == 3
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "problem", ["gap", "preceding_gap", "different_step", "clock", "nonzero", "same_step"]
)
def test_unresolvable_times_are_not_interpolated_across_missing_or_inconsistent_data(
    store, problem
):
    run = store.begin(PROFILE, 1, XML)
    if problem != "preceding_gap":
        append(store, run, 1, 10000)
    append(
        store,
        run,
        2,
        1000 if problem == "nonzero" else 0,
        step=1 if problem == "same_step" else 2,
        elapsed=0,
    )
    append(
        store,
        run,
        4 if problem == "gap" else 3,
        12000,
        step=3 if problem == "different_step" else 2,
        elapsed=4000 if problem == "clock" else 1000,
    )
    with pytest.raises(StorageError):
        store.export(run.id)
    assert not list(store.directory.glob("*.partial"))
    if problem == "gap":
        append(store, run, 3, 11500, step=2, elapsed=500)
        assert [p.test_time_second for p in store.read(run.id)] == [10, 11, 11.5, 12]


def test_time_zero_at_test_start_is_not_modified(store):
    run = store.begin(PROFILE, 1, XML)
    append(store, run, 1, 0)
    append(store, run, 2, 1000)
    assert [p.test_time_second for p in store.read(run.id)] == [0, 1]


def test_cycles_and_step_ids_are_preserved_and_unknown_mode_is_null(store):
    run = store.begin(PROFILE, 1, XML)
    append(store, run, 1, 1000, step=7, cycle=5)
    append(store, run, 2, 2000, step=1, cycle=6, step_type=255)
    table = pq.read_table(store.export(run.id).path)
    assert table["cycle_count"].to_pylist() == [5, 6]
    assert table["step_id"].to_pylist() == [7, 1]
    assert table["step_type"].to_pylist() == ["CC_DISCHARGE", None]
    assert table["step_record_index"].to_pylist() == [1, 1]
    append(store, run, 3, 3000, cycle=4)
    with pytest.raises(StorageError, match="Cycle count regresses"):
        store.export(run.id)


def test_correction_crosses_parquet_batch_boundary(store):
    run = store.begin(PROFILE, 1, XML)
    for sequence in range(1, 4098):
        append(
            store,
            run,
            sequence,
            0 if sequence == 4096 else sequence * 1000,
            step=2 if sequence >= 4096 else 1,
            elapsed=(sequence - 4096) * 1000 if sequence >= 4096 else sequence * 1000,
        )
    table = pq.read_table(store.export(run.id).path)
    assert table.num_rows == 4097
    assert table["test_time_second"].to_pylist()[-3:] == [4095, 4096, 4097]
    assert store.read(run.id, after_sequence=4095, limit=1)[0].test_time_second == 4096


def test_unrelated_file_is_never_overwritten_or_deleted(store):
    run = store.begin(PROFILE, 1, XML)
    path = store.export(run.id).path
    path.write_bytes(b"unrelated data")
    with pytest.raises(StorageError, match="another file"):
        store.export(run.id)
    with pytest.raises(StorageError, match="replaced by another file"):
        store.begin(PROFILE, 2, XML)
    assert path.read_bytes() == b"unrelated data"


def test_schema_one_requires_explicit_new_storage(tmp_path):
    with sqlite3.connect(tmp_path / "measurements.sqlite") as db:
        db.execute("PRAGMA user_version=1")
    with pytest.raises(StorageError, match="expected 2"):
        MeasurementStore(tmp_path)


def test_finalization_grace_restarts_when_late_history_arrives(tmp_path):
    async def exercise():
        client = Client(
            host="127.0.0.1",
            controller_host="192.0.2.1",
            storage_directory=tmp_path,
            history_grace_period_s=12,
        )
        try:
            client.store.bind(b"1" * 16)
            run = client.store.begin(PROFILE, 1, XML)
            append(client.store, run, 1, 1000)
            client.store.set_state(run.id, RunState.FINISHED)
            sleeps = []

            async def sleep(delay):
                sleeps.append(delay)
                assert not (tmp_path / f"{run.id}.bdf.parquet").exists()
                if len(sleeps) == 1:
                    append(client.store, run, 2, 2000)
                    client._schedule_export(run.id)

            with patch("neware8.client.asyncio.sleep", side_effect=sleep):
                client._schedule_export(run.id)
                await client._exports[run.id]
            assert sleeps == [12, 12]
            assert client.error is None
            assert [point.sequence for point in client.store.read(run.id)] == [1, 2]
            assert all(point.raw == b"" for point in client.store.read(run.id))
            with client.store._transaction() as db:
                assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        finally:
            await client.close()

    asyncio.run(exercise())


def test_shutdown_during_grace_keeps_working_records(tmp_path):
    async def exercise():
        client = Client(
            host="127.0.0.1",
            controller_host="192.0.2.1",
            storage_directory=tmp_path,
            history_grace_period_s=3600,
        )
        client.store.bind(b"1" * 16)
        run = client.store.begin(PROFILE, 1, XML)
        append(client.store, run, 1, 1000)
        client.store.set_state(run.id, RunState.FINISHED)
        client._schedule_export(run.id)
        await asyncio.sleep(0)
        await asyncio.wait_for(client.close(), 1)
        reopened = MeasurementStore(tmp_path)
        try:
            assert reopened.read(run.id)[0].raw
            assert not (tmp_path / f"{run.id}.bdf.parquet").exists()
        finally:
            reopened.close()

    asyncio.run(exercise())


def test_automatic_export_waits_for_adjacent_history_without_recorder_error(tmp_path):
    async def exercise():
        client = Client(
            host="127.0.0.1",
            controller_host="192.0.2.1",
            storage_directory=tmp_path,
            history_grace_period_s=0.01,
        )
        try:
            client.store.bind(b"\x01" * 16)
            run = client.store.begin(PROFILE, 1, XML)
            append(client.store, run, 1, 10000)
            append(client.store, run, 2, 0, step=2, elapsed=0)
            client.store.set_state(run.id, RunState.FINISHED)
            client._dirty_exports.add(run.id)
            await client._export_finished(run.id)
            assert client.error is None
            append(client.store, run, 3, 12000, step=2, elapsed=1000)
            client._dirty_exports.add(run.id)
            await client._export_finished(run.id)
            assert client.error is None
            assert pq.read_table(tmp_path / f"{run.id}.bdf.parquet").num_rows == 3
        finally:
            await client.close()

    asyncio.run(exercise())
