"""The real client against the simulated controller: programs execute and are recorded."""

import asyncio
import functools
import itertools
from contextlib import asynccontextmanager

import pytest

from neware8 import ChannelState, Client, Control, Recipe, alarm_info
from neware8.simulator import CAPACITY_UPPER_ALARM, SimulatedController
from helpers import PROFILE, XML

ADDRESS = PROFILE.address


def synchronous(test):
    """Run an async test function to completion, as the other client tests do."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))

    return wrapper


CHARGE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<root><config type="Step File" version="17">
<Head_Info><Operate Value="66"/><RateType Value="103"/><Scale Value="1"/><Start_Step Value="1"/></Head_Info>
<Whole_Prt><Protect><Main><Volt><Upper Value="45000"/><Lower Value="0"/></Volt><Curr><Upper Value="8"/><Lower Value="-8"/></Curr><Cap><Upper Value="CAPACITY"/></Cap></Main></Protect><Record><Main><Time Value="1000"/></Main></Record></Whole_Prt>
<Step_Info Num="2"><Step1 Step_ID="1" Step_Type="1"><Limit><Main><Curr Value="5"/><Time Value="TIME"/></Main></Limit></Step1>
<Step2 Step_ID="2" Step_Type="6"/></Step_Info></config></root>"""


def charge_xml(*, time_ms: int, capacity_mas: int = 10_000_000) -> bytes:
    return CHARGE_XML.replace(b"TIME", str(time_ms).encode()).replace(
        b"CAPACITY", str(capacity_mas).encode()
    )


@asynccontextmanager
async def simulated(tmp_path, **options):
    client = Client(
        host="127.0.0.1",
        controller_host="127.0.0.1",
        storage_directory=tmp_path,
        port=0,
        upload_interval_raw=1,
        history_grace_period_s=0.2,
    )
    await client.listen()
    simulator = SimulatedController(
        port=client.listening_port, status_period_s=0.05, reconnect_delay_s=0.1, **options
    )
    await simulator.start()
    try:
        await client.wait_ready(5)
        while len(client.snapshot.channels) < 8:
            await client.wait_snapshot(after_revision=client.snapshot.revision, timeout=5)
        yield client, simulator
    finally:
        await simulator.close()
        await client.close()


async def wait_state(client, test_id, *states, timeout=10.0):
    async with asyncio.timeout(timeout):
        status = await client.wait_status(ADDRESS)
        while status.test_id != test_id or status.state not in states:
            status = await client.wait_status(ADDRESS, after_ns=status.received_at_ns)
        return status


async def wait_records(client, run, count, timeout=10.0):
    async with asyncio.timeout(timeout):
        while True:
            points = []
            while page := await client.read(
                run, after_sequence=points[-1].sequence if points else 0
            ):
                points.extend(page)
            if len(points) >= count:
                return points
            await asyncio.sleep(0.05)


@synchronous
async def test_program_with_cycles_runs_to_completion_and_is_recorded(tmp_path):
    async with simulated(tmp_path, time_scale=100) as (client, _):
        run = await client.start(Recipe(XML), PROFILE)
        status = await wait_state(client, run.test_id, ChannelState.FINISHED)
        assert status.alarm_code == 0
        # The first record, then five per 10 s execution of the 2 s recording interval.
        points = await wait_records(client, run, 16)

    assert [point.sequence for point in points] == list(range(1, 17))
    assert all(b.test_time_second >= a.test_time_second for a, b in itertools.pairwise(points))
    assert points[-1].test_time_second == pytest.approx(30.0)
    assert [point.cycle for point in points[1::5]] == [1, 2, 3]
    assert all(point.current_ampere == pytest.approx(-0.001) for point in points[1:])
    assert points[-1].step_count == 3
    assert points[-1].discharging_capacity_ah == pytest.approx(0.001 * 30 / 3600, rel=1e-3)
    assert points[-1].charging_capacity_ah == 0.0


@synchronous
async def test_capacity_protection_stops_the_channel_with_an_alarm(tmp_path):
    async with simulated(tmp_path, time_scale=100) as (client, _):
        recipe = Recipe(charge_xml(time_ms=3_600_000, capacity_mas=50))  # 10 s at 5 mA
        run = await client.start(recipe, PROFILE)
        status = await wait_state(client, run.test_id, ChannelState.PROTECTED)

    assert status.alarm_code == CAPACITY_UPPER_ALARM
    assert alarm_info(status.alarm_code).known
    assert 9.0 <= status.elapsed_s <= 11.0


@synchronous
async def test_stop_continue_and_clear_flag(tmp_path):
    async with simulated(tmp_path, time_scale=10) as (client, _):
        run = await client.start(Recipe(charge_xml(time_ms=3_600_000)), PROFILE)
        await wait_state(client, run.test_id, ChannelState.RUNNING)

        await client.control(ADDRESS, Control.STOP, expected_test_id=run.test_id)
        stopped = await wait_state(client, run.test_id, ChannelState.STOPPED)
        await asyncio.sleep(0.3)
        assert client.snapshot.get(ADDRESS).elapsed_s == pytest.approx(stopped.elapsed_s)

        await client.control(ADDRESS, Control.CONTINUE, expected_test_id=run.test_id)
        running = await wait_state(client, run.test_id, ChannelState.RUNNING)
        assert running.elapsed_s >= stopped.elapsed_s

        await client.control(ADDRESS, Control.STOP, expected_test_id=run.test_id)
        await wait_state(client, run.test_id, ChannelState.STOPPED)
        await client.control(ADDRESS, Control.CLEAR_FLAG, expected_test_id=run.test_id)
        await wait_state(client, run.test_id, ChannelState.FINISHED)


@synchronous
async def test_history_written_while_disconnected_arrives_after_reconnect(tmp_path):
    async with simulated(tmp_path, time_scale=50) as (client, simulator):
        run = await client.start(Recipe(charge_xml(time_ms=20_000)), PROFILE)
        await wait_state(client, run.test_id, ChannelState.RUNNING)
        simulator._disconnect()  # the controller drops the connection mid-run
        async with asyncio.timeout(5):
            while client.connected:
                await asyncio.sleep(0.01)
            while not client.connected:  # the simulator reconnects by itself
                await asyncio.sleep(0.01)
        await wait_state(client, run.test_id, ChannelState.FINISHED)
        points = await wait_records(client, run, 21)

    assert [point.sequence for point in points] == list(range(1, 22))
    assert points[-1].step_count == 1
