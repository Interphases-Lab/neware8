# Validation

```shell
uv sync --locked
uv run --locked pytest
uv run --locked mypy
uv run --locked ruff check .
uv run --locked ruff format --check src tests
```

Tests need no controller.

| File | Covers |
|---|---|
| `test_protocol.py` | Framing round trips of recorded frames, fragmented/coalesced/malformed streams, forbidden commands, metadata, status and history layouts, history ACK bytes, the manufacturer start sequence re-encoded byte for byte, lifecycle selections, RTC, recovery requests, serial replies, step records, state and current scaling |
| `test_recipe.py` | The Aurora CC/CV `Step.xml` compiles to the manufacturer's 189-byte program; cycle encoding; rejection of unsupported XML |
| `test_client.py` | The real `Client` against `fake_controller.py` over TCP: discovery, the start sequence, rejected and unanswered starts (no retry), controls and their confirmation, history stored before ACK, settings, information, history resend, unsolicited replies, refused peers, reconnect after a silent connection |
| `test_storage.py`, `test_bdf.py`, `test_counters.py` | Run ownership, deduplication, conflicts, restart, gaps, exports, boundary-time correction, step/cycle/test capacity and energy against recorded datasets |

`tests/fake_controller.py` simulates the documented controller behaviour: Start arms channels and assigns TestIDs, Confirm starts them, the
download buffer resets at fragment 1, controls change state and echo their selection.
Passing these tests shows the client follows the protocol as documented; it does not show
that the real controller behaves the same in every case.
