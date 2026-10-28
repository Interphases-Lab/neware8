"""
Asyncio client for one BTS82 controller: listener, status cache, serialized control and
history recording.

    async with Client(host=..., controller_host=..., storage_directory=...) as client:
        await client.wait_ready()
        run = await client.start(recipe, profile)

The controller connects to us. One exchange runs at a time; a write whose reply does not
arrive is never retried: the connection is closed and the caller gets OperationUncertain.
"""

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from . import messages as msg
from ._bdf import PendingRecordTime
from .alarms import overloaded_energy
from .controller import ControllerInfo, UnitInfo
from .errors import ConnectionLost, OperationRejected, OperationUncertain, ProtocolError, StateError
from .measurements import readings
from .models import (
    ChannelAddress,
    ChannelProfile,
    ChannelState,
    ChannelStatus,
    Control,
    Export,
    RecordChange,
    RecordedPoint,
    RecordRevision,
    Run,
    RunState,
    StatusSnapshot,
    StepType,
    bounded_integer,
)
from .protocol import CONTROLLER_UNIT, Extended, Frame, FrameReader, Short, channel_mask
from .recipe import Recipe
from .session import ACKNOWLEDGED_UNHANDLED, Session
from .steps import split_records
from .storage import MeasurementStore

logger = logging.getLogger(__name__)
wire_logger = logging.getLogger("neware8.wire")
wire_logger.setLevel(logging.WARNING)
wire_logger.addHandler(logging.NullHandler())
wire_logger.propagate = False

REPLY_TIMEOUT = 20  # seconds for any controller reply
CONFIRM_TIMEOUT = 20  # seconds for a control's effect to show in the status
SAME_UNIT = -1  # _exchange: the reply must come from the request's unit

# Controls: command and the state each requires before / shows after.
CONTROLS = {
    Control.STOP: (Short.STOP, False),
    Control.CONTINUE: (Short.CONTINUE, False),
    Control.CLEAR_FLAG: (Extended.RESET_CHANNEL, True),
    Control.JUMP: (Short.JUMP, False),
}
CLEARABLE = {
    ChannelState.FINISHED,
    ChannelState.STOPPED,
    ChannelState.PROTECTED,
    ChannelState.BEEP,
    ChannelState.FINISHED_PROTECTED,
}


class Client:
    """One controller per listener and storage directory; use separate clients for others."""

    def __init__(
        self,
        *,
        host: str,
        controller_host: str,
        storage_directory: str | Path,
        port: int = 8001,
        profiles: tuple[ChannelProfile, ...] = (),
        upload_interval_raw: int = 5,
        missed_upload_intervals: float = 3,
        history_grace_period_s: float = 30,
        receive_timeout_s: float = 60,
    ) -> None:
        ipaddress.ip_address(host)
        ipaddress.ip_address(controller_host)
        bounded_integer("port", port, 65535)
        bounded_integer("upload_interval_raw", upload_interval_raw, 255, 1)
        if len({p.address for p in profiles}) != len(profiles) or len(
            {p.physical_id for p in profiles}
        ) != len(profiles):
            raise ValueError("configured channel addresses and physical IDs must be unique")
        for name, value in (
            ("missed_upload_intervals", missed_upload_intervals),
            ("history_grace_period_s", history_grace_period_s),
            ("receive_timeout_s", receive_timeout_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self.host, self.controller_host, self.port = host, controller_host, port
        self.upload_interval_raw = upload_interval_raw
        self.missed_upload_intervals = missed_upload_intervals
        self.history_grace_period_s = history_grace_period_s
        # The controller sends a heartbeat about every 17 s; like the manufacturer server we
        # drop a silent connection so that the controller's reconnect is not refused.
        self.receive_timeout_s = receive_timeout_s
        self.store = MeasurementStore(storage_directory)
        self._configured = {p.address: p for p in profiles}

        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._peer_task: asyncio.Task[None] | None = None
        self._session = Session()
        self._bound = False  # controller identity verified and storage bound
        self._closed = False
        self._error: BaseException | None = None
        self._ready = asyncio.Event()
        self._discovered = asyncio.Event()
        self._operation = asyncio.Lock()  # one exchange-driven operation at a time
        self._pending: tuple[tuple[bool, int, bool], int | None, asyncio.Future[Frame]] | None = (
            None
        )
        self._on_reply: Callable[[Frame], Awaitable[None]] | None = None
        self._warned: set[tuple[bool, int, bool]] = set()

        self._statuses: dict[ChannelAddress, ChannelStatus] = {}
        self._records: dict[ChannelAddress, msg.StatusRecord] = {}
        self._snapshot = StatusSnapshot(0, time.time_ns(), False, ())
        self._updated = asyncio.Condition()

        self._runs: dict[ChannelAddress, Run] = {}
        self._charge_ranges: dict[ChannelAddress, int | None] = {}
        self._eligible: set[str] = set()  # runs whose history is recorded
        self._starting: set[ChannelAddress] = set()  # start in progress
        self._awaiting: dict[ChannelAddress, ChannelProfile] = {}  # started, not yet observed
        self._exports: dict[str, asyncio.Task[None]] = {}
        self._dirty_exports: set[str] = set()
        self._resume_token: int | None = None
        self._resume_acknowledged = False
        self._resume_first: asyncio.Future[None] | None = None
        self._record_revision = 0
        self._record_versions: Mapping[str, RecordRevision] = MappingProxyType({})
        self._records_changed = asyncio.Condition()

    # ------------------------------------------------------------------ #
    # Properties and waiting
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._bound and self._writer is not None and not self._writer.is_closing()

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def snapshot(self) -> StatusSnapshot:
        """The current immutable snapshot, without copying its channel records."""
        return self._snapshot

    @property
    def statuses(self) -> Mapping[ChannelAddress, ChannelStatus]:
        return MappingProxyType(dict(self._statuses))

    @property
    def channels(self) -> tuple[ChannelAddress, ...]:
        """Main channels listed in the controller's metadata."""
        return tuple(sorted(ChannelAddress(*key) for key in self._session.channels))

    @property
    def maximum_status_age(self) -> float:
        return self.upload_interval_raw * self.missed_upload_intervals

    @property
    def listening_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise StateError("listener is not running")
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def exporting_channels(self) -> tuple[ChannelAddress, ...]:
        """Channels blocked by finalization, including the late-history grace period."""
        return tuple(run.address for run in self._runs.values() if run.id in self._exports)

    @property
    def record_revision(self) -> int:
        return self._record_revision

    @property
    def record_versions(self) -> Mapping[str, RecordRevision]:
        """Process-local revisions of retained runs; reset revisions mark backfills."""
        return self._record_versions

    async def wait_ready(self, timeout: float = 30) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)
        if not self.connected:
            raise ConnectionLost(
                "controller initialization failed or connection ended"
            ) from self._error

    async def wait_snapshot(
        self, *, after_revision: int = 0, timeout: float = 20
    ) -> StatusSnapshot:
        """Wait for a newer snapshot, coalescing updates for slow consumers."""
        bounded_integer("after_revision", after_revision, 2**63 - 1)
        async with asyncio.timeout(timeout), self._updated:
            while self._snapshot.revision <= after_revision:
                if self._closed:
                    raise ConnectionLost("client is closed")
                await self._updated.wait()
            return self._snapshot

    async def wait_status(
        self, address: ChannelAddress, *, after_ns: int = 0, timeout: float = 20
    ) -> ChannelStatus:
        async with asyncio.timeout(timeout), self._updated:
            while True:
                status = self._statuses.get(address)
                if status is not None and status.received_at_ns > after_ns:
                    return status
                if self._writer is None or self._writer.is_closing():
                    raise ConnectionLost("controller is disconnected")
                await self._updated.wait()

    async def wait_records(self, *, after_revision: int, timeout: float = 30) -> int:
        bounded_integer("after_revision", after_revision, 2**63 - 1)
        async with asyncio.timeout(timeout), self._records_changed:
            while self._record_revision <= after_revision:
                if self._closed:
                    raise ConnectionLost("client is closed")
                await self._records_changed.wait()
            return self._record_revision

    async def _notify(self) -> None:
        async with self._updated:
            self._updated.notify_all()

    def _publish_snapshot(self) -> None:
        self._snapshot = StatusSnapshot(
            self._snapshot.revision + 1,
            time.time_ns(),
            self.connected,
            tuple(self._statuses[address] for address in sorted(self._statuses)),
        )

    async def _notify_records(self, changes: tuple[RecordChange, ...] = ()) -> None:
        self._record_revision += 1
        active = {run.id for run in self._runs.values()}
        versions = {key: value for key, value in self._record_versions.items() if key in active}
        for key in active - versions.keys():
            versions[key] = RecordRevision(self._record_revision, self._record_revision)
        for change in changes:
            if change.run_id in versions:
                reset = (
                    self._record_revision
                    if change.backfill
                    else versions[change.run_id].reset_revision
                )
                versions[change.run_id] = RecordRevision(self._record_revision, reset)
        self._record_versions = MappingProxyType(versions)
        async with self._records_changed:
            self._records_changed.notify_all()

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "Client":
        await self.listen()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def listen(self) -> None:
        if self._closed or self._server is not None:
            raise StateError("client is closed or already listening")
        self._server = await asyncio.start_server(self._accept, self.host, self.port)
        logger.info(
            "Native listener started host=%s port=%d controller=%s",
            self.host,
            self.listening_port,
            self.controller_host,
        )

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self._closed or peer[0] != self.controller_host or self._writer is not None:
            logger.warning(
                "Controller connection rejected peer=%s closed=%s session_active=%s "
                "expected_host=%s",
                peer,
                self._closed,
                self._writer is not None,
                self.controller_host,
            )
            writer.close()
            return
        logger.info("Controller connected peer=%s", peer)
        self._writer = writer
        self._session = Session()
        self._statuses.clear()
        self._records.clear()
        self._eligible.clear()
        self._ready.clear()
        self._discovered.clear()
        self._bound = False
        self._error = None
        self._publish_snapshot()
        self._peer_task = asyncio.create_task(self._serve(reader, writer))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks = (asyncio.create_task(self._read(reader)), asyncio.create_task(self._initialize()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = error
            logger.exception("Controller session failed controller=%s", self.controller_host)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._pending is not None and not self._pending[2].done():
                self._pending[2].set_exception(ConnectionLost("controller disconnected"))
            if self._resume_first is not None and not self._resume_first.done():
                self._resume_first.set_exception(
                    ConnectionLost("controller disconnected during history recovery")
                )
            logger.info("Controller disconnected controller=%s", self.controller_host)
            self._bound = False
            self._eligible.clear()
            self._publish_snapshot()
            self._ready.set()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            if self._writer is writer:
                self._writer = None
            await self._notify()

    async def _initialize(self) -> None:
        """After metadata: read the controller GUID, bind storage, resume owned runs."""
        await asyncio.wait_for(self._discovered.wait(), 30)
        async with self._operation:
            reply = await self._exchange(
                self._session.request(Extended.GET_CONTROLLER_SERIAL, extended=True),
                write=False,
                reply_unit=None,
            )
            serial = msg.ControllerSerial.from_payload(self._payload(reply))
            await asyncio.to_thread(self.store.bind, serial.guid)
            saved = await asyncio.to_thread(self.store.runs)
            self._runs = {run.address: run for run in saved}
            for run in saved:
                self._charge_ranges[run.address] = await asyncio.to_thread(
                    self.store.charge_range, run.id
                )
            await self._notify_records()
            self._bound = True
            logger.info(
                "Controller ready controller=%s serial=%s channels=%d retained_runs=%d",
                self.controller_host,
                serial.serial_number,
                len(self.channels),
                len(saved),
            )
            for status in tuple(self._statuses.values()):
                await self._reconcile(status)
            for run in self._runs.values():
                if run.state == RunState.FINISHED:
                    self._schedule_export(run.id)
            self._ready.set()
            self._publish_snapshot()
            await self._notify()

    async def close(self) -> None:
        """Close local resources. This does not stop any controller measurement."""
        if self._closed:
            return
        self._closed = True
        async with self._records_changed:
            self._records_changed.notify_all()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._peer_task is not None:
            self._peer_task.cancel()
            await asyncio.gather(self._peer_task, return_exceptions=True)
        tasks = tuple(self._exports.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._bound = False
        self._publish_snapshot()
        await self._notify()
        await asyncio.to_thread(self.store.close)

    # ------------------------------------------------------------------ #
    # Frames in and out
    # ------------------------------------------------------------------ #
    async def _send(self, frame: Frame) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise ConnectionLost("controller is not connected")
        raw = frame.to_bytes()
        if wire_logger.isEnabledFor(logging.DEBUG):
            wire_logger.debug("tx %s %s", self.controller_host, raw.hex())
        logger.debug("tx %s", frame)
        writer.write(raw)
        await writer.drain()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        stream = FrameReader()
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), self.receive_timeout_s)
            except TimeoutError:
                raise ConnectionLost(
                    f"nothing received from the controller for {self.receive_timeout_s:g} s"
                ) from None
            if not chunk:
                break
            for frame in stream.feed(chunk):
                await self._receive(frame)
        stream.finish()
        raise ConnectionLost("controller closed the TCP connection")

    async def _receive(self, frame: Frame) -> None:
        if wire_logger.isEnabledFor(logging.DEBUG):
            wire_logger.debug("rx %s %s", self.controller_host, frame.to_bytes().hex())
        for reply in self._session.receive(frame):
            await self._send(reply)
        if self._session.info is not None:
            self._discovered.set()
        if frame.reply:
            await self._receive_reply(frame)
        elif frame.key == (True, Extended.HISTORY, False):
            await self._receive_history(frame)
        elif frame.key == (True, Extended.STATUS, False):
            await self._receive_status(frame)
        elif (
            frame.extended,
            frame.command,
        ) in ACKNOWLEDGED_UNHANDLED and frame.key not in self._warned:
            self._warned.add(frame.key)
            logger.warning(
                "Unacknowledged controller upload %s; the manufacturer host replies "
                "to it but this library does not",
                frame.name,
            )
        await self._notify()

    async def _receive_reply(self, frame: Frame) -> None:
        pending = self._pending
        if pending is None or frame.key != pending[0] or pending[2].done():
            raise ProtocolError(f"unsolicited or duplicate reply: {frame}")
        if pending[1] is not None and frame.unit != pending[1]:
            raise ProtocolError(f"reply from unit {frame.unit}, expected {pending[1]}: {frame}")
        try:
            if self._on_reply is not None:
                await self._on_reply(frame)
        except Exception as error:  # fail the exchange, not the connection's reader
            pending[2].set_exception(error)
            return
        pending[2].set_result(frame)

    async def _exchange(
        self,
        request: Frame,
        *,
        write: bool = True,
        reply_unit: int | None = SAME_UNIT,
        on_reply: Callable[[Frame], Awaitable[None]] | None = None,
    ) -> Frame:
        """Send one request and wait for its reply.

        `reply_unit` None accepts a reply from any unit. `on_reply` runs before the reply is
        delivered, while no other frame is processed.
        """
        if self._pending is not None:
            raise StateError("another exchange is pending")
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        expected_unit = request.unit if reply_unit == SAME_UNIT else reply_unit
        self._pending = (request.extended, request.command, True), expected_unit, future
        self._on_reply = on_reply
        try:
            await self._send(request)
            return await asyncio.wait_for(future, REPLY_TIMEOUT)
        except BaseException as error:
            logger.warning(
                "Exchange failed controller=%s request=%s outcome=%s error=%s",
                self.controller_host,
                request.name,
                "uncertain" if write else "read_failed",
                type(error).__name__,
                exc_info=not isinstance(error, asyncio.CancelledError),
            )
            self._disconnect()
            if isinstance(error, asyncio.CancelledError):
                raise
            if write:
                raise OperationUncertain(
                    "write outcome is uncertain; no automatic retry"
                ) from error
            raise ConnectionLost("read-only exchange failed") from error
        finally:
            if not future.done():
                future.cancel()
            self._pending = None
            self._on_reply = None

    def _disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()

    @staticmethod
    def _payload(reply: Frame, size: int | None = None) -> bytes:
        if reply.result:
            raise OperationRejected(f"controller rejected {reply.name}: result {reply.result}")
        if size is not None and len(reply.payload) != size:
            raise ProtocolError(f"{reply.name}: expected {size} bytes, got {len(reply.payload)}")
        return reply.payload

    # ------------------------------------------------------------------ #
    # Status and history uploads
    # ------------------------------------------------------------------ #
    async def _receive_status(self, frame: Frame) -> None:
        upload = msg.StatusUpload.from_payload(frame.payload)
        for record in upload.records:
            if record.aux:
                continue
            address = ChannelAddress(frame.unit, record.channel)
            values = readings(
                record, self._charge_ranges.get(address), overloaded_energy(record.alarm_code)
            )
            status = ChannelStatus(
                address,
                record.test_id,
                values.state,
                record.work_type,
                record.step_type,
                record.step_id,
                record.cycle,
                record.alarm_code,
                values.voltage_v,
                values.current_a,
                values.temperature_c,
                values.total_elapsed_s,
                values.step_elapsed_s,
                time.time_ns(),
                time.monotonic(),
                record.raw,
            )
            previous = self._statuses.get(address)
            if previous is None or (previous.state, previous.test_id, previous.alarm_code) != (
                status.state,
                status.test_id,
                status.alarm_code,
            ):
                logger.log(
                    logging.WARNING if status.alarm_code else logging.INFO,
                    "Channel status changed channel=%s test_id=%d state=%s alarm=%d",
                    address,
                    status.test_id,
                    status.state,
                    status.alarm_code,
                )
            self._statuses[address] = status
            self._records[address] = record
            if self._bound:
                await self._reconcile(status)
        self._publish_snapshot()

    async def _receive_history(self, frame: Frame) -> None:
        """Store owned records durably, then ACK. Unbound or unreconciled batches are not ACKed."""
        batch = msg.HistoryBatch.from_payload(frame.payload)
        if not self._bound or any(address not in self._statuses for address in self._runs):
            return
        if self._resume_token is not None and (
            not self._resume_acknowledged or batch.token != self._resume_token
        ):
            return
        for record in batch.records:
            run = self._runs.get(ChannelAddress(record.unit, record.channel))
            if (
                run is not None
                and run.id in self._eligible
                and run.test_id == record.test_id
                and run.state == RunState.FINISHED
            ):
                self._schedule_export(run.id)
        changed = await asyncio.to_thread(self.store.append, batch, tuple(self._eligible))
        if changed:
            await self._notify_records(changed)
        await asyncio.to_thread(self.store.save_checkpoint, batch.token)
        await self._send(frame.answer(msg.HistoryBatch.ack_payload(batch.token), unit=0))
        if self._resume_token is not None:
            self._resume_token = None
            if self._resume_first is not None and not self._resume_first.done():
                self._resume_first.set_result(None)

    async def _reconcile(self, status: ChannelStatus) -> None:
        run = self._runs.get(status.address)
        if run is None:
            return
        if status.test_id != run.test_id:
            if status.address in self._starting:
                return
            self._eligible.discard(run.id)
            if run.state != RunState.INTERRUPTED:
                logger.warning(
                    "Run identity changed channel=%s run_id=%s owned_test_id=%d "
                    "observed_test_id=%d; recording interrupted",
                    status.address,
                    run.id,
                    run.test_id,
                    status.test_id,
                )
                await asyncio.to_thread(self.store.set_state, run.id, RunState.INTERRUPTED)
                self._runs[status.address] = await asyncio.to_thread(self.store.get, run.id)
            return
        self._eligible.add(run.id)
        self._awaiting.pop(status.address, None)
        if status.state == ChannelState.FINISHED and run.state != RunState.FINISHED:
            await asyncio.to_thread(self.store.set_state, run.id, RunState.FINISHED)
            self._runs[status.address] = await asyncio.to_thread(self.store.get, run.id)
            self._schedule_export(run.id)

    def _schedule_export(self, run_id: str) -> None:
        self._dirty_exports.add(run_id)
        if run_id not in self._exports:
            self._exports[run_id] = asyncio.create_task(self._export_finished(run_id))

    async def _export_finished(self, run_id: str) -> None:
        """Finalize after `history_grace_period_s` without new history for the run."""
        try:
            while run_id in self._dirty_exports:
                self._dirty_exports.discard(run_id)
                await asyncio.sleep(self.history_grace_period_s)
                if run_id in self._dirty_exports:
                    continue
                conversion = asyncio.create_task(asyncio.to_thread(self.store.finalize, run_id))
                try:
                    await asyncio.shield(conversion)
                except asyncio.CancelledError:
                    await conversion  # let the publishing thread finish before closing
                    raise
                except PendingRecordTime:
                    logger.debug("BDF export awaits adjacent history for %s", run_id)
        except Exception as error:
            self._error = error
            logger.exception("Automatic BDF export failed run_id=%s", run_id)
        finally:
            self._dirty_exports.discard(run_id)
            self._exports.pop(run_id, None)

    # ------------------------------------------------------------------ #
    # Preconditions
    # ------------------------------------------------------------------ #
    def _latest(self, address: ChannelAddress) -> ChannelStatus:
        if not self.connected:
            raise ConnectionLost("no ready controller session")
        status = self._statuses.get(address)
        if status is None:
            raise StateError("channel has not reported in this controller session")
        return status

    def _fresh(self, address: ChannelAddress) -> ChannelStatus:
        status = self._latest(address)
        if status.age_s > self.maximum_status_age:
            raise StateError(
                f"channel has missed {self.missed_upload_intervals:g} upload intervals"
            )
        return status

    def check_start(self, profile: ChannelProfile) -> None:
        """Check start preconditions without changing data or sending anything."""
        if profile.address in self.exporting_channels:
            raise StateError("final export in progress: waiting for late history or writing BDF")
        if profile.address in self._awaiting:
            raise StateError("channel start is awaiting status confirmation")
        if any(p.physical_id == profile.physical_id for p in self._awaiting.values()):
            raise StateError("physical channel is reserved by a start awaiting confirmation")
        if profile.address not in self.channels:
            raise StateError("channel is not in the controller's channel metadata")
        if self._fresh(profile.address).state != ChannelState.FINISHED:
            raise StateError("start requires a finished channel")
        for address, other in self._statuses.items():
            if address == profile.address or other.state == ChannelState.FINISHED:
                continue
            run = self._runs.get(address)
            configured = self._configured.get(address)
            physical = (
                run.physical_id
                if run is not None and run.test_id == other.test_id
                else configured.physical_id
                if configured is not None
                else None
            )
            if physical is None:
                raise StateError("physical mapping of another active channel is not known")
            self._fresh(address)
            if physical == profile.physical_id:
                raise StateError("physical channel is already assigned to another active run")
            if other.alarm_code or other.state not in (ChannelState.RUNNING, ChannelState.STOPPED):
                raise StateError("another active channel has an unresolved state")

    def _check_idle(self) -> None:
        """All discovered channels freshly observed finished and alarm-free."""
        self._check_bound()
        if self._awaiting:
            raise StateError("channel starts are awaiting status confirmation")
        if not self.channels:
            raise StateError("complete channel discovery is required")
        for address in self.channels:
            status = self._fresh(address)
            if status.state != ChannelState.FINISHED or status.alarm_code:
                raise StateError("all discovered channels must be finished and alarm-free")

    def _check_bound(self) -> None:
        if not self.connected:
            raise ConnectionLost("no ready controller session")
        if self._error is not None:
            raise StateError("resolve the recorder error before controller operations")

    # ------------------------------------------------------------------ #
    # Starting tests
    # ------------------------------------------------------------------ #
    async def start(
        self,
        recipe: Recipe,
        profile: ChannelProfile,
        *,
        run_id: str | None = None,
        on_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> Run:
        """Start one channel. See start_many."""
        runs = await self.start_many(
            recipe, (profile,), run_ids=None if run_id is None else (run_id,), on_attempt=on_attempt
        )
        return runs[0]

    async def start_many(
        self,
        recipe: Recipe,
        profiles: tuple[ChannelProfile, ...],
        *,
        run_ids: tuple[str, ...] | None = None,
        on_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[Run, ...]:
        """Start channels of one unit with one identical program.

        Sequence (all replies checked before the next step): channel map, aux init, program
        download, per-test protections, global protections, Start (arms channels and assigns
        TestIDs), StartConfirm (starts them). Returned runs carry the acknowledged TestIDs;
        confirming the running state from status uploads is the caller's responsibility.
        """
        if not profiles or len({p.address for p in profiles}) != len(profiles):
            raise ValueError("batch requires distinct channel addresses")
        if len({p.address.unit for p in profiles}) != 1:
            raise ValueError("a masked start requires channels in the same unit")
        if len({p.physical_id for p in profiles}) != len(profiles):
            raise StateError("batch channels must have distinct physical mappings")
        ids: tuple[str | None, ...] = (
            (None,) * len(profiles)
            if run_ids is None
            else tuple(UUID(value).hex for value in run_ids)
        )
        if len(ids) != len(profiles) or (run_ids is not None and len(set(ids)) != len(ids)):
            raise ValueError("batch requires one unique run ID per channel")
        programs = await asyncio.gather(*(asyncio.to_thread(recipe.compile, p) for p in profiles))
        if any(program.native != programs[0].native for program in programs[1:]):
            raise ValueError("masked starts require identical compiled programs")

        def check() -> None:
            for profile in profiles:
                self.check_start(profile)

        async with self._operation:
            check()
            if on_attempt is not None:
                await on_attempt()
            for profile in profiles:
                await self._discard_data(profile.address)
            self._starting = {p.address for p in profiles}
            sent: list[bool] = []  # appended to before the first frame goes out
            try:
                runs = await self._start_sequence(
                    recipe, programs[0].native, programs[0].step_count, profiles, ids, check, sent
                )
                logger.info(
                    "Native start acknowledged channel=%s run_id=%s test_id=%s",
                    profiles[0].address,
                    tuple(r.id for r in runs),
                    tuple(r.test_id for r in runs),
                )
                return runs
            except BaseException as error:
                logger.warning(
                    "Native start failed channel=%s run_id=%s error=%s",
                    profiles[0].address,
                    ids,
                    type(error).__name__,
                )
                if isinstance(error, OperationRejected):
                    for profile in profiles:
                        await self._discard_data(profile.address)
                if sent:
                    self._disconnect()
                if not sent or isinstance(
                    error, (asyncio.CancelledError, OperationRejected, OperationUncertain)
                ):
                    raise
                raise OperationUncertain(
                    "start did not complete; reconcile before another operation"
                ) from error
            finally:
                self._starting = set()

    async def _start_sequence(
        self,
        recipe: Recipe,
        program: bytes,
        step_count: int,
        profiles: tuple[ChannelProfile, ...],
        ids: tuple[str | None, ...],
        check: Callable[[], None],
        sent: list[bool],
    ) -> tuple[Run, ...]:
        session = self._session
        unit = profiles[0].address.unit
        guid = uuid4().bytes_le
        targets = (msg.Target.of(unit, [p.address.channel for p in profiles]),)
        wanted = sorted((unit, p.address.channel) for p in profiles)

        async def exchange(
            command: int,
            payload: bytes,
            *,
            extended: bool = True,
            to: int = unit,
            on_reply: Callable[[Frame], Awaitable[None]] | None = None,
        ) -> bytes:
            check()
            logger.debug("Start phase channel=%s command=0x%04x", profiles[0].address, command)
            sent.append(True)
            reply = await self._exchange(
                session.request(command, payload, unit=to, extended=extended), on_reply=on_reply
            )
            return self._payload(reply)

        entries = tuple(msg.MapEntry(unit, p.address.channel, 0, p.physical_id) for p in profiles)
        if (
            await exchange(
                Short.SET_CHANNEL_MAP, msg.ChannelMap(guid, entries).to_payload(), extended=False
            )
            != guid
        ):
            raise ProtocolError("channel map reply does not echo the GUID")
        self._payload_empty(
            await exchange(Short.SET_AUX, msg.AuxInit().to_payload(), extended=False), "SetBgsAux"
        )

        fragments = msg.split_program(program, split_records(program))
        for index, fragment in enumerate(fragments, 1):
            request = msg.StepDownload(
                guid, targets, len(program), step_count, index == len(fragments), index, fragment
            )
            reply = msg.StepDownloadReply.from_payload(
                await exchange(Extended.DOWNLOAD_STEPS, request.to_payload())
            )
            if (reply.guid, reply.targets, reply.final, reply.index) != (
                guid,
                targets,
                request.final,
                index,
            ):
                raise ProtocolError(f"download fragment {index} acknowledged incorrectly")

        test_protection = msg.TestProtection(guid, targets)
        if (
            await exchange(Extended.SET_TEST_PROTECTION, test_protection.to_payload())
            != test_protection.reply_payload()
        ):
            raise ProtocolError("test protection reply does not echo GUID and targets")
        self._payload_empty(
            await exchange(
                Extended.SET_GLOBAL_PROTECTION,
                msg.GlobalProtection.pan_error_protection().to_payload(),
                to=0,
            ),
            "SetGrobalProtect82",
        )

        runs: list[Run] = []

        async def record_runs(reply: Frame) -> None:
            # Runs exist before any status of the new TestID can be processed.
            if reply.result:
                return
            started = msg.StartReply.from_payload(reply.payload, reply.result)
            if (
                started.guid != guid
                or sorted((c.unit, c.channel) for c in started.channels) != wanted
            ):
                raise ProtocolError("Start reply does not match the requested channels")
            assigned = {(c.unit, c.channel): c.test_id for c in started.channels}
            for profile, run_id in zip(profiles, ids, strict=True):
                run = await asyncio.to_thread(
                    self.store.begin,
                    profile,
                    assigned[profile.address.unit, profile.address.channel],
                    recipe.xml,
                    run_id,
                )
                runs.append(run)
                self._runs[profile.address] = run
                self._awaiting[profile.address] = profile
                self._charge_ranges[profile.address] = profile.charge_range_raw
                self._eligible.add(run.id)
            await self._notify_records()

        await exchange(Extended.START, msg.Start(guid, targets).to_payload(), on_reply=record_runs)
        assigned = tuple(
            msg.ChannelTestId(r.address.unit, r.address.channel, r.test_id) for r in runs
        )
        confirm = msg.ConfirmStartReply.from_payload(
            await self._payload_after(
                Extended.CONFIRM_START, msg.ConfirmStart(guid, assigned).to_payload(), unit
            )
        )
        if confirm.guid != guid or sorted((c.unit, c.channel) for c in confirm.channels) != wanted:
            raise ProtocolError("StartConfirm reply does not match the requested channels")
        failed = [c for c in confirm.channels if c.result]
        if failed:
            raise OperationRejected(f"StartConfirm rejected channels: {failed}")
        return tuple(runs)

    async def _payload_after(self, command: int, payload: bytes, unit: int) -> bytes:
        """Exchange without re-checking start preconditions (the channel is now armed)."""
        reply = await self._exchange(
            self._session.request(command, payload, unit=unit, extended=True)
        )
        return self._payload(reply)

    @staticmethod
    def _payload_empty(payload: bytes, what: str) -> None:
        if payload:
            raise ProtocolError(f"{what} reply has an unexpected payload")

    async def _discard_data(self, address: ChannelAddress) -> None:
        self._awaiting.pop(address, None)
        previous = self._runs.pop(address, None)
        if previous is not None:
            task = self._exports.get(previous.id)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._eligible.discard(previous.id)
            self._dirty_exports.discard(previous.id)
        self._charge_ranges.pop(address, None)
        await asyncio.to_thread(self.store.clear, address)
        await self._notify_records()

    async def discard_data(self, address: ChannelAddress) -> None:
        """Discard retained data and ownership; this does not stop a physical measurement."""
        async with self._operation:
            await self._discard_data(address)

    # ------------------------------------------------------------------ #
    # Channel control
    # ------------------------------------------------------------------ #
    async def control(
        self,
        address: ChannelAddress,
        operation: Control,
        *,
        expected_test_id: int,
        destination_step: int | None = None,
    ) -> None:
        """Stop, Continue, Clear Flag (reset) or Jump one channel.

        Completes when the controller acknowledges and, except for Clear Flag, a later status
        of the same TestID shows the effect. A missing acknowledgement or effect raises
        OperationUncertain and closes the connection.
        """
        operation = Control(operation)
        logger.info(
            "Control requested channel=%s test_id=%d operation=%s destination_step=%s",
            address,
            expected_test_id,
            operation,
            destination_step,
        )
        if operation == Control.JUMP:
            if destination_step is None:
                raise ValueError("Jump requires a destination step")
            bounded_integer("destination_step", destination_step, 255, 1)
        elif destination_step is not None:
            raise ValueError("destination step only applies to Jump")
        async with self._operation:
            if address in self._awaiting:
                raise StateError("channel start is awaiting status confirmation")
            status = self._latest(address)
            if status.test_id != expected_test_id:
                raise StateError("channel TestID changed")
            self._check_control(operation, status, destination_step)
            command, extended = CONTROLS[operation]
            selection = msg.Selection(channel_mask([address.channel]), destination_step)
            reply = await self._exchange(
                self._session.request(
                    command, selection.to_payload(), unit=address.unit, extended=extended
                )
            )
            observed = status
            try:
                if reply.result:
                    if reply.payload not in (b"", selection.to_payload()[:8]):
                        raise ProtocolError(f"rejection names other channels: {reply}")
                    raise OperationRejected(f"{operation.value} rejected: result {reply.result}")
                if reply.payload != selection.to_payload()[:8]:
                    raise ProtocolError(f"control reply names other channels: {reply}")
                if operation == Control.CLEAR_FLAG:
                    logger.info(
                        "Clear Flag acknowledged channel=%s test_id=%d", address, expected_test_id
                    )
                    return
                async with asyncio.timeout(CONFIRM_TIMEOUT):
                    while True:
                        observed = await self.wait_status(address, after_ns=observed.received_at_ns)
                        if observed.test_id != expected_test_id:
                            raise StateError("TestID changed during control")
                        if self._control_done(operation, observed, destination_step):
                            logger.info(
                                "Control confirmed channel=%s operation=%s state=%s",
                                address,
                                operation,
                                observed.state,
                            )
                            return
            except OperationRejected:
                logger.warning(
                    "Control rejected controller=%s channel=%s operation=%s",
                    self.controller_host,
                    address,
                    operation,
                )
                raise
            except BaseException as error:
                logger.error(
                    "Control outcome uncertain controller=%s channel=%s operation=%s "
                    "last_state_raw=%d last_alarm=%d",
                    self.controller_host,
                    address,
                    operation,
                    observed.state_raw,
                    observed.alarm_code,
                    exc_info=True,
                )
                self._disconnect()
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise OperationUncertain("control effect could not be confirmed") from error

    @staticmethod
    def _check_control(operation: Control, status: ChannelStatus, step: int | None) -> None:
        if operation == Control.CLEAR_FLAG:
            if status.state not in CLEARABLE:
                raise StateError(f"Clear Flag does not apply to a {status.state} channel")
            return
        required = ChannelState.STOPPED if operation == Control.CONTINUE else ChannelState.RUNNING
        if status.state != required or status.alarm_code:
            raise StateError(f"{operation.value} requires a {required} channel without alarm")
        if operation == Control.JUMP and status.step_id == step:
            raise StateError("Jump destination is already active; the jump cannot be confirmed")

    @staticmethod
    def _control_done(operation: Control, status: ChannelStatus, step: int | None) -> bool:
        if status.alarm_code:
            return False
        if operation == Control.JUMP:
            return status.step_id == step and (
                status.state == ChannelState.RUNNING
                or (status.state == ChannelState.FINISHED and status.step_type == StepType.END)
            )
        target = ChannelState.STOPPED if operation == Control.STOP else ChannelState.RUNNING
        return status.state == target

    # ------------------------------------------------------------------ #
    # History recovery
    # ------------------------------------------------------------------ #
    async def resume_history(self, *, index: int | None = None) -> None:
        """Ask the controller to resend history from the saved token (or `index`).

        The token is used as is: the controller resends that batch again and duplicates are
        dropped by sequence. No arithmetic, no retries.
        """
        logger.info(
            "History resumption requested controller=%s index=%s", self.controller_host, index
        )
        async with self._operation:
            if not self.connected:
                raise ConnectionLost("no ready controller")
            if not self._eligible:
                raise StateError("a matching library-started run is required first")
            selected = await asyncio.to_thread(self.store.checkpoint) if index is None else index
            if selected is None:
                raise StateError("no saved history token; supply an index")
            bounded_integer("history index", selected, 2**32 - 1)
            self._resume_token, self._resume_acknowledged = selected, False
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._resume_first = future

            async def accepted(reply: Frame) -> None:
                self._resume_acknowledged = reply.result == 0

            try:
                reply = await self._exchange(
                    self._session.request(
                        Extended.RESEND_HISTORY, selected.to_bytes(4, "little"), extended=True
                    ),
                    reply_unit=None,
                    on_reply=accepted,
                )
                self._payload(reply, 0)
                await asyncio.wait_for(future, REPLY_TIMEOUT)
            except BaseException:
                self._disconnect()
                raise
            finally:
                if not future.done():
                    future.cancel()
                self._resume_first = None
                self._resume_token = None
                self._resume_acknowledged = False

    async def history_minimum_index(self) -> int:
        """The oldest history position the controller can still resend (GetDfMinIndex)."""
        async with self._operation:
            self._check_bound()
            reply = await self._exchange(
                self._session.request(Extended.GET_HISTORY_MIN_INDEX, extended=True),
                write=False,
                reply_unit=None,
            )
            return int.from_bytes(self._payload(reply, 4), "little")

    # ------------------------------------------------------------------ #
    # Controller information and settings
    # ------------------------------------------------------------------ #
    async def _query(
        self, command: int, unit: int, *, extended: bool, reply_unit: int | None = SAME_UNIT
    ) -> bytes:
        reply = await self._exchange(
            self._session.request(command, unit=unit, extended=extended),
            write=False,
            reply_unit=reply_unit,
        )
        return self._payload(reply)

    async def controller_info(self) -> ControllerInfo:
        """Read the controller's serial number, firmware version and RTC status."""
        async with self._operation:
            self._check_bound()
            serial = msg.ControllerSerial.from_payload(
                await self._query(Extended.GET_CONTROLLER_SERIAL, 0, extended=True, reply_unit=None)
            )
            version = await self._query(
                Short.GET_CONTROLLER_VERSION, CONTROLLER_UNIT, extended=False
            )
            rtc = await self._query(Extended.GET_RTC_STATUS, 0, extended=True)
            if len(rtc) != 1:
                raise ProtocolError("RTC status reply must be one byte")
            return ControllerInfo(
                serial.guid,
                serial.serial_number,
                serial.production_time.to_datetime(),
                _controller_version(version),
                rtc[0],
                serial.raw,
                version,
            )

    async def unit_info(self, unit: int) -> UnitInfo:
        """Read the serial number and firmware version of a lower unit."""
        bounded_integer("unit", unit, 32, 1)
        async with self._operation:
            self._check_bound()
            if not any(address.unit == unit for address in self.channels):
                raise StateError("unit is not in the controller's channel metadata")
            serial = msg.UnitSerial.from_payload(
                await self._query(Extended.GET_UNIT_SERIAL, unit, extended=True)
            )
            version = await self._query(Short.GET_UNIT_VERSION, unit, extended=False)
            if len(version) != 30:
                raise ProtocolError("unit version reply must be 30 bytes")
            return UnitInfo(
                unit,
                serial.guid,
                serial.serial_number,
                serial.production_time.to_datetime(),
                serial.valid_until.to_datetime(),
                serial.latest_calibration.to_datetime(),
                msg.version_text(version),
                serial.raw,
                version,
            )

    async def _setting(
        self, command: int, payload: bytes, reply_unit: int | None, reply_size: int
    ) -> bytes:
        reply = await self._exchange(self._session.request(command, payload), reply_unit=reply_unit)
        if reply.result:
            raise OperationRejected(f"controller rejected {reply.name}: result {reply.result}")
        if len(reply.payload) != reply_size:
            self._disconnect()
            raise OperationUncertain(f"invalid {reply.name}; no automatic retry")
        return reply.payload

    async def set_upload_interval(self, interval_raw: int) -> None:
        """Set the controller-wide status upload interval in seconds (1..255).

        This does not change recipe recording. Freshness checks use it after the ACK.
        """
        bounded_integer("interval_raw", interval_raw, 255, 1)
        logger.info(
            "Upload interval requested controller=%s interval_raw=%d",
            self.controller_host,
            interval_raw,
        )
        async with self._operation:
            self._check_bound()
            await self._setting(
                Short.SET_UPLOAD_INTERVAL, bytes([interval_raw]), CONTROLLER_UNIT, 0
            )
            self.upload_interval_raw = interval_raw

    async def set_clock(self, local_time: datetime) -> None:
        """Set the controller clock to a naive local time (2000..2099, ms resolution).

        All discovered channels must be freshly observed finished and alarm-free.
        """
        payload = msg.rtc_payload(local_time)
        logger.info(
            "Clock setting requested controller=%s local_time=%s", self.controller_host, local_time
        )
        async with self._operation:
            self._check_idle()
            await self._setting(Short.SET_RTC, payload, CONTROLLER_UNIT, 0)

    async def initialize_test_id(self, seed: int, *, source: str) -> int:
        """Set the device-wide TestID counter; all discovered channels must be idle.

        `source` must describe the complete device-wide inventory establishing the maximum.
        """
        bounded_integer("seed", seed, 0xFFFFFFFF)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must describe the verified device-wide maximum TestID")
        async with self._operation:
            self._check_idle()
            known = [s.test_id for s in self._statuses.values()]
            known += [run.test_id for run in await asyncio.to_thread(self.store.runs)]
            if known and seed < max(known):
                raise ValueError("seed is below a known TestID")
            logger.info("Explicit TestID initialization seed=%d source=%s", seed, source)
            payload = await self._setting(Short.SET_TEST_ID, seed.to_bytes(4, "little"), None, 4)
            reported = int.from_bytes(payload, "little")
            if reported != seed:
                self._disconnect()
                raise OperationUncertain("reported TestID differs from the requested seed")
            return reported

    # ------------------------------------------------------------------ #
    # Recorded data
    # ------------------------------------------------------------------ #
    async def runs(self) -> tuple[Run, ...]:
        return await asyncio.to_thread(self.store.runs)

    async def read(
        self, run: Run, *, after_sequence: int = 0, limit: int = 4096
    ) -> tuple[RecordedPoint, ...]:
        return await asyncio.to_thread(
            self.store.read, run.id, after_sequence=after_sequence, limit=limit
        )

    async def export(self, run: Run, path: str | Path | None = None) -> Export:
        return await asyncio.to_thread(self.store.export, run.id, path)


def _controller_version(payload: bytes) -> str:
    """The controller's version reply may append a length-prefixed trailer after 30 bytes."""
    if len(payload) != 30 and not (len(payload) >= 31 and len(payload) == 31 + payload[30]):
        raise ProtocolError("unexpected controller version reply length")
    return msg.version_text(payload)
