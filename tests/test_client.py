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
)
from neware8.protocol import Extended, Frame, Short
from fake_controller import FakeController
from helpers import PROFILE, XML

ADDRESS = PROFILE.address


@asynccontextmanager
async def connected(tmp_path, **options):
    client = Client(
        host="127.0.0.1",
        controller_host="127.0.0.1",
        storage_directory=tmp_path,
        port=0,
        upload_interval_raw=1,
        history_grace_period_s=60,
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


def test_connect_discovers_channels_and_binds_storage(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            assert client.connected
            assert client.channels == tuple(ChannelAddress(1, c) for c in range(1, 9))
            status = client.snapshot.get(ADDRESS)
            assert status.state == ChannelState.FINISHED and status.voltage_v == 3.6
            assert status.current_a == pytest.approx(-0.001)

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
            assert (await client.runs())[0].test_id == started.test_id
            with pytest.raises(StateError, match="finished channel"):
                client.check_start(PROFILE)

    run(check())


def test_rejected_start_leaves_no_run(tmp_path):
    async def check():
        async with connected(tmp_path, reject={(True, Extended.START): 1}) as (client, fake):
            with pytest.raises(OperationRejected):
                await client.start(Recipe(XML), PROFILE)
            assert await client.runs() == ()
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


def test_history_is_stored_before_it_is_acknowledged(tmp_path):
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
            points = await client.read(started)
            assert [p.sequence for p in points] == [1, 2]
            assert points[0].voltage_volt == 3.6
            assert client.store.checkpoint() == 77

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


def test_resume_history_waits_for_the_requested_batch(tmp_path):
    async def check():
        async with connected(tmp_path) as (client, fake):
            await client.start(Recipe(XML), PROFILE)
            await wait_state(client, ChannelState.RUNNING)
            await client.resume_history(index=5)
            assert fake.sent(True, Extended.RESEND_HISTORY)[0].payload == (5).to_bytes(4, "little")
            async with asyncio.timeout(5):
                while 5 not in fake.acks:
                    await asyncio.sleep(0.01)

    run(check())


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
        client = Client(
            host="127.0.0.1", controller_host="192.0.2.1", storage_directory=tmp_path, port=0
        )
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
            storage_directory=tmp_path,
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
