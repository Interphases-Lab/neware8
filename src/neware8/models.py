"""Immutable addresses, channel observations and measurement identities."""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
import math
from pathlib import Path
import time


def bounded_integer(name: str, value: int, maximum: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")


class ChannelState(StrEnum):
    FINISHED = "finish"
    RUNNING = "running"
    STOPPED = "stop"
    PROTECTED = "protect"
    PAUSED = "pause"
    OFFLINE_PAUSED = "offline_pause"
    FINISHED_PROTECTED = "finish_protect"
    BEEP = "beep"
    SYNCHRONOUS_CONTROL = "synchronous_control"
    LIGHT = "light"
    VACUUM = "vacuum"
    RELEASE = "release"
    LEAKAGE_RATE = "leakage_rate"
    JAM = "jam"
    WAIT_TIMEOUT = "wait_timeout"
    RESERVED_PAUSE = "reserved_pause"
    WAIT_START = "wait_start"
    UNKNOWN = "unknown"


class StepType(IntEnum):
    UNDEFINED = 0
    CC_CHARGE = 1
    CC_DISCHARGE = 2
    CV_CHARGE = 3
    REST = 4
    CYCLE = 5
    END = 6
    CCCV_CHARGE = 7
    CP_DISCHARGE = 8
    CP_CHARGE = 9
    CR_DISCHARGE = 10
    CR_CHARGE = 11
    INTERNAL_RESISTANCE = 12
    PAUSE = 13
    PULSE = 16
    SIMULATION = 17
    PCCCV_CHARGE = 18
    CV_DISCHARGE = 19
    CCCV_DISCHARGE = 20
    CONTROL = 21
    OPEN_CIRCUIT_VOLTAGE = 22
    SUBSTEP = 23
    FOLLOW = 24
    SLOPE = 25
    CPCV_DISCHARGE = 26
    CPCV_CHARGE = 27
    CRCV_DISCHARGE = 28
    CRCV_CHARGE = 29


class RunState(StrEnum):
    RECORDING = "recording"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"


class DataCompleteness(StrEnum):
    UNKNOWN = "unknown"
    GAPS = "gaps"


class Control(StrEnum):
    STOP = "stop"
    CONTINUE = "continue"
    CLEAR_FLAG = "clear_flag"
    JUMP = "jump"


@dataclass(frozen=True, slots=True, order=True)
class ChannelAddress:
    unit: int
    channel: int

    def __post_init__(self) -> None:
        bounded_integer("unit", self.unit, 32, 1)
        bounded_integer("channel", self.channel, 8, 1)


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    address: ChannelAddress
    physical_id: int
    voltage_rating_v: float
    current_rating_a: float
    current_scale: int
    charge_range_raw: int | None = None

    def __post_init__(self) -> None:
        bounded_integer("physical_id", self.physical_id, 255)
        bounded_integer("current_scale", self.current_scale, 100_000_000, 1)
        for name in ("voltage_rating_v", "current_rating_a"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.charge_range_raw is not None:
            bounded_integer("charge_range_raw", self.charge_range_raw, 2**31 - 1, -2_000_000_000)
            if self.charge_range_raw == 0:
                raise ValueError("charge_range_raw must be nonzero")


@dataclass(frozen=True, slots=True)
class ChannelStatus:
    address: ChannelAddress
    test_id: int
    state: ChannelState
    state_raw: int
    step_type_raw: int
    step_id: int
    cycle: int
    alarm_code: int
    voltage_v: float
    current_a: float | None
    temperature_c: float
    elapsed_s: float
    step_elapsed_s: float
    received_at_ns: int
    received_monotonic: float
    raw: bytes

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.received_monotonic

    @property
    def step_type(self) -> StepType | None:
        try:
            return StepType(self.step_type_raw)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """An immutable, revisioned view of all latest main-channel observations."""

    revision: int
    created_at_ns: int
    connected: bool
    channels: tuple[ChannelStatus, ...]

    def get(self, address: ChannelAddress) -> ChannelStatus | None:
        return next((status for status in self.channels if status.address == address), None)


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    address: ChannelAddress
    test_id: int
    state: RunState
    physical_id: int
    started_at_ns: int
    record_count: int
    completeness: DataCompleteness


@dataclass(frozen=True, slots=True)
class Export:
    path: Path
    run: Run
    record_count: int
    missing_sequence_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class RecordedPoint:
    sequence: int
    test_time_second: float
    voltage_volt: float
    current_ampere: float | None
    cycle: int
    step_id: int
    raw: bytes


@dataclass(frozen=True, slots=True)
class RecordChange:
    run_id: str
    first_sequence: int
    last_sequence: int
    backfill: bool


@dataclass(frozen=True, slots=True)
class RecordRevision:
    revision: int
    reset_revision: int
