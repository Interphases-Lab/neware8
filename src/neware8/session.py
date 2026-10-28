"""Per-connection state: controller identity, channel metadata and automatic replies. No I/O."""

from dataclasses import dataclass, field

from .messages import Connect, DeviceInfo, InfoAssembler, InfoFragment, RangeBlock
from .protocol import Extended, Frame, ProtocolError, Short


@dataclass
class Session:
    """Tracks what one controller connection has told us.

    `receive` returns the replies the protocol requires immediately (Connect, SendInfo).
    History batches are acknowledged by the client only after the records are stored.
    """

    start_flag: bytes | None = None
    device: int | None = None
    connect: Connect | None = None
    info: DeviceInfo | None = None
    heartbeats: int = 0
    _assembler: InfoAssembler = field(default_factory=InfoAssembler)

    @property
    def channels(self) -> dict[tuple[int, int], RangeBlock]:
        return self.info.main_channels() if self.info else {}

    def request(
        self, command: int, payload: bytes = b"", *, unit: int = 0, extended: bool = False
    ) -> Frame:
        """A host request addressed like this controller's own frames."""
        if self.start_flag is None or self.device is None:
            raise ProtocolError("no controller frame received yet")
        return Frame(self.start_flag, self.device, unit, command, payload, extended)

    def receive(self, frame: Frame) -> list[Frame]:
        identity = frame.start_flag, frame.device
        if self.start_flag is not None and identity != (self.start_flag, self.device):
            raise ProtocolError("controller start flag or device changed within the session")
        self.start_flag, self.device = identity
        if frame.reply or frame.extended:
            return []
        if frame.command == Short.CONNECT:
            self.connect = Connect.from_payload(frame.payload)
            return [frame.answer()]
        if frame.command == Short.SEND_INFO:
            info = self._assembler.add(InfoFragment.from_payload(frame.payload))
            if info is not None:
                self.info = info
            return [frame.answer(unit=0)]
        if frame.command == Short.HEARTBEAT:
            if frame.payload:
                raise ProtocolError("heartbeat with a payload")
            self.heartbeats += 1
        return []


# Uploads the manufacturer host acknowledges but this library does not interpret.
ACKNOWLEDGED_UNHANDLED = {(True, Extended.OTHER_DEVICE_INFO), (True, Extended.BACKUP_DATA)}
