"""
Engineering values from status and history records: current scaling, channel state and units.

Voltage is in 0.1 mV, time in ms and temperature in 0.1 degC. Current, capacity and energy are
scaled by the record's current range (MainFrame.GetCurrRange in the manufacturer client); a
record with range 0 needs the channel's charge range from the metadata or the profile.
"""

import struct
from dataclasses import dataclass

from .messages import StatusRecord
from .models import ChannelState


def _float32(value: float) -> float:
    """Round to single precision, as the manufacturer's float arithmetic does."""
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def signed32(raw: int) -> int:
    return raw - (1 << 32) if raw & 1 << 31 else raw


def current_scale(range_raw: int) -> int:
    """Divisor that turns raw current into mA, from the signed range encoding.

    Negative ranges are in uA-class units, positive ones in A-class units; the thresholds are
    those of the manufacturer's GetCurrScale.
    """
    value = _float32(signed32(range_raw & 0xFFFFFFFF))
    if value == 0:
        raise ValueError("range 0: the channel's charge range is needed to scale current")
    if not -2_000_000_000 <= value < 2_147_483_648:
        raise ValueError(f"unsupported current range encoding {range_raw}")
    magnitude = abs(int(value))
    thresholds: tuple[tuple[int, int], ...]
    if value < 0:
        thresholds = (
            (10, 100000),
            (100, 10000),
            (1000, 1000),
            (1000000, 100),
            (10000000, 100000000),
            (100000000, 10000000),
        )
        default = 1000000
    else:
        thresholds = ((10, 10000), (100, 1000), (1000, 100))
        default = 10
    return next((scale for limit, scale in thresholds if magnitude < limit), default)


# Work-type bits: base state in bits 4..5, extended state in bits 8+ (0 and 6 mean "base").
BASE_STATES = {
    0x00: ChannelState.FINISHED,
    0x10: ChannelState.RUNNING,
    0x20: ChannelState.STOPPED,
    0x30: ChannelState.PROTECTED,
}
EXTENDED_STATES = {
    1: ChannelState.PAUSED,
    2: ChannelState.FINISHED,
    3: ChannelState.OFFLINE_PAUSED,
    4: ChannelState.BEEP,
    5: ChannelState.SYNCHRONOUS_CONTROL,
    7: ChannelState.LIGHT,
    8: ChannelState.VACUUM,
    9: ChannelState.RELEASE,
    10: ChannelState.LEAKAGE_RATE,
    11: ChannelState.JAM,
    12: ChannelState.WAIT_TIMEOUT,
    13: ChannelState.RESERVED_PAUSE,
    14: ChannelState.WAIT_START,
    15: ChannelState.FINISHED_PROTECTED,
}


def channel_state(work_type: int) -> ChannelState:
    extended = work_type >> 8
    if extended in (0, 6):
        return BASE_STATES[work_type & 0x30]
    return EXTENDED_STATES.get(extended, ChannelState.UNKNOWN)


@dataclass(frozen=True)
class Readings:
    state: ChannelState
    voltage_v: float
    current_a: float | None  # None when the range is unknown
    temperature_c: float
    step_elapsed_s: float
    total_elapsed_s: float
    charge_capacity_ah: float | None
    discharge_capacity_ah: float | None
    charge_energy_wh: float | None  # None also when an alarm reuses the energy slots
    discharge_energy_wh: float | None


def _signed64(raw: int) -> int:
    return raw - (1 << 64) if raw & 1 << 63 else raw


def readings(
    record: StatusRecord,
    charge_range: int | None = None,
    energy_overloaded: frozenset[str] = frozenset(),
) -> Readings:
    range_raw = record.range_raw
    if range_raw == 0 and charge_range is not None:
        range_raw = charge_range & 0xFFFFFFFF
    try:
        scale: int | None = current_scale(range_raw)
    except ValueError:
        scale = None

    def scaled(raw: int, divisor: int, overloaded: bool = False) -> float | None:
        return None if scale is None or overloaded else _signed64(raw) / (scale * divisor)

    return Readings(
        state=channel_state(record.work_type),
        voltage_v=record.voltage_raw / 10000,
        current_a=None if scale is None else record.current_raw / (scale * 1000),
        temperature_c=record.temperature_raw / 10,
        step_elapsed_s=record.step_time_ms / 1000,
        total_elapsed_s=record.total_time_ms / 1000,
        charge_capacity_ah=scaled(record.charge_capacity_raw, 3_600_000),
        discharge_capacity_ah=scaled(record.discharge_capacity_raw, 3_600_000),
        charge_energy_wh=scaled(
            record.charge_energy_raw, 3_600_000, "charge_energy" in energy_overloaded
        ),
        discharge_energy_wh=scaled(
            record.discharge_energy_raw, 3_600_000, "discharge_energy" in energy_overloaded
        ),
    )
