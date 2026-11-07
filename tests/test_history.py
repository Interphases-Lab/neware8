"""Decoding history records: step-boundary times, totals and step counts."""

import struct

import pytest

from neware8 import StorageError
from neware8.history import RunningTotals, decode, is_main, recorded_point
from neware8.messages import HistoryRecord
from helpers import batch, history_payload

MAH = 36_000_000  # one mAh in raw counter units at the captured record's current range


def record(sequence, total, *, step=1, elapsed=None, cycle=1, charge=0, discharge=0):
    payload = bytearray(
        history_payload(
            sequence=sequence,
            time_ms=total,
            step_id=step,
            step_change_count=step,
            step_time_ms=total if elapsed is None else elapsed,
            cycle=cycle,
        )
    )
    struct.pack_into("<qqqq", payload, 5 + 43, charge, discharge, charge * 4, discharge * 4)
    return batch(bytes(payload)).records[0]


def times(records, **options):
    return [point.test_time_second for point in decode(records, charge_range=None, **options)]


def test_records_scale_to_engineering_units():
    raw = bytearray(record(1, 1500).raw)
    struct.pack_into("<i", raw, 31, 36000)  # 0.1 mV
    point = recorded_point(HistoryRecord.from_bytes(bytes(raw)), None)
    assert (point.sequence, point.test_time_second, point.voltage_volt) == (1, 1.5, 3.6)
    assert point.current_ampere == pytest.approx(123 / 10_000_000)
    assert is_main(record(1, 0))


def test_boundary_time_is_anchored_to_step_clock_and_raw_is_preserved():
    records = [
        record(1, 10000),
        record(2, 0, step=2, elapsed=0),
        record(3, 15000, step=2, elapsed=4000),
    ]
    points = list(decode(records, charge_range=None))
    assert [p.test_time_second for p in points] == [10, 11, 15]
    assert points[1].raw == records[1].raw
    assert times(records[1:], previous=records[0]) == [11, 15]


def test_pending_boundary_is_withheld_until_its_following_record_is_known():
    first, boundary = record(1, 10000), record(2, 0, step=2, elapsed=0)
    assert times([first, boundary]) == [10]
    assert times([boundary, record(3, 12000, step=2, elapsed=1000)], previous=first) == [11, 12]


@pytest.mark.parametrize("problem", ["gap", "preceding_gap"])
def test_missing_neighbours_withhold_the_boundary(problem):
    records = [record(2, 0, step=2, elapsed=0), record(4 if problem == "gap" else 3, 12000, step=2)]
    if problem == "gap":
        records.insert(0, record(1, 10000))
    assert times(records) == ([10] if problem == "gap" else [])


@pytest.mark.parametrize("problem", ["different_step", "clock", "nonzero", "same_step"])
def test_inconsistent_times_are_not_interpolated(problem):
    records = [
        record(1, 10000),
        record(
            2,
            1000 if problem == "nonzero" else 0,
            step=1 if problem == "same_step" else 2,
            elapsed=0,
        ),
        record(
            3,
            12000,
            step=3 if problem == "different_step" else 2,
            elapsed=4000 if problem == "clock" else 1000,
        ),
    ]
    with pytest.raises(StorageError):
        times(records)


def test_time_zero_at_test_start_is_not_modified():
    assert times([record(1, 0), record(2, 1000)]) == [0, 1]


def test_totals_and_step_count_are_resumable():
    records = [
        record(1, 1000, step=1),
        record(2, 2000, step=1, charge=MAH),
        record(3, 3000, step=2),
        record(4, 4000, step=2, discharge=2 * MAH),
    ]
    expected = [
        (0.0, 0.0, 1),
        (0.001, 0.0, 1),
        (0.001, 0.0, 2),
        (0.001, 0.002, 2),
    ]

    def summary(points):
        return [(p.charging_capacity_ah, p.discharging_capacity_ah, p.step_count) for p in points]

    whole = list(decode(records, charge_range=None, totals=RunningTotals(None)))
    assert summary(whole) == pytest.approx(expected)
    totals = RunningTotals(None)
    first = list(decode(records[:2], charge_range=None, totals=totals))
    rest = list(decode(records[2:], charge_range=None, previous=records[1], totals=totals))
    assert summary(first + rest) == pytest.approx(expected)
    assert whole[-1].charging_energy_wh == pytest.approx(0.004)


def test_totals_after_a_gap_are_unknown_but_steps_keep_counting():
    totals = RunningTotals(None)
    records = [record(1, 1000, charge=MAH), record(3, 3000, step=2, charge=MAH)]
    points = list(decode(records, charge_range=None, totals=totals))
    assert points[-1].charging_capacity_ah is None
    assert points[-1].step_count == 2


def test_undecodable_current_leaves_totals_unknown():
    raw = bytearray(record(1, 1000).raw)
    struct.pack_into("<i", raw, 82, 0)  # zero current range and no charge range
    unscaled = HistoryRecord.from_bytes(bytes(raw))
    (point,) = decode([unscaled], charge_range=None, totals=RunningTotals(None))
    assert point.current_ampere is None
    assert point.charging_capacity_ah is None
