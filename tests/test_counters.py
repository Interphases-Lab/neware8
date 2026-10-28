from fractions import Fraction
import json
import struct
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from neware8 import MeasurementStore
from neware8._counters import CounterTotals
from helpers import FIXTURES, PROFILE, history_payload
from helpers import batch as batch_of


def record(sequence, step, counters, *, cycle=0, raw_range=-10, change=None):
    return SimpleNamespace(
        sequence=sequence,
        cycle=cycle,
        step_id=step,
        step_change_count=step if change is None else change,
        range_raw=raw_range,
        **dict(
            zip(
                (
                    "charge_capacity_raw",
                    "discharge_capacity_raw",
                    "charge_energy_raw",
                    "discharge_energy_raw",
                ),
                counters,
            )
        ),
    )


def test_step_cycle_and_test_totals_keep_direction_and_do_not_sum_every_sample():
    totals = CounterTotals(None)
    totals.update(record(1, 1, (0, 0, 0, 0)))
    totals.update(record(2, 1, (100, 0, 300, 0)))
    first_end = totals.update(record(3, 1, (200, 0, 600, 0)))
    next_start = totals.update(record(4, 2, (0, 0, 0, 0)))
    assert next_start["charging_capacity_ah"] == first_end["charging_capacity_ah"]
    assert next_start["step_charging_capacity_ah"] == 0
    totals.update(record(5, 2, (100, 0, 400, 0)))
    totals.update(record(6, 3, (0, 0, 0, 0)))
    end = totals.update(record(7, 3, (0, 500, 0, 1500)))
    assert end["charging_capacity_ah"] == pytest.approx(300 / 36_000_000_000)
    assert end["discharging_capacity_ah"] == pytest.approx(500 / 36_000_000_000)
    assert end["cumulative_capacity_ah"] == pytest.approx(800 / 36_000_000_000)
    assert end["net_capacity_ah"] == pytest.approx(-200 / 36_000_000_000)
    assert end["charging_energy_wh"] == pytest.approx(1000 / 36_000_000_000)
    assert end["discharging_energy_wh"] == pytest.approx(1500 / 36_000_000_000)
    assert end["net_energy_wh"] == pytest.approx(-500 / 36_000_000_000)
    next_cycle = totals.update(record(8, 1, (0, 0, 0, 0), cycle=1))
    assert next_cycle["charging_capacity_ah"] == end["charging_capacity_ah"]
    assert next_cycle["cycle_charging_capacity_ah"] == 0
    assert next_cycle["cycle_discharging_energy_wh"] == 0


def test_each_record_uses_its_range_including_automatic_range_changes():
    totals = CounterTotals(-10)
    first = totals.update(record(1, 1, (100, 0, 200, 0), raw_range=0))
    same = totals.update(record(2, 1, (1000, 0, 2000, 0), raw_range=-5))
    assert first == same
    last = totals.update(record(3, 1, (2000, 0, 4000, 0), raw_range=-5))
    assert last["charging_capacity_ah"] == 2 * first["charging_capacity_ah"]
    with pytest.raises(ValueError, match="explicit channel range"):
        CounterTotals(None).update(record(1, 1, (0, 0, 0, 0), raw_range=0))


def test_step_execution_counter_wrap_and_repeated_program_step_ids():
    totals = CounterTotals(None)
    totals.update(record(1, 2, (100, 0, 300, 0), change=255))
    result = totals.update(record(2, 2, (20, 0, 60, 0), change=0))
    assert result["step_charging_capacity_ah"] == pytest.approx(20 / 36_000_000_000)
    assert result["charging_capacity_ah"] == pytest.approx(120 / 36_000_000_000)


@pytest.mark.parametrize("start", [1, 7])
def test_missing_history_does_not_produce_false_test_or_cycle_totals(start):
    totals = CounterTotals(None)
    first = totals.update(record(start, 1, (100, 0, 300, 0)))
    assert (first["charging_capacity_ah"] is None) == (start != 1)
    missing = totals.update(record(start + 2, 1, (300, 0, 900, 0)))
    assert missing["step_charging_capacity_ah"] is not None
    assert missing["charging_capacity_ah"] is None
    assert missing["cycle_charging_capacity_ah"] is None
    next_cycle = totals.update(record(start + 3, 1, (10, 0, 30, 0), cycle=1))
    assert next_cycle["charging_capacity_ah"] is None
    assert next_cycle["cycle_charging_capacity_ah"] == pytest.approx(10 / 36_000_000_000)


def test_invalid_counter_does_not_contaminate_other_quantities():
    totals = CounterTotals(None)
    totals.update(record(1, 1, (100, 0, 200, 0)))
    bad = totals.update(record(2, 1, (90, 0, 300, -1)))
    assert bad["step_charging_capacity_ah"] is None
    assert bad["net_capacity_ah"] is None
    assert bad["discharging_energy_wh"] is None
    assert bad["charging_energy_wh"] == pytest.approx(300 / 36_000_000_000)
    assert totals.update(record(3, 1, (200, 0, 400, 0)))["charging_capacity_ah"] is None
    recovered = totals.update(record(4, 1, (30, 0, 80, 0), cycle=1))
    assert recovered["step_charging_capacity_ah"] is not None
    assert recovered["cycle_charging_capacity_ah"] is not None
    assert recovered["charging_capacity_ah"] is None
    assert recovered["charging_energy_wh"] == pytest.approx(480 / 36_000_000_000)


EXPECTED = {
    "contact_check": (100000, 12999904, 6999921, 573686, 443052),
    "cc_cv": (100000, 1129319, 0, 52878, 0),
    "cycling": (100000, 2006718528, 2160206656, 83693654, 115166578),
}


@pytest.mark.parametrize("name", EXPECTED)
def test_captured_counter_resets_and_totals_export_after_reopen(tmp_path, name):
    fixture = json.loads((FIXTURES / "counters.json").read_text())
    data = next(item for item in fixture["datasets"] if item["name"] == name)
    records = [bytes.fromhex(raw) for raw in data["records"]]
    store = MeasurementStore(tmp_path)
    store.bind(bytes(range(16)))
    run = store.begin(PROFILE, 1, None)
    for offset in range(0, len(records), 100):
        batch = records[offset : offset + 100]
        store.append(batch_of(bytes([len(batch)]) + bytes(4) + b"".join(batch)), (run.id,))
    store.close()
    store = MeasurementStore(tmp_path)
    try:
        table = pq.read_table(store.export(run.id).path)
        assert table.num_rows == len(records)
        scale, charge, discharge, charge_energy, discharge_energy = EXPECTED[name]
        last = table.to_pylist()[-1]
        for column, value in (
            ("charging_capacity_ah", charge),
            ("discharging_capacity_ah", discharge),
            ("charging_energy_wh", charge_energy),
            ("discharging_energy_wh", discharge_energy),
        ):
            assert last[column] == float(Fraction(value, scale * 3_600_000))
        for row in table.to_pylist():
            for prefix in ("step_", "cycle_", ""):
                for kind, unit in (("capacity", "ah"), ("energy", "wh")):
                    charging = row[f"{prefix}charging_{kind}_{unit}"]
                    discharging = row[f"{prefix}discharging_{kind}_{unit}"]
                    assert row[f"{prefix}cumulative_{kind}_{unit}"] == pytest.approx(
                        charging + discharging
                    )
                    assert row[f"{prefix}net_{kind}_{unit}"] == pytest.approx(
                        charging - discharging
                    )
        if name == "cycling":
            assert last["cycle_charging_capacity_ah"] == float(Fraction(653750656, 360_000_000_000))
            assert last["cycle_discharging_capacity_ah"] == float(
                Fraction(720066496, 360_000_000_000)
            )
    finally:
        store.close()


def test_late_history_restores_totals_and_parquet_batch_does_not_reset_them(tmp_path):
    store = MeasurementStore(tmp_path)
    store.bind(bytes(range(16)))
    run = store.begin(PROFILE, 1, None)

    def append(sequence):
        raw = bytearray(history_payload(sequence=sequence, time_ms=sequence * 1000)[5:])
        struct.pack_into("<qqqq", raw, 43, sequence * 100, 0, sequence * 300, 0)
        struct.pack_into("<i", raw, 82, -10)
        store.append(batch_of(b"\x01" + bytes(4) + raw), (run.id,))

    try:
        append(1)
        append(3)
        partial = pq.read_table(store.export(run.id).path)
        assert partial["charging_capacity_ah"].to_pylist()[-1] is None
        assert partial["step_charging_capacity_ah"].to_pylist()[-1] is not None
        append(2)
        for sequence in range(4, 4098):
            append(sequence)
        complete = pq.read_table(store.export(run.id).path)
        assert complete["charging_capacity_ah"].null_count == 0
        assert complete["charging_capacity_ah"].to_pylist()[-1] == pytest.approx(
            409700 / 36_000_000_000
        )
    finally:
        store.close()
