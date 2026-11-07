"""The client against a simulated controller over real TCP."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

import neware8.client
from neware8 import (
    ChannelAddress,
    ChannelState,
    Client,
    Control,
    OperationRejected,
    OperationUncertain,
    ProtocolError,
    Recipe,
    StateError,
    StorageError,
)
from neware8.messages import HISTORY_FILE_SHIFT
from neware8.protocol import Extended, Frame, Short
from fake_controller import FakeController, HistoryLog
from helpers import PROFILE, XML, MemorySink

ADDRESS = PROFILE.address


@asynccontextmanager
async def connected(tmp_path, **options):
    client = Client(
        host="127.0.0.1",
        controller_host="127.0.0.1",
        history=MemorySink(),
        port=0,
        upload_interval_raw=1,
    )
    await client.listen()
    fake = FakeController(client.listening_port, **options)
    await fake.connect()
    try:
        await client.wait_ready(5)
        while len(client.snapshot.channels) < 8:
            await client.wait_snapshot(after_revision=client.snapshot.revision, timeout=5)
        yield client, fake
    finally:
        await fake.close()
        await client.close()


async def wait_state(client, state, *, step=None):
    async with asyncio.timeout(5):
        while True:
            status = await client.wait_status(ADDRESS, after_ns=0)
            if status.state == state and (step is None or status.step_id == step):
                return status
            await client.wait_status(ADDRESS, after_ns=status.received_at_ns)


def run(coroutine):
    asyncio.run(coroutine)


def test_connect_discovers_channels_and_binds_the_sink(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            assert client.connected
            assert client.channels == tuple(ChannelAddress(1, c) for c in range(1, 9))
            status = client.snapshot.get(ADDRESS)
            assert status.state == ChannelState.FINISHED and status.voltage_v == 3.6
            assert status.current_a == pytest.approx(-0.001)
            assert client.history.controller == bytes(range(16))

    run(check())


def test_start_follows_the_manufacturer_sequence(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            requests = [(f.extended, f.command) for f in fake.received if not f.reply]
            assert requests == [
                (True, Extended.GET_CONTROLLER_SERIAL),
                (False, Short.SET_CHANNEL_MAP),
                (False, Short.SET_AUX),
                (True, Extended.DOWNLOAD_STEPS),
                (True, Extended.SET_TEST_PROTECTION),
                (True, Extended.SET_GLOBAL_PROTECTION),
                (True, Extended.START),
                (True, Extended.CONFIRM_START),
            ]
            assert started.test_id == fake.channels[1, 1].test_id
            assert fake.program == Recipe(XML).compile(PROFILE).native
            await wait_state(client, ChannelState.RUNNING)
            assert list(client.history.tests.values()) == [started]
            with pytest.raises(StateError, match="finished channel"):
                client.check_start(PROFILE)

    run(check())


def test_rejected_start_registers_no_test(tmp_path):
    async def check():
        async with connected(tmp_path, reject={(True, Extended.START): 1}) as (client, fake):
            with pytest.raises(OperationRejected):
                await client.start(Recipe(XML), PROFILE)
            assert client.history.tests == {}
            assert not fake.channels[1, 1].armed

    run(check())


def test_missing_reply_is_uncertain_and_never_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(neware8.client, "REPLY_TIMEOUT", 0.3)

    async def check():
        async with connected(tmp_path, silent={(True, Extended.START)}) as (client, fake):
            with pytest.raises(OperationUncertain):
                await client.start(Recipe(XML), PROFILE)
            assert len(fake.sent(True, Extended.START)) == 1
            assert len(fake.sent(True, Extended.CONFIRM_START)) == 0
            await asyncio.sleep(0.1)
            assert not client.connected

    run(check())


def test_controls_wait_for_the_state_change(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            test_id = started.test_id
            await client.control(ADDRESS, Control.STOP, expected_test_id=test_id)
            assert client.snapshot.get(ADDRESS).state == ChannelState.STOPPED
            with pytest.raises(StateError):
                await client.control(ADDRESS, Control.STOP, expected_test_id=test_id)
            await client.control(ADDRESS, Control.CONTINUE, expected_test_id=test_id)
            await client.control(
                ADDRESS, Control.JUMP, expected_test_id=test_id, destination_step=3
            )
            assert client.snapshot.get(ADDRESS).step_id == 3
            fake.finish(1, 1)
            await wait_state(client, ChannelState.FINISHED)
            await client.control(ADDRESS, Control.CLEAR_FLAG, expected_test_id=test_id)
            with pytest.raises(StateError, match="TestID changed"):
                await client.control(ADDRESS, Control.CLEAR_FLAG, expected_test_id=test_id + 1)

    run(check())


def test_rejected_stop_with_empty_reply(tmp_path):
    async def check():
        async with connected(tmp_path, reject={(False, Short.STOP): 1}) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            with pytest.raises(OperationRejected):
                await client.control(ADDRESS, Control.STOP, expected_test_id=started.test_id)
            assert client.connected

    run(check())


def test_history_is_committed_before_it_is_acknowledged(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            fake.history(
                77,
                [
                    fake.history_record(1, 1, 1),
                    fake.history_record(1, 1, 2),
                    fake.history_record(1, 2, 1, test_id=12345),
                ],
            )
            async with asyncio.timeout(5):
                while 77 not in fake.acks:
                    await asyncio.sleep(0.01)
            points = client.history.points(started)
            assert [p.sequence for p in points] == [1, 2]
            assert points[0].voltage_volt == 3.6
            assert await client.history.checkpoint() == 77

    run(check())


def test_refused_commit_is_never_acknowledged(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            client.history.refuse = StorageError("disk full")
            fake.history(77, [fake.history_record(1, 1, 1)])
            await until(lambda: not client.connected)
            assert 77 not in fake.acks
            assert isinstance(client.error, StorageError)

    run(check())


def test_settings_and_information(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            await client.set_upload_interval(2)
            assert fake.interval_raw == 2 and client.maximum_status_age == 6
            await client.set_clock(datetime(2026, 10, 20, 12, 0, 0))
            assert (
                await client.initialize_test_id(0xA8003000, source="test inventory") == 0xA8003000
            )
            info = await client.controller_info()
            assert (info.serial_number, info.version, info.rtc_status_raw) == (
                "SIM000000001",
                "SIM_1.0",
                0,
            )
            unit = await client.unit_info(1)
            assert (unit.serial_number, unit.version) == ("UNIT00000001", "UNIT_1.0")
            assert await client.history_minimum_index() == 100
            started = await client.start(Recipe(XML), PROFILE)
            assert started.test_id == 0xA8003001
            await wait_state(client, ChannelState.RUNNING)
            with pytest.raises(StateError, match="finished and alarm-free"):
                await client.set_clock(datetime(2026, 10, 20, 12, 0, 0))

    run(check())


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def reconnect(client, first, log=None):
    """Drop `first` and connect a controller with the same channels and history log."""
    await first.close()
    await until(lambda: client._writer is None)
    second = FakeController(client.listening_port, log=first.log if log is None else log)
    second.channels = first.channels
    await second.connect()
    await client.wait_ready(5)
    return second


async def sequences(client, test):
    return client.history.sequences(test)


def test_resume_history_waits_for_the_requested_batch(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            fake.record([fake.history_record(1, 1, s) for s in (1, 2, 3)])
            token = fake.log.token(2)
            await until(lambda: token in fake.acks)
            await client.resume_history(index=token)
            assert fake.sent(True, Extended.RESEND_HISTORY)[0].payload == token.to_bytes(
                4, "little"
            )
            await until(lambda: fake.acks.count(token) == 2)

    run(check())


def test_explicit_resume_requires_the_requested_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(neware8.client, "REPLY_TIMEOUT", 0.3)

    async def check():
        async with connected(tmp_path) as (client, fake):
            await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            fake.record([fake.history_record(1, 1, 1)])
            await until(lambda: fake.acks)
            with pytest.raises(TimeoutError):  # unknown position: controller restarts at 0
                await client.resume_history(index=fake.log.token(7))
            await until(lambda: not client.connected)

    run(check())


def test_reconnect_resends_history_lost_in_flight(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, first):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            first.record([first.history_record(1, 1, s) for s in (1, 2)])
            await until(lambda: first.log.token(0) in first.acks)
            first.record([first.history_record(1, 1, s) for s in (3, 4, 5)], lost=True)
            second = await reconnect(client, first)
            try:
                second.record([second.history_record(1, 1, 6)])
                checkpoint = first.log.token(0).to_bytes(4, "little")
                await until(lambda: [f.payload for f in second.sent(True, Extended.RESEND_HISTORY)])
                assert second.sent(True, Extended.RESEND_HISTORY)[0].payload == checkpoint
                async with asyncio.timeout(5):
                    while await sequences(client, started) != [1, 2, 3, 4, 5, 6]:
                        await asyncio.sleep(0.02)
            finally:
                await second.close()

    run(check())


def test_reconnect_without_a_checkpoint_sends_no_resend(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, first):
            assert await client.history.checkpoint() is None
            second = await reconnect(client, first)
            try:
                await asyncio.sleep(0.2)
                assert second.sent(True, Extended.RESEND_HISTORY) == []
            finally:
                await second.close()

    run(check())


def test_expired_checkpoint_replays_oldest_history(tmp_path, caplog):
    async def check():
        async with connected(tmp_path) as (client, first):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            first.record([first.history_record(1, 1, s) for s in (1, 2)])
            await until(lambda: first.acks)
            # The controller no longer holds the checkpoint's file; it restarts at the oldest.
            records = first.log.records + [first.history_record(1, 1, 3)]
            log = HistoryLog(base=110 << HISTORY_FILE_SHIFT, records=records, sent=3)
            second = await reconnect(client, first, log)
            try:
                async with asyncio.timeout(5):
                    while await sequences(client, started) != [1, 2, 3]:
                        await asyncio.sleep(0.02)
            finally:
                await second.close()

    run(check())
    assert "History resend started at 0x37000000 instead of 0x36800000" in caplog.text


def test_skipped_history_is_logged_once(tmp_path, caplog):
    async def check():
        async with connected(tmp_path) as (client, fake):
            await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            first = [fake.history_record(1, 1, s) for s in (1, 2)]
            fake.history(fake.log.token(0), first)
            fake.history(fake.log.token(5), [fake.history_record(1, 1, 6)])
            fake.history(fake.log.token(0), first)  # retransmission: not a gap
            fake.history(fake.log.token(6), [fake.history_record(1, 1, 7)])
            await until(lambda: fake.log.token(6) in fake.acks)

    run(check())
    assert caplog.text.count("History gap") == 1
    assert "History gap of 3 records" in caplog.text


def test_unsolicited_reply_ends_the_session(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            fake.send(Frame(fake.start_flag, fake.device, 1, Short.STOP, bytes(8), reply=True))
            async with asyncio.timeout(5):
                while client.connected:
                    await asyncio.sleep(0.01)
            assert isinstance(client.error, ProtocolError)

    run(check())


def test_other_hosts_are_refused(tmp_path):
    async def check():
        client = Client(host="127.0.0.1", controller_host="192.0.2.1", history=MemorySink(), port=0)
        await client.listen()
        fake = FakeController(client.listening_port)
        await fake.connect()
        await asyncio.sleep(0.1)
        assert not client.connected
        await fake.close()
        await client.close()

    run(check())


def test_silent_controller_is_dropped_so_it_can_reconnect(tmp_path):
    async def check():
        client = Client(
            host="127.0.0.1",
            controller_host="127.0.0.1",
            history=MemorySink(),
            port=0,
            receive_timeout_s=0.3,
        )
        await client.listen()
        first = FakeController(client.listening_port)
        await first.connect()
        await client.wait_ready(5)
        first.go_silent()
        async with asyncio.timeout(5):
            while client.connected:
                await asyncio.sleep(0.02)
        second = FakeController(client.listening_port)
        await second.connect()
        await client.wait_ready(5)
        assert client.connected
        for fake in (first, second):
            await fake.close()
        await client.close()

    run(check())


def test_continue_after_a_protection_stop_needs_the_alarm_acknowledged(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            fake.protect(1, 1, alarm=501)
            await wait_state(client, ChannelState.PROTECTED)
            test_id = started.test_id
            with pytest.raises(StateError, match="acknowledged_alarm=501"):
                await client.control(ADDRESS, Control.CONTINUE, expected_test_id=test_id)
            with pytest.raises(StateError, match="acknowledged_alarm=501"):  # wrong alarm
                await client.control(
                    ADDRESS, Control.CONTINUE, expected_test_id=test_id, acknowledged_alarm=502
                )
            assert fake.sent(False, Short.CONTINUE) == []
            await client.control(
                ADDRESS, Control.CONTINUE, expected_test_id=test_id, acknowledged_alarm=501
            )
            status = client.snapshot.get(ADDRESS)
            assert (status.state, status.alarm_code, status.test_id) == (
                ChannelState.RUNNING,
                0,
                test_id,
            )
            with pytest.raises(ValueError, match="only applies to Continue"):
                await client.control(
                    ADDRESS, Control.STOP, expected_test_id=test_id, acknowledged_alarm=501
                )

    run(check())


def test_continue_that_protects_again_reports_the_new_alarm(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            started = await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            fake.protect(1, 1, alarm=501)
            await wait_state(client, ChannelState.PROTECTED)
            fake.channels[1, 1].retrip_alarm = 503
            with pytest.raises(StateError, match="continued, then stopped again.*alarm 503"):
                await client.control(
                    ADDRESS,
                    Control.CONTINUE,
                    expected_test_id=started.test_id,
                    acknowledged_alarm=501,
                )
            assert client.connected  # a certain outcome keeps the session
            assert client.snapshot.get(ADDRESS).alarm_code == 503

    run(check())
