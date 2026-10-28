"""
Wire format of the Neware BTS82 native protocol (controller <-> host, TCP 8001): framing and
the command catalogue. No I/O. See docs/protocol/README.md.

Every frame is::

    [start flag, 8 bytes][device][unit][length:u16le][command ...][payload][checksum][0x5A]

`length` counts the whole frame. The checksum is the byte sum of everything from `device` up
to the payload end. The command part has four forms:

    [cmd]                     short request/upload       cmd < 0x7F
    [cmd | 0x80][result]      short reply
    [0x7F][cmd:u16le]         extended request/upload
    [0xFF][result][cmd|0x8000:u16le]   extended reply

The controller opens the connection, so "request" only describes the form: uploads from the
controller (status, history, metadata) use the request forms and the host replies to them.
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from .errors import ProtocolError as ProtocolError

TERMINATOR = 0x5A
EXTENDED_REQUEST = 0x7F
EXTENDED_REPLY = 0xFF
SHORT_REPLY_BIT = 0x80
EXTENDED_REPLY_BIT = 0x8000
HEADER_SIZE = 12  # start flag + device + unit + length
MIN_FRAME_SIZE = HEADER_SIZE + 1 + 2
MAX_FRAME_SIZE = 0xFFFF
CONTROLLER_UNIT = 0xFE  # unit byte addressing the controller itself
SUCCESS = 0


class Short(IntEnum):
    """One-byte commands. Names follow the manufacturer's packet classes (BGS/BTS70)."""

    CONNECT = 0x00  # controller -> host on connect; body: client type, version
    SET_RTC = 0x03
    GET_SL_STATUS = 0x04  # BTS70; purpose not established
    SET_SL_STATUS = 0x05  # BTS70; purpose not established
    LIGHTEN = 0x0A  # channel locate light
    STOP = 0x11
    BROADCAST_STOP = 0x12
    CONTINUE = 0x14
    SET_DEVICE_ID = 0x20  # persistent; changes the controller's device byte
    HEARTBEAT = 0x30  # controller -> host, no reply
    GET_CONTROLLER_SN = 0x33  # legacy serial query (GetSNZwj)
    SET_AUX = 0x39  # SetBgsAux: six bytes, all zero in every manufacturer start
    CALIBRATE_ZERO = 0x42  # calibration, forbidden
    UPDATE_CONTROLLER = 0x49  # firmware (UpdateZwj), forbidden
    GET_UNIT_VERSION = 0x55  # GetVerXwj
    GET_CONTROLLER_VERSION = 0x56  # GetVerZwj
    SET_UPLOAD_INTERVAL = 0x5E  # SetRTInterval
    JUMP = 0x6F  # Jump70
    SEND_INFO = 0x72  # controller -> host: channel/range metadata, possibly multipart
    SET_CHANNEL_MAP = 0x73  # SetChlMap70
    SET_TEST_ID = 0x74  # SetTestID70: device-wide TestID counter


class Extended(IntEnum):
    """Two-byte commands (prefix 0x7F). Generation in brackets is the defining packet class."""

    SET_CONTROLLER_RESTART = 0x0006  # [BGS] SetMidReStart, forbidden
    UPDATE_UNIT = 0x0007  # [BTS76] UpdateXWJ firmware, forbidden
    RESET_CHANNEL = 0x0008  # [BTS76] ResetMidChlStatus; the UI's "Clear Flag"
    DISK_STATUS = 0x000D  # [BTS78] controller -> host
    GET_HISTORY_MIN_INDEX = 0x000F  # [BTS78] GetDfMinIndex
    RESEND_HISTORY = 0x0010  # [BTS78] SetReSendDfData
    HISTORY = 0x0015  # [BTS79] SendDFData79, controller -> host, ACKed
    GET_BACKUP_FILE_INFO = 0x0018  # [BTS79] backup file transfer, forbidden
    GET_BACKUP_FILE = 0x0019  # forbidden
    SEND_BACKUP_FILE = 0x001A  # forbidden
    FORWARD_CALIBRATION = 0x0022  # [BTS80] forbidden
    GET_RTC_STATUS = 0x0024  # [BTS80]
    SET_RTC_STATUS = 0x0025  # [BTS80]
    HISTORY_STATUS = 0x0027  # [BTS80] DfStatus80, controller -> host: sizes
    SET_FILE = 0x0029  # [BTS80] file transfer, forbidden
    GET_CONTROLLER_SERIAL = 0x002A  # [BTS80] GetZwjSn: GUID, serial number, dates
    GET_UNIT_SERIAL = 0x002B  # [BTS80] GetXwjSn
    BACKUP_DATA = 0x002E  # [BTS81] controller -> host, ACKed with the data flag
    DOWNLOAD_STEPS = 0x0036  # [BTS82] SetStep82
    RESERVE_PAUSE = 0x0037  # [BTS82] ReserveStop82: pause at a step/cycle/time
    GET_RESERVE_PAUSE = 0x0038  # [BTS82] GetReserveStop82
    STATUS = 0x0039  # [BTS82] SendRTData82, controller -> host, not ACKed
    CONFIRM_START = 0x003A  # [BTS82] StartConfirm: actually starts armed channels
    SHELF_STATUS = 0x003B  # [BTS82] BonStatusN2, controller -> host
    START = 0x003C  # [BTS82] SetStart82: arms channels, assigns TestIDs
    SET_GLOBAL_PROTECTION = 0x003D  # [BTS82] SetGrobalProtect82: device-wide settings
    SET_LEAK_RATE = 0x003E  # [BTS82]
    SET_TEMPERATURE_MAP = 0x003F  # [BTS82] SendTempMap
    CHANNEL_CONTROL = 0x0040  # [BTS82] SendChlCtrl: (channel, ctrl); purpose unknown
    CONTROLLER_STATUS = 0x0041  # [BTS82] ZwjTotalRT, controller -> host: fans, disks
    TFTP_FILE = 0x0042  # [BTS82] forbidden
    CALIBRATE = 0x0044  # [BTS82] forbidden
    GET_UNIT_VERSIONS = 0x0045  # [BTS82] GetXwjVerEx
    SET_DBC = 0x0047  # [BTS82] CAN DBC configuration
    SEND_DBC = 0x0048  # [BTS82]
    SET_OTHER_DEVICE_MAP = 0x0051  # [BTS82] external devices (chillers, chambers)
    OTHER_DEVICE_CONTROL = 0x0052  # [BTS82]
    OTHER_DEVICE_INFO = 0x0053  # [BTS82] controller -> host, ACKed
    SET_TEST_PROTECTION = 0x0054  # [BTS82] SetStepGlobal: per-test global protections


# Commands that can modify firmware, calibration, files or restart the controller. They are
# catalogued so they are recognisable in captures; the library never builds them.
FORBIDDEN = frozenset(
    {
        (False, Short.CALIBRATE_ZERO),
        (False, Short.UPDATE_CONTROLLER),
        *(
            (True, command)
            for command in (
                Extended.SET_CONTROLLER_RESTART,
                Extended.UPDATE_UNIT,
                Extended.GET_BACKUP_FILE_INFO,
                Extended.GET_BACKUP_FILE,
                Extended.SEND_BACKUP_FILE,
                Extended.FORWARD_CALIBRATION,
                Extended.SET_FILE,
                Extended.TFTP_FILE,
                Extended.CALIBRATE,
            )
        ),
    }
)


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


@dataclass(frozen=True)
class Frame:
    """One native frame. `command` never includes the reply bit; `reply` says which form."""

    start_flag: bytes
    device: int
    unit: int
    command: int
    payload: bytes = b""
    extended: bool = False
    reply: bool = False
    result: int = SUCCESS  # replies only

    @property
    def key(self) -> tuple[bool, int, bool]:
        return self.extended, self.command, self.reply

    @property
    def name(self) -> str:
        enum = Extended if self.extended else Short
        try:
            name = enum(self.command).name
        except ValueError:
            name = f"0x{self.command:04x}" if self.extended else f"0x{self.command:02x}"
        return name + (" reply" if self.reply else "")

    def to_bytes(self) -> bytes:
        if len(self.start_flag) != 8:
            raise ValueError("start flag must be 8 bytes")
        if (self.extended, self.command) in FORBIDDEN and not self.reply:
            raise ValueError(f"{self.name} is a forbidden maintenance command")
        if self.extended:
            head = (
                struct.pack("<BBH", EXTENDED_REPLY, self.result, self.command | EXTENDED_REPLY_BIT)
                if self.reply
                else struct.pack("<BH", EXTENDED_REQUEST, self.command)
            )
        else:
            if not 0 <= self.command < EXTENDED_REQUEST:
                raise ValueError(f"short command 0x{self.command:x} out of range")
            head = (
                bytes([self.command | SHORT_REPLY_BIT, self.result])
                if self.reply
                else bytes([self.command])
            )
        size = HEADER_SIZE + len(head) + len(self.payload) + 2
        if size > MAX_FRAME_SIZE:
            raise ValueError(f"{size}-byte frame exceeds the u16 length field")
        body = struct.pack("<BBH", self.device, self.unit, size) + head + self.payload
        return self.start_flag + body + bytes([checksum(body), TERMINATOR])

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Frame":
        if len(raw) < MIN_FRAME_SIZE:
            raise ProtocolError(f"{len(raw)}-byte frame is too short")
        device, unit, size = struct.unpack_from("<BBH", raw, 8)
        if size != len(raw):
            raise ProtocolError(f"length field says {size}, frame has {len(raw)} bytes")
        if raw[-1] != TERMINATOR:
            raise ProtocolError("frame does not end in 0x5A")
        if raw[-2] != checksum(raw[8:-2]):
            raise ProtocolError(f"bad checksum in {raw.hex()}")
        wire = raw[12]
        if wire == EXTENDED_REQUEST:
            if size < 17:
                raise ProtocolError("truncated extended request")
            return cls(
                raw[:8],
                device,
                unit,
                struct.unpack_from("<H", raw, 13)[0],
                raw[15:-2],
                extended=True,
            )
        if wire == EXTENDED_REPLY:
            if size < 18:
                raise ProtocolError("truncated extended reply")
            command = struct.unpack_from("<H", raw, 14)[0]
            if not command & EXTENDED_REPLY_BIT:
                raise ProtocolError(f"extended reply without reply bit: 0x{command:04x}")
            return cls(
                raw[:8],
                device,
                unit,
                command & ~EXTENDED_REPLY_BIT,
                raw[16:-2],
                extended=True,
                reply=True,
                result=raw[13],
            )
        if wire & SHORT_REPLY_BIT:
            if size < 16:
                raise ProtocolError("truncated short reply")
            return cls(
                raw[:8],
                device,
                unit,
                wire & ~SHORT_REPLY_BIT,
                raw[14:-2],
                reply=True,
                result=raw[13],
            )
        return cls(raw[:8], device, unit, wire, raw[13:-2])

    def answer(
        self, payload: bytes = b"", *, unit: int | None = None, result: int = SUCCESS
    ) -> "Frame":
        """The reply frame to this request/upload, with the same start flag and device."""
        return Frame(
            self.start_flag,
            self.device,
            self.unit if unit is None else unit,
            self.command,
            payload,
            self.extended,
            reply=True,
            result=result,
        )

    def __str__(self) -> str:
        result = f" result={self.result}" if self.reply else ""
        return (
            f"Frame({self.name} device={self.device} unit={self.unit}{result} "
            f"payload={self.payload.hex(' ')})"
        )


@dataclass
class FrameReader:
    """Splits a TCP byte stream into frames; TCP segment boundaries are not frame boundaries."""

    buffer: bytearray = field(default_factory=bytearray)

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer += data
        frames = []
        while len(self.buffer) >= HEADER_SIZE:
            size = struct.unpack_from("<H", self.buffer, 10)[0]
            if size < MIN_FRAME_SIZE:
                raise ProtocolError(f"invalid frame length {size}")
            if len(self.buffer) < size:
                break
            frames.append(Frame.from_bytes(bytes(self.buffer[:size])))
            del self.buffer[:size]
        return frames

    def finish(self) -> None:
        if self.buffer:
            raise ProtocolError(f"{len(self.buffer)} trailing bytes do not form a frame")


def channel_mask(channels: "list[int] | tuple[int, ...]") -> int:
    """Bit (channel - 1) for each 1-based channel."""
    return sum(1 << (channel - 1) for channel in channels)


def mask_channels(mask: int) -> tuple[int, ...]:
    return tuple(bit + 1 for bit in range(mask.bit_length()) if mask >> bit & 1)
