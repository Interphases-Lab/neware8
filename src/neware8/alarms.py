"""Descriptions of controller alarm (protection) codes, from the manufacturer's LogCode.csv."""

import json
import math
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    catalog: dict[str, Any] = json.loads(
        (Path(__file__).parent / "data/bts82_alarm_catalog.json").read_text(encoding="utf-8")
    )
    if catalog["schema_version"] != 1:
        raise ValueError("unsupported alarm catalog schema")
    return catalog


@dataclass(frozen=True, slots=True)
class AlarmInfo:
    code: int
    known: bool
    message: str


def alarm_info(code: int) -> AlarmInfo:
    """Describe an alarm without inferring severity from undocumented catalogue fields."""
    if not 0 <= code <= 0xFFFFFFFF:
        raise ValueError("alarm codes are uint32")
    if code == 0:
        return AlarmInfo(0, True, "No reported protection")
    row = _catalog()["codes"].get(str(code))
    if row is None:
        return AlarmInfo(code, False, "Unknown manufacturer code")
    return AlarmInfo(
        code, True, row["event_en"] or f"Controller alarm {code} (no description available)"
    )


# Alarms whose details occupy the record's energy slots (DevLogItem.Text in the client).
PRESSURE_ALARMS = {207, 208}
THRESHOLD_ALARMS = {693, 694, 695, 696, 712, 713}
OTHER_CHANNEL_ALARMS = {250, 251, 252, 533, 558, 597}


def overloaded_energy(code: int) -> frozenset[str]:
    """Energy quantities that carry alarm details instead of energy for this alarm code."""
    if code in PRESSURE_ALARMS | THRESHOLD_ALARMS:
        return frozenset({"charge_energy", "discharge_energy"})
    if code in OTHER_CHANNEL_ALARMS:
        return frozenset({"discharge_energy"})
    return frozenset()


def alarm_details(
    code: int, charge_energy_raw: int, discharge_energy_raw: int
) -> dict[str, float | int | None] | None:
    """Decode the details some alarms pack into the energy slots; None for other alarms."""

    def i32(value: int) -> int:
        return int.from_bytes((value & 0xFFFFFFFF).to_bytes(4, "little"), "little", signed=True)

    if code in PRESSURE_ALARMS:
        return {
            "start_pressure_kpa": i32(charge_energy_raw >> 32) / 10000,
            "end_pressure_kpa": i32(charge_energy_raw) / 10000,
            "leakage_rate_kpa_per_min": discharge_energy_raw / 10000,
        }
    if code in OTHER_CHANNEL_ALARMS:
        details: dict[str, float | int | None] = {
            "other_unit": discharge_energy_raw >> 8 & 0xFF,
            "other_channel": discharge_energy_raw & 0xFF,
        }
        if code == 252:
            details["temperature_unit"] = discharge_energy_raw >> 24 & 0xFF
            details["temperature_channel"] = discharge_energy_raw >> 16 & 0xFF
        return details
    if code in THRESHOLD_ALARMS:
        unit = "mv" if code in (695, 696) else "c"
        details = {
            "seconds_since_1970": charge_energy_raw >> 32,
            "channel": discharge_energy_raw & 0xFFFFFFFF,
        }
        for name, bits in (
            ("sample", charge_energy_raw & 0xFFFFFFFF),
            ("threshold", discharge_energy_raw >> 32),
        ):
            value = struct.unpack("<f", struct.pack("<I", bits))[0]
            details[f"{name}_{unit}"] = value / 10 if math.isfinite(value) else None
        return details
    return None
