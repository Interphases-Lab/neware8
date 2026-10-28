"""Standard BDF columns and conservative elapsed-time reconstruction."""

from dataclasses import replace

import pyarrow as pa

from .errors import StorageError
from .messages import HistoryRecord
from .models import RecordedPoint, StepType
from ._counters import COLUMNS


SCHEMA = pa.schema(
    [
        ("test_time_second", pa.float64()),
        ("voltage_volt", pa.float64()),
        ("current_ampere", pa.float64()),
        ("cycle_count", pa.int64()),
        ("step_id", pa.int64()),
        ("record_index", pa.uint32()),
        ("step_time_second", pa.float64()),
        ("step_type", pa.string()),
        ("step_record_index", pa.int64()),
        ("power_watt", pa.float64()),
        *((name, pa.float64()) for name in COLUMNS),
    ]
)


class PendingRecordTime(StorageError):
    """A step-boundary record needs adjacent history before its time can be resolved."""


def occurrence(record: HistoryRecord) -> tuple[int, int, int]:
    return (record.cycle, record.step_id, record.step_change_count)


def correct_time(
    point: RecordedPoint,
    record: HistoryRecord,
    previous: HistoryRecord | None,
    following: HistoryRecord | None,
) -> RecordedPoint:
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


def row(point: RecordedPoint, record: HistoryRecord, step_record_index: int) -> dict[str, object]:
    if point.current_ampere is None:
        raise StorageError("current scaling is unresolved; cannot export required BDF current")
    try:
        step_type = StepType(record.step_type).name
    except ValueError:
        step_type = None
    return {
        "test_time_second": point.test_time_second,
        "voltage_volt": point.voltage_volt,
        "current_ampere": point.current_ampere,
        "cycle_count": point.cycle,
        "step_id": point.step_id,
        "record_index": point.sequence,
        "step_time_second": record.step_time_ms / 1000,
        "step_type": step_type,
        "step_record_index": step_record_index,
        "power_watt": point.voltage_volt * point.current_ampere,
    }
