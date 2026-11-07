# Validation

```shell
uv sync --locked
uv run --locked pytest
uv run --locked ty check
uv run --locked ruff check .
uv run --locked ruff format --check src tests
```

Tests need no controller.

| File | Covers |
|---|---|
| `test_protocol.py` | Framing round trips of recorded frames, fragmented/coalesced/malformed streams, forbidden commands, metadata, status and history layouts, history ACK bytes, the manufacturer start sequence re-encoded byte for byte, lifecycle selections, RTC, recovery requests, serial replies, step records, state and current scaling |
| `test_recipe.py` | The Aurora CC/CV `Step.xml` compiles to the manufacturer's 189-byte program; cycle encoding; rejection of unsupported XML |
| `test_client.py` | The real `Client` against `fake_controller.py` over TCP: discovery, the start sequence, rejected and unanswered starts (no retry), controls and their confirmation, history stored before ACK, settings, information, explicit and automatic history resend (records in flight at a drop, an expired position), gap logging, unsolicited replies, refused peers, reconnect after a silent connection |
| `test_storage.py`, `test_bdf.py`, `test_counters.py` | Run ownership, deduplication, conflicts, restart, gaps, exports, boundary-time correction, step/cycle/test capacity and energy against recorded datasets, totals in paged reads (live, resumed and archived) |
| `test_simulator.py` | The real `Client` against `neware8.simulator` over TCP: a cycled program runs to completion and is recorded with totals, a capacity protection trips with its alarm, Stop/Continue/Clear Flag, history written while disconnected arrives after the reconnect |

`tests/fake_controller.py` simulates the documented controller behaviour: Start arms channels and assigns TestIDs, Confirm starts them, the
download buffer resets at fragment 1, controls change state and echo their selection. Its
history log outlives a connection: a new connection continues after the last batch sent, and
a resend from an unknown position restarts at the oldest record.
Passing these tests shows the client follows the protocol as documented; it does not show
that the real controller behaves the same in every case.

## Simulated controller

`neware8.simulator.SimulatedController` is a public, packaged simulator for application
development and tests without hardware. Where the fake controller only follows the
handshakes and lets tests script history, the simulator executes each channel's downloaded
program on an equivalent-circuit cell (`SimulatedCell`): CC, CV and CCCV charge and
discharge, rest, backward cycles and End, the step end conditions, the step protections
(alarms 67, 68, 69, 70 and 733) and the recording rules. Stop freezes a test, Continue
resumes it with a restarted step timer, Clear Flag finishes a stopped or protected channel.
History lives in a log that outlives connections, and the simulator reconnects by itself.

```python
async with Client(host="127.0.0.1", controller_host="127.0.0.1",
                  storage_directory=path, port=0) as client:
    async with SimulatedController(port=client.listening_port, time_scale=60):
        await client.wait_ready()
```

Simulated time runs `time_scale` times faster than wall-clock time. Status uploads follow
the upload interval the client sets, in wall-clock seconds, unless `status_period_s` is
given. The simulator's alarm codes, cell model, record contents (no auxiliary channels,
constant temperature) and step-start behaviour are its own choices and were not validated
against the device.
