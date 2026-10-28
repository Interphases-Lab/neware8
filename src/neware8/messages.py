"""
Payload layouts of the BTS82 native commands. No I/O; frames are built in protocol.py.

Each class decodes with `from_payload(bytes)` and, where the host sends it, encodes with
`to_payload()`. Field names follow the manufacturer's PDB/struct names where those are known.
Unknown bytes are kept in `raw`/`reserved` fields rather than dropped.
"""

import struct
import zlib
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import IntEnum
from typing import TypeVar

from .protocol import ProtocolError, channel_mask, mask_channels

T = TypeVar("T")
MAX_FRAGMENT = 4096  # CPacket_SetStep82 buffer
FRAGMENT_TARGET = 1000  # whole step records per download frame, as the manufacturer client


def _need(payload: bytes, size: int, what: str) -> None:
    if len(payload) != size:
        raise ProtocolError(f"{what}: expected {size} bytes, got {len(payload)}")


def _need_at_least(payload: bytes, size: int, what: str) -> None:
    if len(payload) < size:
        raise ProtocolError(f"{what}: expected at least {size} bytes, got {len(payload)}")


def _unpack(cls: type[T], layout: struct.Struct, raw: bytes) -> T:
    """Fill a record dataclass from a struct whose items are its fields in order, plus `raw`."""
    names = [f.name for f in fields(cls) if f.name != "raw"]  # type: ignore[arg-type]
    values = dict(zip(names, layout.unpack(raw), strict=True), raw=raw)
    return cls(**values)


def _cstring(raw: bytes) -> str:
    """NUL-terminated text; the manufacturer client writes it with the GBK code page."""
    return raw.split(b"\0", 1)[0].decode("gbk", "replace")


# --------------------------------------------------------------------------- #
# Session: connect and channel metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Connect:
    """Short 0x00, controller -> host. The host replies 0x80 with the controller unit byte."""

    client_type: int  # 24 on BTS82 controllers
    version: int

    @classmethod
    def from_payload(cls, payload: bytes) -> "Connect":
        _need(payload, 3, "Connect")
        return cls(*struct.unpack("<BH", payload))


@dataclass(frozen=True)
class InfoFragment:
    """Short 0x72 (SendInfo70), controller -> host; ACKed with unit 0. Bodies concatenate."""

    version: int
    copyright: bytes  # b"Neware"
    format: int  # 7 on BTS82
    final: bool  # end flag: 1 more parts follow, 2 last part
    part: int  # 1-based frame index
    body: bytes

    @classmethod
    def from_payload(cls, payload: bytes) -> "InfoFragment":
        _need_at_least(payload, 11, "SendInfo header")
        version, copyright, fmt, end_flag, part = struct.unpack_from("<H6sBBB", payload)
        if end_flag not in (1, 2) or part == 0:
            raise ProtocolError(f"SendInfo end flag {end_flag} / part {part}")
        return cls(version, copyright, fmt, end_flag == 2, part, payload[11:])


@dataclass(frozen=True)
class RangeBlock:
    """One 627-byte DeviceRange (format 7): a group of channels sharing type and ranges."""

    SIZE = 627

    channels: tuple[tuple[int, int], ...]  # (unit, channel), 1-based
    channel_type: int  # 1 main; other values are auxiliary sensor types
    charge_range: int  # signed range encoding, see measurements.py
    discharge_range: int
    voltage_range: int
    temperature_range: int
    resistance_range: int
    current_ranges: tuple[int, ...]  # selectable current ranges, signed range encodings
    frequency_type: int
    unit_id: int  # m_dwXwjID
    controller_guid: bytes
    unit_guids: tuple[bytes, ...]  # one per unit slot 1..32
    raw: bytes = field(repr=False)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "RangeBlock":
        _need(raw, cls.SIZE, "DeviceRange")
        masks = raw[:32]
        charge, discharge, volt, temp, res, count = struct.unpack_from("<iiIIIB", raw, 33)
        if count > 10:
            raise ProtocolError(f"DeviceRange lists {count} current ranges (max 10)")
        ranges = struct.unpack_from("<10i", raw, 54)[:count]
        frequency, unit_id = struct.unpack_from("<BI", raw, 94)
        return cls(
            channels=tuple(
                (unit + 1, bit + 1)
                for unit, mask in enumerate(masks)
                for bit in range(8)
                if mask >> bit & 1
            ),
            channel_type=raw[32],
            charge_range=charge,
            discharge_range=discharge,
            voltage_range=volt,
            temperature_range=temp,
            resistance_range=res,
            current_ranges=ranges,
            frequency_type=frequency,
            unit_id=unit_id,
            controller_guid=raw[99:115],
            unit_guids=tuple(raw[115 + 16 * i : 131 + 16 * i] for i in range(32)),
            raw=raw,
        )


@dataclass(frozen=True)
class DeviceInfo:
    """The assembled SendInfo body: device type, block count and range blocks."""

    device_type: int
    blocks: tuple[RangeBlock, ...]

    MAIN = 1

    @classmethod
    def from_body(cls, body: bytes) -> "DeviceInfo":
        _need_at_least(body, 3, "SendInfo body")
        device_type, count = struct.unpack_from("<BH", body)
        _need(body, 3 + count * RangeBlock.SIZE, "SendInfo blocks")
        return cls(
            device_type,
            tuple(
                RangeBlock.from_bytes(body[3 + i * RangeBlock.SIZE : 3 + (i + 1) * RangeBlock.SIZE])
                for i in range(count)
            ),
        )

    def main_channels(self) -> dict[tuple[int, int], RangeBlock]:
        found: dict[tuple[int, int], RangeBlock] = {}
        for block in self.blocks:
            if block.channel_type != self.MAIN:
                continue
            for channel in block.channels:
                if channel in found:
                    raise ProtocolError(f"channel {channel} listed in two range blocks")
                found[channel] = block
        return found


class InfoAssembler:
    """Collects SendInfo parts. Parts must be consecutive and share one header."""

    MAX_BODY = 1 << 20

    def __init__(self) -> None:
        self._header: tuple[int, bytes, int] | None = None
        self._parts: list[bytes] = []

    def add(self, fragment: InfoFragment) -> DeviceInfo | None:
        header = fragment.version, fragment.copyright, fragment.format
        if fragment.part == 1:
            self._header, self._parts = header, []
        elif header != self._header or fragment.part != len(self._parts) + 1:
            self._header, self._parts = None, []
            raise ProtocolError(f"SendInfo part {fragment.part} out of sequence")
        if fragment.format != 7:
            raise ProtocolError(f"unsupported SendInfo format {fragment.format}")
        self._parts.append(fragment.body)
        if sum(map(len, self._parts)) > self.MAX_BODY:
            raise ProtocolError("SendInfo body exceeds 1 MiB")
        if not fragment.final:
            return None
        body, self._header, self._parts = b"".join(self._parts), None, []
        return DeviceInfo.from_body(body)


# --------------------------------------------------------------------------- #
# Status and history uploads
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StatusRecord:
    """One 100-byte RTDATA record of the live status upload (extended 0x0039)."""

    SIZE = 100
    FORMAT = struct.Struct("<BBBIBBIQiiHhQQQQIIQIIIHIB")

    data_flag: int
    channel: int
    aux: int  # 0 for the main channel, else auxiliary sensor ID
    cycle: int
    step_id: int
    step_type: int
    work_type: int  # see measurements.channel_state
    step_time_ms: int
    voltage_raw: int  # 0.1 mV
    current_raw: int  # scaled by the current range, see measurements.py
    resistance_raw: int
    temperature_raw: int  # 0.1 degC
    charge_capacity_raw: int
    discharge_capacity_raw: int
    charge_energy_raw: int
    discharge_energy_raw: int
    range_raw: int  # dwCurStepRange
    alarm_code: int  # dwPrtCode
    total_time_ms: int
    sequence: int  # dwSeqID
    history_sequence: int  # dwDfSeqIDCurr
    test_id: int
    reserve_pause_step: int  # 0xFFFF: none
    reserve_pause_cycle: int  # 0xFFFFFFFF: none
    reserve_pause_type: int
    raw: bytes = field(repr=False)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "StatusRecord":
        _need(raw, cls.SIZE, "RTDATA")
        return _unpack(cls, cls.FORMAT, raw)


@dataclass(frozen=True)
class StatusUpload:
    """Extended 0x0039, controller -> host, not acknowledged."""

    data_pointer: int  # m_dwDataPointer; ignored by the host, 1 in captures
    records: tuple[StatusRecord, ...]

    @classmethod
    def from_payload(cls, payload: bytes) -> "StatusUpload":
        _need_at_least(payload, 5, "RT upload")
        pointer, count = struct.unpack_from("<IB", payload)
        _need(payload, 5 + count * StatusRecord.SIZE, "RT upload records")
        return cls(
            pointer,
            tuple(
                StatusRecord.from_bytes(payload[5 + i * 100 : 105 + i * 100]) for i in range(count)
            ),
        )


class RecordKind(IntEnum):
    """btDataFlag of history records (observed in captures; meaning from content and host use)."""

    SAMPLE = 0x55  # 'U' a recorded sample
    STEP_END = 0x58  # 'X' last values of a step
    STEP_START = 0x59  # 'Y' first values of a step
    TEST_END = 0x6A  # 'j'
    EVENT = 0xAA  # log event: the resistance field holds a LogCode (alarms.alarm_info)


@dataclass(frozen=True)
class HistoryRecord:
    """One 94-byte DFDATA record of a history batch (extended 0x0015)."""

    SIZE = 94
    MAIN = RecordKind.SAMPLE
    FORMAT = struct.Struct("<BBBBIIIBBBIQiiHhqqqqHBBBBBiQ")

    data_flag: int
    unit: int
    channel: int
    aux: int
    test_id: int
    sequence: int  # dwTestSn: per-test record counter
    cycle: int
    step_id: int
    step_type: int
    step_change_count: int
    work_type: int
    step_time_ms: int
    voltage_raw: int
    current_raw: int
    resistance_raw: int
    temperature_raw: int
    charge_capacity_raw: int
    discharge_capacity_raw: int
    charge_energy_raw: int
    discharge_energy_raw: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    range_raw: int
    total_time_ms: int
    raw: bytes = field(repr=False)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "HistoryRecord":
        _need(raw, cls.SIZE, "DFDATA")
        return _unpack(cls, cls.FORMAT, raw)

    @property
    def timestamp(self) -> datetime | None:
        try:
            return datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)
        except ValueError:
            return None

    @property
    def event_code(self) -> int | None:
        """LogCode of an EVENT record, e.g. 8 Start, 434 time-limit jump, 1 Finish."""
        return self.resistance_raw if self.data_flag == RecordKind.EVENT else None


@dataclass(frozen=True)
class HistoryBatch:
    """Extended 0x0015. ACK 0x8015 (unit 0) echoes `token`; persist records before ACKing."""

    token: int  # position in the controller's history stream
    records: tuple[HistoryRecord, ...]

    @classmethod
    def from_payload(cls, payload: bytes) -> "HistoryBatch":
        _need_at_least(payload, 5, "history batch")
        count, token = struct.unpack_from("<BI", payload)
        _need(payload, 5 + count * HistoryRecord.SIZE, "history records")
        return cls(
            token,
            tuple(
                HistoryRecord.from_bytes(payload[5 + i * 94 : 99 + i * 94]) for i in range(count)
            ),
        )

    @staticmethod
    def ack_payload(token: int) -> bytes:
        return struct.pack("<I", token)


# --------------------------------------------------------------------------- #
# Starting a test: map, aux init, download, protections, start, confirm
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Target:
    """A unit and a 1-based channel bit mask."""

    unit: int
    mask: int

    @property
    def channels(self) -> tuple[int, ...]:
        return mask_channels(self.mask)

    @classmethod
    def of(cls, unit: int, channels: "tuple[int, ...] | list[int]") -> "Target":
        return cls(unit, channel_mask(channels))


def _targets(targets: tuple[Target, ...]) -> bytes:
    return bytes([len(targets)]) + b"".join(struct.pack("<BI", t.unit, t.mask) for t in targets)


def _read_targets(payload: bytes, offset: int) -> tuple[tuple[Target, ...], int]:
    _need_at_least(payload, offset + 1, "target count")
    count = payload[offset]
    end = offset + 1 + 5 * count
    _need_at_least(payload, end, "targets")
    return tuple(
        Target(*struct.unpack_from("<BI", payload, offset + 1 + 5 * i)) for i in range(count)
    ), end


@dataclass(frozen=True)
class MapEntry:
    unit: int
    channel: int
    aux: int  # 0 for the main channel
    physical: int  # controller-wide physical channel


@dataclass(frozen=True)
class ChannelMap:
    """Short 0x73 (SetChlMap70). Reply 0xF3 echoes the GUID."""

    guid: bytes
    entries: tuple[MapEntry, ...]

    def to_payload(self) -> bytes:
        return (
            self.guid
            + struct.pack("<H", len(self.entries))
            + b"".join(
                struct.pack("<4B", e.unit, e.channel, e.aux, e.physical) for e in self.entries
            )
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "ChannelMap":
        _need_at_least(payload, 18, "channel map")
        count = struct.unpack_from("<H", payload, 16)[0]
        _need(payload, 18 + 4 * count, "channel map entries")
        return cls(
            payload[:16], tuple(MapEntry(*payload[18 + 4 * i : 22 + 4 * i]) for i in range(count))
        )


@dataclass(frozen=True)
class AuxInit:
    """Short 0x39 (SetBgsAux). The manufacturer client sends six zero bytes before every start."""

    temperature: int = 0
    height: int = 0
    polarity: int = 0
    recount: int = 0
    connected: int = 0

    def to_payload(self) -> bytes:
        return struct.pack(
            "<hBBBB", self.temperature, self.height, self.polarity, self.recount, self.connected
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "AuxInit":
        _need(payload, 6, "SetBgsAux")
        return cls(*struct.unpack("<hBBBB", payload))


@dataclass(frozen=True)
class StepDownload:
    """Extended 0x0036 (SetStep82): one fragment of the step program.

    The controller resets its buffer when `index` is 1, rejects with result 2 if fragments
    overflow `total_bytes`, and with result 1 if the final program does not decode.
    """

    guid: bytes
    targets: tuple[Target, ...]
    total_bytes: int
    step_count: int
    final: bool
    index: int  # 1-based
    fragment: bytes

    def to_payload(self) -> bytes:
        if not 0 < len(self.fragment) <= MAX_FRAGMENT:
            raise ValueError(f"{len(self.fragment)}-byte download fragment")
        return (
            self.guid
            + _targets(self.targets)
            + struct.pack(
                "<IHBH", self.total_bytes, self.step_count, 2 if self.final else 1, self.index
            )
            + self.fragment
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "StepDownload":
        targets, end = _read_targets(payload, 16)
        _need_at_least(payload, end + 9, "step download header")
        total, steps, flag, index = struct.unpack_from("<IHBH", payload, end)
        return cls(payload[:16], targets, total, steps, flag == 2, index, payload[end + 9 :])


@dataclass(frozen=True)
class StepDownloadReply:
    guid: bytes
    targets: tuple[Target, ...]
    final: bool
    index: int

    @classmethod
    def from_payload(cls, payload: bytes) -> "StepDownloadReply":
        targets, end = _read_targets(payload, 16)
        _need(payload, end + 3, "step download reply")
        flag, index = struct.unpack_from("<BH", payload, end)
        return cls(payload[:16], targets, flag == 2, index)


def split_program(program: bytes, records: "list[bytes]") -> list[bytes]:
    """Group whole step records into fragments of at most FRAGMENT_TARGET bytes."""
    if b"".join(records) != program:
        raise ValueError("records do not make up the program")
    fragments: list[bytes] = []
    current = b""
    for record in records:
        if len(record) > FRAGMENT_TARGET:
            raise ValueError(f"{len(record)}-byte step record exceeds the fragment size")
        if current and len(current) + len(record) > FRAGMENT_TARGET:
            fragments.append(current)
            current = b""
        current += record
    return [*fragments, current]


@dataclass(frozen=True)
class ProtectionBlock:
    """A bitmap of settings followed by CRC32, used by 0x003D and 0x0054.

    Only the empty per-test block and the fan-error setting are built by this library;
    docs/protocol/commands.md lists the other bits.
    """

    map: int
    values: bytes = b""

    PAN_ERROR_PROTECTION = 1 << 10  # SysGlobalPrt.PrtWhenPanErr; value 0 enables it

    def to_bytes(self) -> bytes:
        body = struct.pack("<I", self.map) + self.values
        return body + struct.pack("<I", zlib.crc32(body))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ProtectionBlock":
        _need_at_least(raw, 8, "protection block")
        if zlib.crc32(raw[:-4]) != struct.unpack_from("<I", raw, len(raw) - 4)[0]:
            raise ProtocolError("protection block CRC32 mismatch")
        return cls(struct.unpack_from("<I", raw)[0], raw[4:-4])


@dataclass(frozen=True)
class TestProtection:
    """Extended 0x0054 (SetStepGlobal). The reply echoes GUID and targets."""

    guid: bytes
    targets: tuple[Target, ...]
    block: ProtectionBlock = ProtectionBlock(0)

    def to_payload(self) -> bytes:
        data = self.block.to_bytes()
        return self.reply_payload() + struct.pack("<I", len(data)) + data

    def reply_payload(self) -> bytes:
        """What the controller echoes: GUID and targets."""
        return self.guid + _targets(self.targets)

    @classmethod
    def from_payload(cls, payload: bytes) -> "TestProtection":
        targets, end = _read_targets(payload, 16)
        _need_at_least(payload, end + 4, "test protection length")
        size = struct.unpack_from("<I", payload, end)[0]
        _need(payload, end + 4 + size, "test protection block")
        return cls(payload[:16], targets, ProtectionBlock.from_bytes(payload[end + 4 :]))


@dataclass(frozen=True)
class GlobalProtection:
    """Extended 0x003D (SetGrobalProtect82), unit 0: device-wide settings. Empty reply."""

    block: ProtectionBlock

    @classmethod
    def pan_error_protection(cls, enabled: bool = True) -> "GlobalProtection":
        return cls(
            ProtectionBlock(ProtectionBlock.PAN_ERROR_PROTECTION, bytes([0 if enabled else 1]))
        )

    def to_payload(self) -> bytes:
        data = self.block.to_bytes()
        return struct.pack("<I", len(data)) + data

    @classmethod
    def from_payload(cls, payload: bytes) -> "GlobalProtection":
        _need_at_least(payload, 4, "global protection length")
        _need(payload, 4 + struct.unpack_from("<I", payload)[0], "global protection block")
        return cls(ProtectionBlock.from_bytes(payload[4:]))


@dataclass(frozen=True)
class Start:
    """Extended 0x003C (SetStart82). Arms each targeted channel and assigns it a new TestID.

    Arming is not starting: CONFIRM_START with the assigned TestIDs starts the channels.
    A controller rejects a Start on a channel that is already armed.
    """

    guid: bytes
    targets: tuple[Target, ...]
    flag: int = 1

    def to_payload(self) -> bytes:
        return self.guid + _targets(self.targets) + bytes([self.flag])

    @classmethod
    def from_payload(cls, payload: bytes) -> "Start":
        targets, end = _read_targets(payload, 16)
        _need(payload, end + 1, "Start")
        return cls(payload[:16], targets, payload[end])


@dataclass(frozen=True)
class ChannelTestId:
    unit: int
    channel: int
    test_id: int


def _test_ids(payload: bytes, what: str) -> tuple[ChannelTestId, ...]:
    _need_at_least(payload, 18, what)
    count = struct.unpack_from("<H", payload, 16)[0]
    _need(payload, 18 + 6 * count, what)
    return tuple(
        ChannelTestId(*struct.unpack_from("<BBI", payload, 18 + 6 * i)) for i in range(count)
    )


@dataclass(frozen=True)
class StartReply:
    """Reply 0x803C: the assigned TestIDs. A rejected Start carries only the GUID."""

    guid: bytes
    channels: tuple[ChannelTestId, ...]

    @classmethod
    def from_payload(cls, payload: bytes, result: int) -> "StartReply":
        if result:
            _need(payload, 16, "rejected Start reply")
            return cls(payload, ())
        return cls(payload[:16], _test_ids(payload, "Start reply"))


@dataclass(frozen=True)
class ConfirmStart:
    """Extended 0x003A (StartConfirm)."""

    guid: bytes
    channels: tuple[ChannelTestId, ...]

    def to_payload(self) -> bytes:
        return (
            self.guid
            + struct.pack("<H", len(self.channels))
            + b"".join(struct.pack("<BBI", c.unit, c.channel, c.test_id) for c in self.channels)
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "ConfirmStart":
        return cls(payload[:16], _test_ids(payload, "StartConfirm"))


@dataclass(frozen=True)
class ChannelResult:
    unit: int
    channel: int
    result: int  # 0 started, 1 no such channel, 2 TestID mismatch, 3 channel not armed


@dataclass(frozen=True)
class ConfirmStartReply:
    guid: bytes
    channels: tuple[ChannelResult, ...]

    @classmethod
    def from_payload(cls, payload: bytes) -> "ConfirmStartReply":
        _need_at_least(payload, 18, "StartConfirm reply")
        count = struct.unpack_from("<H", payload, 16)[0]
        _need(payload, 18 + 3 * count, "StartConfirm reply")
        return cls(
            payload[:16],
            tuple(ChannelResult(*payload[18 + 3 * i : 21 + 3 * i]) for i in range(count)),
        )


# --------------------------------------------------------------------------- #
# Lifecycle: Stop, Continue, Reset (Clear Flag), Jump
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Selection:
    """Body of Stop/Continue/Reset requests and replies: a u64 channel mask for one unit.

    Jump requests append the destination step. A rejected Stop reply has an empty body.
    """

    mask: int
    step: int | None = None

    def to_payload(self) -> bytes:
        body = struct.pack("<Q", self.mask)
        return body if self.step is None else body + bytes([self.step])

    @classmethod
    def from_payload(cls, payload: bytes) -> "Selection":
        if len(payload) == 9:
            return cls(struct.unpack_from("<Q", payload)[0], payload[8])
        _need(payload, 8, "channel selection")
        return cls(struct.unpack("<Q", payload)[0])


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def rtc_payload(local_time: datetime) -> bytes:
    """Short 0x03: year % 100, month, day, hour, minute, second, milliseconds:u16."""
    if local_time.tzinfo is not None:
        raise ValueError("the controller clock has no time zone; pass a naive local time")
    if not 2000 <= local_time.year <= 2099:
        raise ValueError("the controller stores two-digit years (2000..2099)")
    if local_time.microsecond % 1000:
        raise ValueError("the controller clock has millisecond resolution")
    return struct.pack(
        "<6BH",
        local_time.year % 100,
        local_time.month,
        local_time.day,
        local_time.hour,
        local_time.minute,
        local_time.second,
        local_time.microsecond // 1000,
    )


def parse_rtc_payload(payload: bytes) -> datetime:
    _need(payload, 8, "RTC")
    year, month, day, hour, minute, second, ms = struct.unpack("<6BH", payload)
    return datetime(2000 + year, month, day, hour, minute, second, ms * 1000)


def version_text(payload: bytes) -> str:
    """Version replies 0xD5/0xD6: a NUL-terminated ASCII string in a 30-byte field.

    The controller reply may append a length-prefixed trailer, kept by the caller.
    """
    if len(payload) < 30 or b"\0" not in payload[:30]:
        raise ProtocolError(f"unexpected version reply {payload.hex()}")
    return _cstring(payload[:30])


@dataclass(frozen=True)
class SerialTime:
    """ZwjTime: year (two digits), month, day, hour, minute, second."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int

    def to_datetime(self) -> datetime | None:
        try:
            return datetime(
                2000 + self.year, self.month, self.day, self.hour, self.minute, self.second
            )
        except ValueError:
            return None  # unset fields are zero


def _serial_time(raw: bytes, offset: int) -> SerialTime:
    return SerialTime(*raw[offset : offset + 6])


@dataclass(frozen=True)
class ControllerSerial:
    """Reply 0x802A (GetZwjSnResp), 52 bytes."""

    guid: bytes
    guid_time: SerialTime
    serial_number: str
    production_time: SerialTime
    cnp: int
    cnp_time: SerialTime
    cnp_language: int
    compatibility: int
    raw: bytes = field(repr=False)

    @classmethod
    def from_payload(cls, payload: bytes) -> "ControllerSerial":
        _need(payload, 52, "controller serial reply")
        return cls(
            payload[:16],
            _serial_time(payload, 16),
            _cstring(payload[22:34]),
            _serial_time(payload, 34),
            struct.unpack_from("<I", payload, 40)[0],
            _serial_time(payload, 44),
            payload[50],
            payload[51],
            payload,
        )


@dataclass(frozen=True)
class UnitSerial:
    """Reply 0x802B (GetXwjSnResp), 92 bytes."""

    guid: bytes
    guid_time: SerialTime
    serial_number: str
    production_time: SerialTime
    valid_until: SerialTime  # m_ztEffectiveUseOf
    functions: int  # m_dwXwjFuncTion
    kb_change_count: int
    first_calibration: SerialTime
    latest_calibration: SerialTime
    calibration_software: int
    calibration_tooling: int
    calibration_worker: str
    cnp: int
    cnp_time: SerialTime
    raw: bytes = field(repr=False)

    @classmethod
    def from_payload(cls, payload: bytes) -> "UnitSerial":
        _need(payload, 92, "unit serial reply")
        (
            functions,
            kb,
        ) = struct.unpack_from("<II", payload, 46)
        software, tooling = struct.unpack_from("<II", payload, 66)
        return cls(
            payload[:16],
            _serial_time(payload, 16),
            _cstring(payload[22:34]),
            _serial_time(payload, 34),
            _serial_time(payload, 40),
            functions,
            kb,
            _serial_time(payload, 54),
            _serial_time(payload, 60),
            software,
            tooling,
            _cstring(payload[74:82]),
            struct.unpack_from("<I", payload, 82)[0],
            _serial_time(payload, 86),
            payload,
        )


# --------------------------------------------------------------------------- #
# Reserve pause (code-derived; not used by the client)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PauseSchedule:
    """Common.ReservePause: when a running channel should pause by itself."""

    NO_STEP, CURRENT_STEP = 0xFFFF, 0xFFFE
    NO_CYCLE, CURRENT_CYCLE = 0xFFFFFFFF, 0xFFFFFFFE

    steps: tuple[tuple[int, int], ...] = ()  # (step, cycle)
    times: tuple[datetime, ...] = ()
    timeout_s: int | None = None
    pre_time: bool = False

    def to_bytes(self) -> bytes:
        body, flags = b"", 0
        if self.steps:
            flags |= 1
            body += struct.pack("<i", len(self.steps))
            body += b"".join(struct.pack("<HI", step, cycle) for step, cycle in self.steps)
        if self.times:
            flags |= 2
            body += struct.pack("<i", len(self.times))
            body += b"".join(
                struct.pack("<h5B", t.year, t.month, t.day, t.hour, t.minute, t.second)
                for t in self.times
            )
        if self.timeout_s is not None:
            flags |= 4
            body += struct.pack("<I", self.timeout_s)
        if self.pre_time:
            flags |= 8
        return struct.pack("<H", flags | 0x8000 if flags else 0) + body


@dataclass(frozen=True)
class ReservePause:
    """Extended 0x0037 (ReserveStop82). Targets use u64 masks, unlike other start commands."""

    SET, CANCEL = 1, 2

    guid: bytes
    targets: tuple[tuple[int, int], ...]  # (unit, u64 mask)
    action: int
    schedule: PauseSchedule

    def to_payload(self) -> bytes:
        schedule = self.schedule.to_bytes()
        return (
            self.guid
            + bytes([len(self.targets)])
            + b"".join(struct.pack("<BQ", unit, mask) for unit, mask in self.targets)
            + bytes([self.action])
            + schedule
        )
