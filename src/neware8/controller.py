"""Controller and lower-unit information read from the device."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ControllerInfo:
    """From GetZwjSn (0x002A), GetVerZwj (0x56) and GetRtcStatus (0x0024)."""

    identity: bytes  # controller GUID; storage is bound to it
    serial_number: str
    production_time: datetime | None
    version: str
    rtc_status_raw: int  # nonzero asks the host to set the clock; bits unknown
    raw_identity: bytes  # complete 52-byte serial reply
    raw_version: bytes

    @property
    def clock_setting_requested(self) -> bool:
        return self.rtc_status_raw != 0


@dataclass(frozen=True)
class UnitInfo:
    """From GetXwjSn (0x002B) and GetVerXwj (0x55) for one lower unit."""

    unit: int
    identity: bytes  # unit GUID
    serial_number: str
    production_time: datetime | None
    valid_until: datetime | None
    latest_calibration: datetime | None
    version: str
    raw_identity: bytes  # complete 92-byte serial reply
    raw_version: bytes
