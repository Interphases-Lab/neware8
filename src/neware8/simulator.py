"""
A simulated BTS82 controller that executes downloaded programs on simulated cells.

It speaks the native protocol to a `Client` exactly like the device: it connects to the
client's listener, uploads channel status periodically, sends history batches and answers
the start, control, information and settings commands. Unlike `tests/fake_controller.py`,
which only follows the handshakes, it runs each channel's native program on an
equivalent-circuit cell, so applications can be developed and tested end to end without
hardware::

    async with Client(host="127.0.0.1", controller_host="127.0.0.1", ...) as client:
        async with SimulatedController(port=client.listening_port, time_scale=60):
            await client.wait_ready()
            ...

What is simulated follows the documented device behaviour (docs/protocol); details that were
never observed on hardware are the simulator's own choices and are marked below:

* Steps: CC, CV and CCCV charge and discharge, rest, backward cycles and End, with the time,
  voltage, current and capacity end conditions of the recipe subset. Each executed Cycle
  jump increases the cycle index; the step-change counter wraps at 256.
* Protections of every active step: voltage upper and lower, current upper and lower and
  capacity upper, after the step's protection delay. A trip stops the channel as protected
  with alarm 67, 68, 69, 70 or 733 (catalogue codes chosen by the simulator).
* Recording: the first record of a test, then by the step's recording interval, voltage and
  current changes, and a record when a step ends or a protection trips. History is held in a
  log that outlives connections, as on the device.
* Stop freezes the test; Continue resumes it with a restarted step timer; Clear Flag turns a
  stopped or protected channel finished and keeps its alarm code; Jump starts another step.
* Simulated time runs `time_scale` times faster than wall-clock time, also while no client is
  connected. Status uploads follow the upload interval in wall-clock seconds unless
  `status_period_s` is given.

Not simulated: auxiliary channels, temperature changes, power and resistance modes, reserve
pause, and controller-wide TestID or clock persistence.
"""

import asyncio
import logging
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from . import messages as msg
from .measurements import current_scale
from .models import StepType
from .protocol import CONTROLLER_UNIT, Extended, Frame, FrameReader, Short, mask_channels
from .steps import Comparison, Step, Value, decode_program

logger = logging.getLogger(__name__)

FINISHED, RUNNING, STOPPED, PROTECTED = 0x00, 0x10, 0x20, 0x30
"""Base work types of the status and history records (see measurements.channel_state)."""

VOLTAGE_UPPER_ALARM = 67
VOLTAGE_LOWER_ALARM = 68
CURRENT_UPPER_ALARM = 69
CURRENT_LOWER_ALARM = 70
CAPACITY_UPPER_ALARM = 733
"""Alarm codes the simulator reports for protection trips (manufacturer catalogue codes)."""

CHARGE_MODES = frozenset({StepType.CC_CHARGE, StepType.CV_CHARGE, StepType.CCCV_CHARGE})
DISCHARGE_MODES = frozenset({StepType.CC_DISCHARGE, StepType.CV_DISCHARGE, StepType.CCCV_DISCHARGE})
VOLTAGE_MODES = frozenset(
    {StepType.CV_CHARGE, StepType.CCCV_CHARGE, StepType.CV_DISCHARGE, StepType.CCCV_DISCHARGE}
)
ACTIVE_MODES = CHARGE_MODES | DISCHARGE_MODES | {StepType.REST}
MAX_SUBSTEP_MS = 1000.0
"""Longest integration step of the cell model, in simulated milliseconds."""


@dataclass
class SimulatedCell:
    """An equivalent-circuit cell: an open-circuit voltage curve behind a series resistance."""

    capacity_ah: float = 0.005
    resistance_ohm: float = 10.0
    state_of_charge: float = 0.5
    temperature_c: float = 25.0

    def open_circuit_voltage(self) -> float:
        soc = min(max(self.state_of_charge, 1e-4), 1 - 1e-4)
        return 3.0 + 1.2 * soc + 0.05 * math.log(soc / (1 - soc))

    def apply(self, current_a: float, duration_s: float) -> None:
        self.state_of_charge += current_a * duration_s / 3600 / self.capacity_ah


@dataclass
class _Channel:
    unit: int
    channel: int
    cell: SimulatedCell
    work_type: int = FINISHED
    test_id: int = 0
    armed: bool = False
    alarm: int = 0
    pending: tuple[Step, ...] = ()  # downloaded, not yet started
    program: tuple[Step, ...] = ()
    position: int = 0  # index into program
    cycle: int = 1
    step_change: int = 1
    step_time_ms: float = 0.0
    total_time_ms: float = 0.0
    voltage_v: float = 0.0
    current_a: float = 0.0
    constant_voltage: bool = False
    charge_ah: float = 0.0
    discharge_ah: float = 0.0
    charge_wh: float = 0.0
    discharge_wh: float = 0.0
    loops: dict[int, int] = field(default_factory=dict)
    sequence: int = 0
    recorded_at_ms: float = 0.0  # step time of the latest record
    recorded_voltage: float = 0.0
    recorded_current: float = 0.0

    @property
    def step(self) -> Step:
        return self.program[self.position]


@dataclass
class _HistoryLog:
    """Controller history: byte positions of 94-byte records, outliving connections."""

    base: int = 109 << msg.HISTORY_FILE_SHIFT
    records: list[bytes] = field(default_factory=list)
    sent: int = 0

    def token(self, index: int) -> int:
        return self.base + index * msg.HistoryRecord.SIZE

    def index(self, token: int) -> int | None:
        index, rest = divmod(token - self.base, msg.HistoryRecord.SIZE)
        return index if not rest and 0 <= index <= len(self.records) else None


class SimulatedController:
    """
    A BTS82 controller simulated in software; see the module documentation.

    Args:
      port: The client's listening port.
      host: The client's address.
      units: The number of lower units, 1..32.
      channels_per_unit: Main channels per unit, 1..8.
      time_scale: How many times faster than wall-clock time the tests run.
      current_range_raw: The channels' native current range; -10 is a 10 mA range.
      cell: Builds the simulated cell of a channel from its (unit, channel) address.
      status_period_s: Wall-clock seconds between status uploads; None follows the upload
        interval the client sets.
      reconnect_delay_s: Wall-clock seconds before reconnecting after a dropped connection.
      tick_s: Wall-clock seconds between simulation steps.
      batch_size: The most records per history batch, 1..255.
    """

    def __init__(  # noqa: PLR0913  # keyword settings of a simulator
        self,
        *,
        port: int,
        host: str = "127.0.0.1",
        units: int = 1,
        channels_per_unit: int = 8,
        time_scale: float = 1.0,
        current_range_raw: int = -10,
        cell: Callable[[int, int], SimulatedCell] | None = None,
        status_period_s: float | None = None,
        reconnect_delay_s: float = 1.0,
        tick_s: float = 0.05,
        batch_size: int = 50,
        guid: bytes = bytes(range(16)),
        device: int = 7,
    ) -> None:
        if not 1 <= units <= 32 or not 1 <= channels_per_unit <= 8:
            raise ValueError("units must be 1..32 and channels_per_unit 1..8")
        if not 1 <= batch_size <= 255:
            raise ValueError("batch_size must be 1..255")
        if not time_scale > 0 or not tick_s > 0:
            raise ValueError("time_scale and tick_s must be positive")
        if len(guid) != 16:
            raise ValueError("guid must be 16 bytes")
        self.host, self.port = host, port
        self.units, self.channels_per_unit = units, channels_per_unit
        self.time_scale = time_scale
        self.range_raw = current_range_raw
        self.scale = current_scale(current_range_raw & 0xFFFFFFFF)
        self.status_period_s = status_period_s
        self.reconnect_delay_s = reconnect_delay_s
        self.tick_s = tick_s
        self.batch_size = batch_size
        self.guid = guid
        self.device = device
        self.start_flag = b"\xff" * 8
        self.interval_raw = 5
        self.test_id_counter = 0xA8002000
        self.log = _HistoryLog()
        build = cell or (lambda unit, channel: SimulatedCell())
        self.channels = {
            (u, c): _Channel(u, c, build(u, c))
            for u in range(1, units + 1)
            for c in range(1, channels_per_unit + 1)
        }
        for ch in self.channels.values():
            ch.voltage_v = ch.cell.open_circuit_voltage()
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._download = b""

    async def __aenter__(self) -> "SimulatedController":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def start(self) -> None:
        """Start the simulation clock and keep a connection to the client open."""
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._clock()),
                asyncio.create_task(self._connection()),
                asyncio.create_task(self._status_loop()),
            ]

    async def close(self) -> None:
        """Stop the simulation and disconnect."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._disconnect()

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def _connection(self) -> None:
        while True:
            try:
                reader, self._writer = await asyncio.open_connection(self.host, self.port)
            except OSError:
                await asyncio.sleep(self.reconnect_delay_s)
                continue
            logger.info("Simulated controller connected port=%d", self.port)
            try:
                self._send(
                    Frame(
                        self.start_flag,
                        self.device,
                        CONTROLLER_UNIT,
                        Short.CONNECT,
                        struct.pack("<BH", 24, 0x011A),
                    )
                )
                self._send(
                    Frame(
                        self.start_flag,
                        self.device,
                        CONTROLLER_UNIT,
                        Short.SEND_INFO,
                        self._info(),
                    )
                )
                for unit in range(1, self.units + 1):
                    self._send_status(unit)
                await self._read(reader)
            except (ConnectionError, OSError):
                pass
            finally:
                self._disconnect()
            logger.info("Simulated controller disconnected port=%d", self.port)
            await asyncio.sleep(self.reconnect_delay_s)

    def _disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def _send(self, frame: Frame) -> None:
        if self.connected:
            assert self._writer is not None
            self._writer.write(frame.to_bytes())

    def _info(self) -> bytes:
        block = bytearray(msg.RangeBlock.SIZE)
        for unit in range(self.units):
            block[unit] = (1 << self.channels_per_unit) - 1
        block[32] = msg.DeviceInfo.MAIN
        struct.pack_into("<ii", block, 33, self.range_raw, self.range_raw)
        block[99:115] = self.guid
        header = struct.pack("<H6sBBB", 0x75, b"Neware", 7, 2, 1)
        return header + struct.pack("<BH", 0, 1) + bytes(block)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        stream = FrameReader()
        while chunk := await reader.read(65536):
            for frame in stream.feed(chunk):
                if frame.reply or (frame.extended, frame.command) == (True, Extended.HISTORY):
                    continue  # history ACKs need no answer
                self._handle(frame)

    # ------------------------------------------------------------------ #
    # Uploads
    # ------------------------------------------------------------------ #
    async def _status_loop(self) -> None:
        while True:
            for unit in range(1, self.units + 1):
                self._send_status(unit)
            period = self.status_period_s
            await asyncio.sleep(self.interval_raw if period is None else period)

    def _send_status(self, unit: int) -> None:
        records = b"".join(
            self._status_record(ch) for ch in self.channels.values() if ch.unit == unit
        )
        self._send(
            Frame(
                self.start_flag,
                self.device,
                unit,
                Extended.STATUS,
                struct.pack("<IB", 1, len(records) // msg.StatusRecord.SIZE) + records,
                extended=True,
            )
        )

    def _status_record(self, ch: _Channel) -> bytes:
        step = ch.step if ch.program else None
        return msg.StatusRecord.FORMAT.pack(
            0x55,
            ch.channel,
            0,
            ch.cycle,
            step.step_id if step else 1,
            step.mode if step else 0,
            ch.work_type,
            round(ch.step_time_ms),
            round(ch.voltage_v * 10000),
            self._current_raw(ch.current_a),
            0,
            round(ch.cell.temperature_c * 10),
            *self._counters_raw(ch),
            self.range_raw & 0xFFFFFFFF,
            ch.alarm,
            round(ch.total_time_ms),
            ch.sequence,
            ch.sequence,
            ch.test_id,
            0xFFFF,
            0xFFFFFFFF,
            0,
        )

    def _current_raw(self, current_a: float) -> int:
        return round(current_a * self.scale * 1000)

    def _counters_raw(self, ch: _Channel) -> tuple[int, int, int, int]:
        divisor = self.scale * 3_600_000
        return (
            round(ch.charge_ah * divisor),
            round(ch.discharge_ah * divisor),
            round(ch.charge_wh * divisor),
            round(ch.discharge_wh * divisor),
        )

    def _record(self, ch: _Channel) -> None:
        """Append a history record of the channel's present values."""
        ch.sequence += 1
        ch.recorded_at_ms = ch.step_time_ms
        ch.recorded_voltage, ch.recorded_current = ch.voltage_v, ch.current_a
        step = ch.step
        now = datetime.now()
        self.log.records.append(
            msg.HistoryRecord.FORMAT.pack(
                msg.HistoryRecord.MAIN,
                ch.unit,
                ch.channel,
                0,
                ch.test_id,
                ch.sequence,
                ch.cycle,
                step.step_id,
                step.mode,
                ch.step_change,
                ch.work_type,
                round(ch.step_time_ms),
                round(ch.voltage_v * 10000),
                self._current_raw(ch.current_a),
                0,
                round(ch.cell.temperature_c * 10),
                *self._counters_raw(ch),
                now.year,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
                self.range_raw,
                round(ch.total_time_ms),
            )
        )

    def _send_history(self) -> None:
        log = self.log
        while self.connected and log.sent < len(log.records):
            batch = log.records[log.sent : log.sent + self.batch_size]
            payload = struct.pack("<BI", len(batch), log.token(log.sent)) + b"".join(batch)
            self._send(
                Frame(
                    self.start_flag,
                    self.device,
                    CONTROLLER_UNIT,
                    Extended.HISTORY,
                    payload,
                    extended=True,
                )
            )
            log.sent += len(batch)

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #
    def _handle(self, frame: Frame) -> None:  # noqa: C901, PLR0912  # one branch per command
        p = frame.payload
        key = (frame.extended, frame.command)
        if key == (True, Extended.GET_CONTROLLER_SERIAL):
            production = bytes([25, 1, 15, 9, 30, 0])
            serial = b"SIM000000001"
            self._send(frame.answer(self.guid + production + serial + production + bytes(12)))
        elif key == (True, Extended.GET_UNIT_SERIAL):
            self._send(frame.answer(self._unit_serial(frame.unit)))
        elif key == (True, Extended.GET_RTC_STATUS):
            self._send(frame.answer(b"\x00"))
        elif key == (True, Extended.GET_HISTORY_MIN_INDEX):
            self._send(frame.answer(self.log.token(0).to_bytes(4, "little")))
        elif key == (True, Extended.RESEND_HISTORY):
            self._send(frame.answer())
            index = self.log.index(int.from_bytes(p, "little"))
            self.log.sent = 0 if index is None else index
            self._send_history()
        elif key == (False, Short.GET_CONTROLLER_VERSION):
            self._send(frame.answer(b"SIM_BTS82_1.0".ljust(30, b"\0")))
        elif key == (False, Short.GET_UNIT_VERSION):
            self._send(frame.answer(f"SIM_UNIT_{frame.unit}".encode().ljust(30, b"\0")))
        elif key == (False, Short.SET_UPLOAD_INTERVAL):
            self.interval_raw = p[0]
            self._send(frame.answer(unit=CONTROLLER_UNIT))
        elif key == (False, Short.SET_RTC):
            self._send(frame.answer(unit=CONTROLLER_UNIT))
        elif key == (False, Short.SET_TEST_ID):
            self.test_id_counter = int.from_bytes(p, "little")
            self._send(frame.answer(p, unit=CONTROLLER_UNIT))
        elif key == (False, Short.SET_CHANNEL_MAP):
            self._send(frame.answer(p[:16]))
        elif key == (False, Short.SET_AUX):
            self._send(frame.answer())
        elif key == (True, Extended.DOWNLOAD_STEPS):
            self._handle_download(frame)
        elif key == (True, Extended.SET_TEST_PROTECTION):
            self._send(frame.answer(msg.TestProtection.from_payload(p).reply_payload()))
        elif key == (True, Extended.SET_GLOBAL_PROTECTION):
            self._send(frame.answer())
        elif key == (True, Extended.START):
            self._handle_start(frame)
        elif key == (True, Extended.CONFIRM_START):
            self._handle_confirm(frame)
        elif key in (
            (False, Short.STOP),
            (False, Short.CONTINUE),
            (False, Short.JUMP),
            (True, Extended.RESET_CHANNEL),
        ):
            self._handle_control(frame)
        else:
            logger.warning("Simulated controller ignores %s", frame.name)

    def _unit_serial(self, unit: int) -> bytes:
        made = bytes([25, 1, 15, 9, 30, 0])
        valid = bytes([30, 1, 15, 0, 0, 0])
        calibrated = bytes([26, 6, 1, 12, 0, 0])
        serial = f"SIMUNIT{unit:05d}".encode()[:12].ljust(12, b"\0")
        return (
            bytes(range(16, 32))
            + made
            + serial
            + made
            + valid
            + bytes(8)
            + calibrated
            + calibrated
            + bytes(8)
            + b"SIM".ljust(8, b"\0")
            + bytes(4)
            + bytes(6)
        )

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
                    program = tuple(decode_program(self._download))
                except ValueError:
                    program = ()
                if len(program) != request.step_count or not _executable(program):
                    result = 1
                else:
                    for target in request.targets:
                        for channel in target.channels:
                            ch = self.channels.get((target.unit, channel))
                            if ch is not None:
                                ch.pending = program
        body = (
            request.guid
            + bytes([len(request.targets)])
            + b"".join(struct.pack("<BI", t.unit, t.mask) for t in request.targets)
            + struct.pack("<BH", 2 if request.final else 1, request.index)
        )
        self._send(frame.answer(body, result=result))

    def _handle_start(self, frame: Frame) -> None:
        request = msg.Start.from_payload(frame.payload)
        selected = [self.channels.get((t.unit, c)) for t in request.targets for c in t.channels]
        if any(
            ch is None or ch.armed or ch.work_type != FINISHED or not ch.pending for ch in selected
        ):
            self._send(frame.answer(request.guid, result=1))
            return
        entries = b""
        for ch in selected:
            assert ch is not None
            self.test_id_counter += 1
            ch.test_id, ch.armed = self.test_id_counter, True
            entries += struct.pack("<BBI", ch.unit, ch.channel, ch.test_id)
        self._send(frame.answer(request.guid + struct.pack("<H", len(selected)) + entries))

    def _handle_confirm(self, frame: Frame) -> None:
        request = msg.ConfirmStart.from_payload(frame.payload)
        results = b""
        for entry in request.channels:
            ch = self.channels.get((entry.unit, entry.channel))
            result = (
                1 if ch is None else 2 if ch.test_id != entry.test_id else 3 if not ch.armed else 0
            )
            if result == 0 and ch is not None:
                self._begin(ch)
            results += bytes([entry.unit, entry.channel, result])
        self._send(frame.answer(request.guid + struct.pack("<H", len(request.channels)) + results))

    def _handle_control(self, frame: Frame) -> None:
        selection = msg.Selection.from_payload(frame.payload)
        for channel in mask_channels(selection.mask):
            ch = self.channels.get((frame.unit, channel))
            if ch is None or not ch.program:
                continue
            if frame.extended:  # Clear Flag: the alarm code stays reported
                if ch.work_type in (STOPPED, PROTECTED):
                    ch.work_type = FINISHED
            elif frame.command == Short.STOP:
                if ch.work_type == RUNNING:
                    ch.work_type, ch.current_a = STOPPED, 0.0
            elif frame.command == Short.CONTINUE:
                if ch.work_type in (STOPPED, PROTECTED):
                    ch.work_type, ch.alarm = RUNNING, 0
                    ch.step_time_ms = ch.recorded_at_ms = 0.0  # the step timer restarts
            elif frame.command == Short.JUMP and selection.step is not None:
                index = next(
                    (i for i, s in enumerate(ch.program) if s.step_id == selection.step), None
                )
                if index is not None and ch.work_type == RUNNING:
                    self._enter(ch, index)
        self._send(frame.answer(frame.payload[:8]))

    # ------------------------------------------------------------------ #
    # Program execution
    # ------------------------------------------------------------------ #
    def _begin(self, ch: _Channel) -> None:
        ch.armed, ch.alarm, ch.work_type = False, 0, RUNNING
        ch.program, ch.pending = ch.pending, ()
        ch.cycle, ch.step_change, ch.sequence = 1, 1, 0
        ch.total_time_ms = 0.0
        ch.loops = {}
        self._enter(ch, 0, change=False)
        self._settle(ch)
        self._record(ch)

    def _enter(self, ch: _Channel, index: int, *, change: bool = True) -> None:
        """Start the step at `index`, resolving Cycle and End steps on the way."""
        while True:
            ch.position = index
            step = ch.step
            if step.mode == StepType.CYCLE:
                start = _int(step, "cycle_start_step")
                count = _int(step, "cycle_count")
                done = ch.loops.get(index, 0) + 1
                target = next(i for i, s in enumerate(ch.program) if s.step_id == start)
                if done < count:
                    ch.loops = {k: v for k, v in ch.loops.items() if not target <= k < index}
                    ch.loops[index] = done
                    ch.cycle += 1
                    index = target
                else:
                    ch.loops.pop(index, None)
                    index += 1
                continue
            break
        if change:
            ch.step_change = (ch.step_change + 1) & 0xFF
        ch.step_time_ms = ch.recorded_at_ms = 0.0
        ch.charge_ah = ch.discharge_ah = ch.charge_wh = ch.discharge_wh = 0.0
        ch.constant_voltage = False
        if step.mode == StepType.END:
            ch.work_type, ch.current_a = FINISHED, 0.0

    async def _clock(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self.tick_s)
            now = loop.time()
            elapsed_ms, last = (now - last) * self.time_scale * 1000, now
            for ch in self.channels.values():
                if ch.work_type == RUNNING:
                    self._advance(ch, elapsed_ms)
                else:
                    ch.voltage_v, ch.current_a = ch.cell.open_circuit_voltage(), 0.0
            self._send_history()

    def advance(self, seconds: float) -> None:
        """Run every running channel for `seconds` of simulated time, immediately."""
        for ch in self.channels.values():
            if ch.work_type == RUNNING:
                self._advance(ch, seconds * 1000)
        self._send_history()

    def _advance(self, ch: _Channel, remaining_ms: float) -> None:
        while remaining_ms > 0 and ch.work_type == RUNNING:
            step = ch.step
            values = step.values
            duration = min(remaining_ms, MAX_SUBSTEP_MS)
            if "time_ms" in values:
                duration = min(duration, max(_int(step, "time_ms") - ch.step_time_ms, 0.0))
            if "record_time_ms" in values:
                due = ch.recorded_at_ms + _int(step, "record_time_ms") - ch.step_time_ms
                duration = min(duration, max(due, 0.0))
            self._integrate(ch, duration)
            remaining_ms -= duration
            alarm = self._tripped(ch)
            if alarm:
                ch.work_type, ch.alarm, ch.current_a = PROTECTED, alarm, 0.0
                self._record(ch)
                return
            if self._ended(ch):
                self._record(ch)
                self._enter(ch, ch.position + 1)
                self._settle(ch)
                continue
            if self._recording_due(ch):
                self._record(ch)

    def _settle(self, ch: _Channel) -> None:
        """Set voltage and current at the start of a step, before time advances."""
        if ch.work_type == RUNNING:
            self._integrate(ch, 0.0)

    def _integrate(self, ch: _Channel, duration_ms: float) -> None:
        step, cell = ch.step, ch.cell
        mode = step.mode
        ocv = cell.open_circuit_voltage()
        resistance = cell.resistance_ohm
        if mode == StepType.REST:
            current, voltage = 0.0, ocv
        else:
            sign = 1.0 if mode in CHARGE_MODES else -1.0
            limit = sign * self._amps(_int(step, "current"))
            current, voltage = limit, ocv + limit * resistance
            if mode in VOLTAGE_MODES:
                setpoint = _int(step, "voltage") / 10000
                beyond = voltage >= setpoint if sign > 0 else voltage <= setpoint
                if ch.constant_voltage or beyond:
                    ch.constant_voltage = True
                    held = (setpoint - ocv) / resistance
                    current = min(max(held, 0.0), limit) if sign > 0 else max(min(held, 0.0), limit)
                    voltage = ocv + current * resistance
        hours = duration_ms / 3_600_000
        cell.apply(current, hours * 3600)
        if current > 0:
            ch.charge_ah += current * hours
            ch.charge_wh += current * voltage * hours
        else:
            ch.discharge_ah -= current * hours
            ch.discharge_wh -= current * voltage * hours
        ch.voltage_v, ch.current_a = voltage, current
        ch.step_time_ms += duration_ms
        ch.total_time_ms += duration_ms

    def _tripped(self, ch: _Channel) -> int:
        step = ch.step
        if step.mode not in ACTIVE_MODES:
            return 0
        if ch.step_time_ms <= _int(step, "protect_delay_ms", 0):
            return 0
        voltage = round(ch.voltage_v * 10000)
        current = self._current_raw(ch.current_a)
        capacity = self._capacity_raw(ch)
        checks = (
            ("protect_voltage_upper", voltage, VOLTAGE_UPPER_ALARM),
            ("protect_voltage_lower", voltage, VOLTAGE_LOWER_ALARM),
            ("protect_current_upper", current, CURRENT_UPPER_ALARM),
            ("protect_current_lower", current, CURRENT_LOWER_ALARM),
            ("protect_capacity_upper", capacity, CAPACITY_UPPER_ALARM),
        )
        for name, measured, alarm in checks:
            limit = step.values.get(name)
            if limit is not None and _compare(measured, limit):
                return alarm
        return 0

    def _ended(self, ch: _Channel) -> bool:
        step = ch.step
        values = step.values
        if "time_ms" in values and ch.step_time_ms >= _int(step, "time_ms"):
            return True
        cutoff = values.get("cutoff_voltage")
        if cutoff is not None and _compare(round(ch.voltage_v * 10000), cutoff):
            return True
        cutoff = values.get("cutoff_current")
        if (
            cutoff is not None
            and ch.constant_voltage
            and _compare(abs(self._current_raw(ch.current_a)), cutoff)
        ):
            return True
        capacity = values.get("capacity")
        return capacity is not None and _compare(self._capacity_raw(ch), capacity)

    def _recording_due(self, ch: _Channel) -> bool:
        step = ch.step
        values = step.values
        if "record_time_ms" in values and (
            ch.step_time_ms - ch.recorded_at_ms >= _int(step, "record_time_ms")
        ):
            return True
        if "record_voltage" in values and abs(ch.voltage_v - ch.recorded_voltage) * 10000 >= (
            _int(step, "record_voltage")
        ):
            return True
        return "record_current" in values and abs(
            self._current_raw(ch.current_a - ch.recorded_current)
        ) >= _int(step, "record_current")

    def _amps(self, native: int) -> float:
        return native / (self.scale * 1000)

    def _capacity_raw(self, ch: _Channel) -> int:
        """Step capacity in native units: mA*s times the current scale."""
        return round((ch.charge_ah + ch.discharge_ah) * 3_600_000 * self.scale)


def _int(step: Step, name: str, default: int | None = None) -> int:
    item = step.values.get(name)
    if item is None:
        if default is None:
            raise KeyError(f"step {step.step_id} has no {name}")
        return default
    if not isinstance(item.value, int):
        raise TypeError(f"step {step.step_id}: {name} is not a scalar")
    return item.value


def _compare(measured: int, limit: Value) -> bool:
    if not isinstance(limit.value, int):
        return False
    target = limit.value
    match limit.comparison:
        case Comparison.GREATER_OR_EQUAL:
            return measured >= target
        case Comparison.GREATER:
            return measured > target
        case Comparison.LESS_OR_EQUAL:
            return measured <= target
        case Comparison.LESS:
            return measured < target
        case Comparison.EQUAL:
            return measured == target
        case Comparison.NOT_EQUAL:
            return measured != target
    return False


def _executable(program: tuple[Step, ...]) -> bool:
    """Whether the simulator can run every step: the recipe subset, ending with End."""
    if not program or program[-1].mode != StepType.END:
        return False
    for step in program:
        if step.mode == StepType.CYCLE:
            if not {"cycle_start_step", "cycle_count"} <= step.values.keys():
                return False
        elif step.mode in ACTIVE_MODES:
            if step.mode != StepType.REST and "current" not in step.values:
                return False
            if step.mode in VOLTAGE_MODES and "voltage" not in step.values:
                return False
        elif step.mode != StepType.END:
            return False
    return True


__all__ = [
    "CAPACITY_UPPER_ALARM",
    "CURRENT_LOWER_ALARM",
    "CURRENT_UPPER_ALARM",
    "SimulatedCell",
    "SimulatedController",
    "VOLTAGE_LOWER_ALARM",
    "VOLTAGE_UPPER_ALARM",
]
