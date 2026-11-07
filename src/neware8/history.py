"""
History records: what the application must store, and how to decode them.

neware8 is the driver; the application owns the measurement data. The client hands every
history batch to the application's `HistorySink` and acknowledges it to the controller only
after `commit` has returned, because the controller treats an acknowledged batch as delivered.
After every connect it asks the sink for the saved position and has the controller resend from
there, since batches in flight when a connection drops are otherwise skipped (observed).

The decoding helpers are pure and encode device behaviour the application should not have to
rediscover:

- `recorded_point` scales one record into volts, amperes and seconds.
- `decode` turns a run's records, in sequence order, into points, reconstructing the time of
  step-boundary records that report zero total and step time, and withholding such a record
  until its following record is known.
- `RunningTotals` accumulates the device's per-step capacity and energy counters into
  whole-test totals, and `CounterTotals` into step, cycle and test totals.

See docs/history.md.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Protocol

from ._counters import TEST_TOTALS, CounterTotals, RunningTotals
from .errors import StorageError
from .measurements import current_scale
from .messages import HistoryBatch, HistoryRecord
from .models import ChannelAddress, RecordedPoint


@dataclass(frozen=True, slots=True)
class StartedTest:
    """A test the controller assigned a TestID to in a start this client sent.

    Its history records carry `(address.unit, address.channel, test_id)`.
    """

    address: ChannelAddress
    test_id: int
    physical_id: int
    charge_range_raw: int | None
    started_at_ns: int  # host time of the Start reply


class HistorySink(Protocol):
    """Where the client delivers history; implemented by the application.

    Every method may raise to refuse. A refused `bind` or `commit` ends the controller
    session (the batch is not acknowledged and is sent again after the reconnect); a refused
    `register` fails the start, whose outcome is then uncertain.
    """

    async def bind(self, controller: bytes) -> None:
        """Accept the controller's 16-byte GUID; called after every connect, before history."""
        ...

    async def register(self, tests: tuple[StartedTest, ...]) -> None:
        """Record newly assigned TestIDs durably; called before any of their history arrives."""
        ...

    async def commit(self, batch: HistoryBatch) -> None:
        """Store the records the application keeps and `batch.token` as the resend position.

        Both must be durable when this returns: the client acknowledges the batch next. The
        controller resends batches, so records already stored with identical bytes must be
        accepted again; the same sequence with different bytes is a conflict.
        """
        ...

    async def checkpoint(self) -> int | None:
        """The last committed `batch.token`, or None if no history was ever committed."""
        ...


class PendingRecordTime(StorageError):
    """A step-boundary record needs adjacent history before its time can be resolved."""


def occurrence(record: HistoryRecord) -> tuple[int, int, int]:
    """Identify one execution of a program step: cycle, step ID and the step-change counter."""
    return (record.cycle, record.step_id, record.step_change_count)


def is_main(record: HistoryRecord) -> bool:
    """Whether a record is a sample of a main channel, as opposed to auxiliary or event data."""
    return record.data_flag == HistoryRecord.MAIN and not record.aux


def recorded_point(record: HistoryRecord, charge_range: int | None) -> RecordedPoint:
    """Scale one main record. `charge_range` resolves records with a zero current range."""
    range_raw = record.range_raw or charge_range
    current = None
    if range_raw is not None:
        try:
            current = record.current_raw / (current_scale(range_raw) * 1000)
        except ValueError:
            pass
    return RecordedPoint(
        record.sequence,
        record.total_time_ms / 1000,
        record.voltage_raw / 10000,
        current,
        record.cycle,
        record.step_id,
        record.raw,
    )


def correct_time(
    point: RecordedPoint,
    record: HistoryRecord,
    previous: HistoryRecord | None,
    following: HistoryRecord | None,
) -> RecordedPoint:
    """Reconstruct the time of an isolated step-boundary record that reports zero time.

    The following record's total time minus its step time is the new step's start. A genuine
    time zero at sequence 1 is kept.

    Raises:
      PendingRecordTime: If the preceding or following record is not known yet.
      StorageError: If the surrounding records cannot establish the time.
    """
    if record.total_time_ms != 0 or record.step_time_ms != 0 or point.sequence == 1:
        return point
    if previous is None or previous.sequence + 1 != point.sequence:
        raise PendingRecordTime(f"Missing preceding record for zero time at {point.sequence}")
    if occurrence(previous) == occurrence(record):
        return point
    if following is None or following.sequence != point.sequence + 1:
        raise PendingRecordTime(f"Missing following record for step boundary at {point.sequence}")
    if occurrence(following) != occurrence(record) or following.step_time_ms <= 0:
        raise StorageError(f"Cannot resolve step-boundary time at {point.sequence}")
    offset = following.total_time_ms - following.step_time_ms
    if not previous.total_time_ms <= offset <= following.total_time_ms:
        raise StorageError(f"Inconsistent clocks around step boundary at {point.sequence}")
    return replace(point, test_time_second=offset / 1000)


def decode(
    records: Iterable[HistoryRecord],
    *,
    charge_range: int | None,
    previous: HistoryRecord | None = None,
    totals: RunningTotals | None = None,
) -> Iterator[RecordedPoint]:
    """Decode a run's main records, in ascending sequence order, into points.

    Args:
      records: Consecutive stored records of one test, from some sequence on.
      charge_range: The channel's charge range for records with a zero current range.
      previous: The stored record just before the first one, if any; needed to resolve a
        boundary record at the start of `records`.
      totals: Totals positioned just before the first record. They are advanced for every
        point yielded and fill its totals and step count; without them those stay unset.

    Yields:
      The points that can be resolved. Decoding stops before a boundary record whose
      neighbours are not known yet; continue from the last yielded sequence once more history
      has arrived.

    Raises:
      StorageError: If times regress or a boundary cannot be resolved.
    """
    iterator = iter(records)
    current = next(iterator, None)
    while current is not None:
        following = next(iterator, None)
        point = recorded_point(current, charge_range)
        try:
            point = correct_time(point, current, previous, following)
        except PendingRecordTime:
            return
        if previous is not None and point.test_time_second < previous.total_time_ms / 1000:
            raise StorageError(f"Unresolved time regression at sequence {point.sequence}")
        if totals is not None:
            values, step_count = totals.update(current)
            point = replace(
                point,
                charging_capacity_ah=values["charging_capacity_ah"],
                discharging_capacity_ah=values["discharging_capacity_ah"],
                charging_energy_wh=values["charging_energy_wh"],
                discharging_energy_wh=values["discharging_energy_wh"],
                step_count=step_count,
            )
        yield point
        previous = replace(current, total_time_ms=round(point.test_time_second * 1000))
        current = following


__all__ = [
    "TEST_TOTALS",
    "CounterTotals",
    "HistorySink",
    "PendingRecordTime",
    "RunningTotals",
    "StartedTest",
    "correct_time",
    "decode",
    "is_main",
    "occurrence",
    "recorded_point",
]
