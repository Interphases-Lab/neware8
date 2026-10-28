"""
A simulated BTS82 controller, modelled on captures and on the manufacturer's controller
simulator (VirtualMiddleBgs.exe, CMiddleSocket82). It connects to the client like the real
device, uploads status periodically and follows the device rules that matter for the client:

* Start arms each channel and assigns the next TestID; a channel that is already armed or
  running rejects Start. StartConfirm starts armed channels (results 0/1/2/3).
* The download buffer resets at fragment 1; overflow answers 2, an undecodable program 1.
* Stop/Continue/Reset/Jump change the channel state and echo the selection.
"""

import asyncio
import struct
from dataclasses import dataclass, field

from neware8 import messages as msg
from neware8.protocol import CONTROLLER_UNIT, Extended, Frame, FrameReader, Short, mask_channels
from neware8.steps import decode_program

RUNNING, STOPPED, FINISHED = 0x10, 0x20, 0x00
GUID = bytes(range(16))


@dataclass
class SimChannel:
    unit: int
    channel: int
    work_type: int = FINISHED
    test_id: int = 0
    armed: bool = False
    step_id: int = 1
    step_type: int = 0
    alarm: int = 0
    sequence: int = 0


@dataclass
class FakeController:
    port: int
    units: int = 1
    channels_per_unit: int = 8
    device: int = 7
    start_flag: bytes = b"\xff" * 8
    interval: float = 0.02
    test_id_counter: int = 0xA8002000
    silent: set[tuple[bool, int]] = field(default_factory=set)  # never answered
    reject: dict[tuple[bool, int], int] = field(default_factory=dict)  # answered with result
    received: list[Frame] = field(default_factory=list)
    acks: list[int] = field(default_factory=list)
    min_history_index: int = 100

    def __post_init__(self) -> None:
        self.channels = {
            (u, c): SimChannel(u, c)
            for u in range(1, self.units + 1)
            for c in range(1, self.channels_per_unit + 1)
        }
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._download = b""
        self.program = b""
        self.interval_raw = 5

    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        reader, self._writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.send(
            Frame(
                self.start_flag,
                self.device,
                CONTROLLER_UNIT,
                Short.CONNECT,
                struct.pack("<BH", 24, 0x011A),
            )
        )
        self.send(
            Frame(self.start_flag, self.device, CONTROLLER_UNIT, Short.SEND_INFO, self._info())
        )
        self._tasks = [
            asyncio.create_task(self._read(reader)),
            asyncio.create_task(self._status_loop()),
        ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def go_silent(self) -> None:
        """Stop uploading without closing the socket, like a pulled network cable."""
        self._tasks[1].cancel()

    def send(self, frame: Frame) -> None:
        assert self._writer is not None
        self._writer.write(frame.to_bytes())

    def _info(self) -> bytes:
        block = bytearray(msg.RangeBlock.SIZE)
        for unit in range(self.units):
            block[unit] = (1 << self.channels_per_unit) - 1
        block[32] = msg.DeviceInfo.MAIN
        struct.pack_into("<ii", block, 33, -10, -10)
        block[99:115] = GUID
        header = struct.pack("<H6sBBB", 0x75, b"Neware", 7, 2, 1)
        return header + struct.pack("<BH", 0, 1) + bytes(block)

    # ------------------------------------------------------------------ #
    async def _status_loop(self) -> None:
        while True:
            for unit in range(1, self.units + 1):
                self.send_status(unit)
            await asyncio.sleep(self.interval)

    def send_status(self, unit: int) -> None:
        records = b"".join(self._record(ch) for ch in self.channels.values() if ch.unit == unit)
        count = records and len(records) // msg.StatusRecord.SIZE
        self.send(
            Frame(
                self.start_flag,
                self.device,
                unit,
                Extended.STATUS,
                struct.pack("<IB", 1, count) + records,
                extended=True,
            )
        )

    @staticmethod
    def _record(ch: SimChannel) -> bytes:
        return msg.StatusRecord.FORMAT.pack(
            0x55,
            ch.channel,
            0,
            1,
            ch.step_id,
            ch.step_type,
            ch.work_type,
            1000,
            36000,
            -10000,
            0,
            250,
            0,
            0,
            0,
            0,
            (-10) & 0xFFFFFFFF,
            ch.alarm,
            2000,
            0,
            0,
            ch.test_id,
            0xFFFF,
            0xFFFFFFFF,
            0,
        )

    def history(self, token: int, records: list[bytes]) -> None:
        payload = struct.pack("<BI", len(records), token) + b"".join(records)
        self.send(
            Frame(
                self.start_flag,
                self.device,
                CONTROLLER_UNIT,
                Extended.HISTORY,
                payload,
                extended=True,
            )
        )

    def history_record(
        self,
        unit: int,
        channel: int,
        sequence: int,
        *,
        test_id: int | None = None,
        time_ms: int = 1000,
    ) -> bytes:
        ch = self.channels[unit, channel]
        return msg.HistoryRecord.FORMAT.pack(
            0x55,
            unit,
            channel,
            0,
            ch.test_id if test_id is None else test_id,
            sequence,
            1,
            ch.step_id,
            1,
            1,
            ch.work_type,
            time_ms,
            36000,
            -10000,
            0,
            250,
            0,
            0,
            0,
            0,
            2026,
            10,
            20,
            12,
            0,
            0,
            -10,
            time_ms,
        )

    # ------------------------------------------------------------------ #
    async def _read(self, reader: asyncio.StreamReader) -> None:
        stream = FrameReader()
        while chunk := await reader.read(65536):
            for frame in stream.feed(chunk):
                self.received.append(frame)
                if frame.reply:
                    if (frame.extended, frame.command) == (True, Extended.HISTORY):
                        self.acks.append(int.from_bytes(frame.payload, "little"))
                    continue
                key = (frame.extended, frame.command)
                if key == (True, Extended.HISTORY):
                    continue
                if key in self.silent:
                    continue
                if key in self.reject:
                    payload = b"" if key == (False, Short.STOP) else self._rejection(frame)
                    self.send(frame.answer(payload, result=self.reject[key]))
                    continue
                self._handle(frame)

    def _rejection(self, frame: Frame) -> bytes:
        if (frame.extended, frame.command) == (True, Extended.START):
            return frame.payload[:16]
        if not frame.extended and frame.command in (Short.CONTINUE, Short.JUMP):
            return frame.payload[:8]
        return b""

    def _handle(self, frame: Frame) -> None:
        p = frame.payload
        if (frame.extended, frame.command) == (True, Extended.GET_CONTROLLER_SERIAL):
            self.send(frame.answer(GUID + bytes(6) + b"SIM000000001" + bytes(18)))
        elif (frame.extended, frame.command) == (True, Extended.GET_UNIT_SERIAL):
            self.send(frame.answer(bytes(range(16, 32)) + bytes(6) + b"UNIT00000001" + bytes(58)))
        elif (frame.extended, frame.command) == (True, Extended.GET_RTC_STATUS):
            self.send(frame.answer(b"\x00"))
        elif (frame.extended, frame.command) == (True, Extended.GET_HISTORY_MIN_INDEX):
            self.send(frame.answer(self.min_history_index.to_bytes(4, "little")))
        elif (frame.extended, frame.command) == (True, Extended.RESEND_HISTORY):
            self.send(frame.answer())
            self.history(int.from_bytes(p, "little"), [])
        elif not frame.extended and frame.command == Short.GET_CONTROLLER_VERSION:
            self.send(frame.answer(b"SIM_1.0".ljust(30, b"\0")))
        elif not frame.extended and frame.command == Short.GET_UNIT_VERSION:
            self.send(frame.answer(b"UNIT_1.0".ljust(30, b"\0")))
        elif not frame.extended and frame.command == Short.SET_UPLOAD_INTERVAL:
            self.interval_raw = p[0]
            self.send(frame.answer(unit=CONTROLLER_UNIT))
        elif not frame.extended and frame.command == Short.SET_RTC:
            self.send(frame.answer(unit=CONTROLLER_UNIT))
        elif not frame.extended and frame.command == Short.SET_TEST_ID:
            self.test_id_counter = int.from_bytes(p, "little")
            self.send(frame.answer(p, unit=CONTROLLER_UNIT))
        elif not frame.extended and frame.command == Short.SET_CHANNEL_MAP:
            self.send(frame.answer(p[:16]))
        elif not frame.extended and frame.command == Short.SET_AUX:
            self.send(frame.answer())
        elif frame.extended and frame.command == Extended.DOWNLOAD_STEPS:
            self._handle_download(frame)
        elif frame.extended and frame.command == Extended.SET_TEST_PROTECTION:
            request = msg.TestProtection.from_payload(p)
            self.send(frame.answer(request.reply_payload()))
        elif frame.extended and frame.command == Extended.SET_GLOBAL_PROTECTION:
            self.send(frame.answer())
        elif frame.extended and frame.command == Extended.START:
            self._handle_start(frame)
        elif frame.extended and frame.command == Extended.CONFIRM_START:
            self._handle_confirm(frame)
        elif (
            not frame.extended
            and frame.command in (Short.STOP, Short.CONTINUE, Short.JUMP)
            or (frame.extended and frame.command == Extended.RESET_CHANNEL)
        ):
            self._handle_control(frame)

    def _handle_download(self, frame: Frame) -> None:
        request = msg.StepDownload.from_payload(frame.payload)
        if request.index == 1:
            self._download = b""
        result = 0
        if len(self._download) + len(request.fragment) > request.total_bytes:
            result, self._download = 2, b""
        else:
            self._download += request.fragment
            if request.final:
                try:
                    if len(decode_program(self._download)) != request.step_count:
                        result = 1
                except ValueError:
                    result = 1
                self.program = self._download
        body = (
            request.guid
            + bytes([len(request.targets)])
            + b"".join(struct.pack("<BI", t.unit, t.mask) for t in request.targets)
            + struct.pack("<BH", 2 if request.final else 1, request.index)
        )
        self.send(frame.answer(body, result=result))

    def _handle_start(self, frame: Frame) -> None:
        request = msg.Start.from_payload(frame.payload)
        guid = request.guid
        selected = [self.channels[t.unit, c] for t in request.targets for c in t.channels]
        if any(ch.armed or ch.work_type != FINISHED for ch in selected):
            self.send(frame.answer(guid, result=1))
            return
        entries = b""
        for ch in selected:
            self.test_id_counter += 1
            ch.test_id, ch.armed = self.test_id_counter, True
            entries += struct.pack("<BBI", ch.unit, ch.channel, ch.test_id)
        self.send(frame.answer(guid + struct.pack("<H", len(selected)) + entries))

    def _handle_confirm(self, frame: Frame) -> None:
        request = msg.ConfirmStart.from_payload(frame.payload)
        results = b""
        for entry in request.channels:
            ch = self.channels.get((entry.unit, entry.channel))
            result = (
                1 if ch is None else 2 if ch.test_id != entry.test_id else 3 if not ch.armed else 0
            )
            if result == 0 and ch is not None:
                ch.armed, ch.work_type, ch.step_id, ch.step_type = False, RUNNING, 1, 1
            results += bytes([entry.unit, entry.channel, result])
        self.send(frame.answer(request.guid + struct.pack("<H", len(request.channels)) + results))

    def _handle_control(self, frame: Frame) -> None:
        selection = msg.Selection.from_payload(frame.payload)
        for channel in mask_channels(selection.mask):
            ch = self.channels[frame.unit, channel]
            if frame.extended:  # reset ("Clear Flag")
                ch.work_type, ch.alarm = FINISHED, 0
            elif frame.command == Short.STOP:
                ch.work_type = STOPPED
            elif frame.command == Short.CONTINUE:
                ch.work_type = RUNNING
            elif frame.command == Short.JUMP:
                ch.step_id = selection.step or ch.step_id
        self.send(frame.answer(frame.payload[:8]))

    def finish(self, unit: int, channel: int) -> None:
        self.channels[unit, channel].work_type = FINISHED

    def sent(self, extended: bool, command: int) -> list[Frame]:
        return [
            f
            for f in self.received
            if (f.extended, f.command, f.reply) == (extended, command, False)
        ]
