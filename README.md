# py-neware8

An asyncio driver for communicating directly with a Neware BTS82 controller. It handles the
native session, programs and controls, and delivers history to the application, which owns
the measurement data.
The distribution is `py-neware8`; the import is `neware8`. Python 3.11 or later is
required.

This is a best-effort implementation of the native protocol, based on Wireshark captures
of the manufacturer's software talking to our cycler and on static analysis of the
BTS software. Some details remain unknown. It is not an official Neware protocol specification.

**Hardware testing has been limited to our specific BTS82 cycler and
configuration:** ten units of eight channels, rated 5 V / 10 mA, using native TCP
port 8001 and a current scale of 10000. Compatibility with other models,
firmware versions, ranges or configurations has not been established. These
values describe the tested installation; they are not package defaults. The
automated tests cover protocol decoding, history delivery and decoding using
controlled input data. Not every exposed operation has been exercised on hardware.

The controller initiates the TCP connection to the library's listener. Configure
its host endpoint using the established site procedure before using this package.

## Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
from this repository:

```shell
uv sync --locked
```

This creates `.venv`, installs the package in editable mode, and installs the
development dependency group using `uv.lock`. For a runtime-only environment,
use `uv sync --locked --no-dev`.

Run development commands through uv:

```shell
uv run --locked pytest
uv run --locked ty check
uv run --locked ruff check .
uv run --locked ruff format --check src tests
uv build
```

Commit `uv.lock` with dependency changes. Use `uv add` for runtime dependencies
and `uv add --dev` for development tools; use `uv lock --upgrade` for deliberate
dependency upgrades. CI uses the lockfile across its Python and operating-system
matrix. uv manages environments and dependencies, and its `uv_build` backend
builds both source distributions and wheels. Package layout and source archive
contents are configured in `pyproject.toml`.

## Running a measurement

The addresses and ratings below are arguments supplied by your application. A
physical ID is controller-wide; it is not the logical channel number. Use the
installation's established mapping. Compilation is offline and sends nothing.

The application provides a `HistorySink` that stores history durably; the client
acknowledges a batch to the controller only after the sink committed it (see
[History](docs/history.md)).

```python
from pathlib import Path
from neware8 import ChannelProfile, ChannelState, Client, HistorySink, Recipe


async def start_measurement(
    local_ip: str, controller_ip: str, profile: ChannelProfile, xml_path: Path, sink: HistorySink
) -> None:
    recipe = Recipe.from_file(xml_path)
    compiled = recipe.compile(profile)
    print(compiled.step_count, compiled.maximum_duration_s)

    async with Client(host=local_ip, controller_host=controller_ip, history=sink) as client:
        await client.wait_ready()
        test = await client.start(recipe, profile)  # the sink registered test.test_id
        status = await client.wait_status(profile.address)
        while status.test_id != test.test_id or status.state != ChannelState.FINISHED:
            status = await client.wait_status(profile.address, after_ns=status.received_at_ns)
```

Construct a `ChannelProfile` with `address`, `physical_id`, `voltage_rating_v`,
`current_rating_a` and `current_scale`. `charge_range_raw` is optional and must
be supplied explicitly when records have zero current-step range. It is a native
range encoding, not an ampere value.

Calling `start` or `control` explicitly authorizes that library operation. The
library never asks interactive questions. Application operators remain responsible
for selecting an appropriate cell, recipe and channel. Disconnecting does not
stop a measurement. A timeout or cancellation can leave a write's outcome uncertain;
the connection closes and the write is never automatically replayed.

## Data ownership

Live statuses for all observed main channels are cached in memory.
`client.snapshot` returns the current immutable whole-controller status snapshot
without copying. `await client.wait_snapshot(after_revision=...)` waits for an
update and coalesces intermediate revisions for slow consumers.

neware8 stores no measurement data. Every history batch goes to the application's
`HistorySink`, which keeps what it needs, typically the main records of the tests it
started, and the resend position. The client tells the sink each TestID it is assigned
before any of that test's history can arrive. Tests started elsewhere are observable
and controllable by channel; whether their history is kept is the sink's choice.

`neware8.history` decodes stored records into engineering units, with the device's
step-boundary time quirk resolved and capacity and energy accumulated from its per-step
counters. It also lists the established mapping to the Battery Data Format.

On reconnect, a fresh controller identity is handed to the sink before history is
handled. Writes are never replayed. History is requested again from the sink's
checkpoint after every connect, so batches in flight during a drop are recovered;
`resume_history()` does this explicitly. The controller's retention limits and final
completeness remain unknown.

## Documentation

- [API and lifecycle](docs/api.md)
- [Controller information and settings](docs/controller.md)
- [Architecture and concurrency](docs/architecture.md)
- [History: delivery, decoding and BDF mapping](docs/history.md)
- [Protocol specification](docs/protocol/README.md)
- [Corrections and open questions](docs/protocol/evidence.md)
- [Validation and the simulated controller](docs/testing.md)

The supported XML subset includes protected CC/CV/CCCV charge/discharge, rest,
finite cycles and End. Unsupported XML settings fail explicitly. Auxiliary
configuration, voltage scans, electrical parallel grouping, firmware, bootloader
and calibration operations are not public APIs.

## Logging

The library uses standard Python logging and does not configure application output.
Configure `logging.basicConfig` in your application and set the `neware8` logger to
INFO for connection, channel-state and control events, or DEBUG for exchanges, start
phases and history batch ranges. Failures include context such as controller address,
channel and TestID where available; unexpected session failures include tracebacks. Repeated unchanged statuses are not logged
at INFO.

Raw packets are separately opt-in: set `logging.getLogger("neware8.wire").level`
with `setLevel(logging.DEBUG)` and set its `propagate` attribute to `True` to use your
application handlers. Normal DEBUG logging does not format or emit packet dumps.
Raw packets may contain program and measurement data.
