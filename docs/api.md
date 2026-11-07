# API and lifecycle

All network methods belong to the event loop that starts the client. Use one client per controller. History goes to the application's `HistorySink`; see [History](history.md).

## Connection and observation

| API | Behaviour |
|---|---|
| `Client(host=..., controller_host=..., history=sink, port=8001, profiles=())` | Explicit IP addresses; no site defaults. Port 0 requests an available local port. `profiles` describe the installed channels (physical mapping, charge range). `receive_timeout_s` (60) closes a connection that stays silent; the controller heartbeats about every 17 s. |
| `async with client` / `await client.listen()` | Listen for the configured controller; reject other peers and duplicate connections. |
| `await client.wait_ready(timeout=30)` | Wait for completed discovery, a fresh identity query and the sink's `bind`. |
| `client.channels` | Advertised main-channel addresses after complete multipart discovery. |
| `client.statuses` | Read-only snapshot mapping addresses to immutable status records. |
| `client.snapshot` | O(1) read of the immutable whole-controller `StatusSnapshot`. |
| `await client.wait_snapshot(after_revision=..., timeout=20)` | Latest newer snapshot; intermediate revisions can be coalesced. |
| `await client.wait_status(address, after_ns=0, timeout=20)` | Latest observation newer than the requested receipt timestamp. |
| `client.connected`, `client.error` | Connection availability and the most recent session error, including a refused sink call. |
| `await client.close()` | Release the listener and socket; does not stop the cycler. The sink is the application's to close. |

Status age uses a monotonic host clock. `received_at_ns` is host receipt time, not device measurement time. Raw state, step type, alarm code and the complete 100-byte record remain available. Unknown step types return `None` from the `step_type` property; unknown state encodings retain `state_raw`.

`StatusSnapshot` holds:

- a tuple of channel statuses sorted by address
- a monotonically increasing revision
- the creation time and connection flag

One replacement is published per live-status frame, plus connection-state changes. Previous snapshots remain immutable, and unchanged channel records are shared. Reading the snapshot does not clone records or reparse bytes. No queue of stale status updates grows for slow readers.

```python
snapshot = client.snapshot
snapshot = await client.wait_snapshot(after_revision=snapshot.revision)
for status in snapshot.channels:
    print(status.address, status.voltage_v, status.current_a, status.age_s)
```

## Recipes and control

`Recipe.from_file(path)` accepts `.xml` only. `Recipe(xml_bytes)` accepts original XML bytes. `compile(profile)` returns immutable native bytes, the source digest, step count and maximum duration, without opening a connection.

`await client.start(recipe, profile)` serializes the mapping, auxiliary, program, protection, start and confirmation exchanges, and returns the `StartedTest`. It requires:

- complete discovery
- a status within `upload_interval_raw * missed_upload_intervals` seconds
- a finished target channel

A retained diagnostic code on a finished target does not prevent starting; it remains available in status. Other active channels must have known physical mappings, fresh status, no unresolved alarms and distinct physical IDs. Supply configured profiles for channels started elsewhere. Download replies must match the requested GUID, target and fragment. The device-assigned TestID is registered with the sink in the Start reply, before any later frame is read, so history of the new test cannot arrive before the sink knows it.

`await client.start_many(recipe, profiles)` sends one masked start for distinct channels in the same unit. Every profile is validated and must compile to the same native program; different programs or units are rejected before any write. Each channel gets its own controller-assigned TestID, returned in input order. Masks are covered by synthetic handshake tests; multi-channel hardware acceptance remains outstanding. The API acknowledges the native sequence; callers confirm live status separately.

Starts awaiting matching status reserve their physical channels. A status that reports the new TestID while the start sequence is still in progress (an armed, not yet confirmed channel) does not confirm the start.

`client.check_start(profile)` checks readiness without writes; `start` repeats these checks under its operation lock. Optional async `on_attempt` runs once, after the readiness checks and before the first write, for durable application bookkeeping. It must not call another operation on this client. Cancellation or failure of that callback prevents transmission. Explicit rejection after an attempt begins leaves no started test; uncertain transmission must be reconciled, never retried blindly.

`await client.control(address, Control.STOP, expected_test_id=observed_test_id)` stops the selected channel, whichever client started its test. Clear Flag, Continue and Jump use the same channel-addressed API:

- The latest channel report from the active connection must have a matching TestID. An identity change aborts the command instead of targeting a replacement test.
- `Control.CONTINUE`, `Control.CLEAR_FLAG` and `Control.JUMP` use their established state preconditions. Jump also requires `destination_step=...`, an existing step in that test's program.
- Clear Flag completes on a matching successful ACK; its reported diagnostic is retained. Other controls require the ACK and a same-TestID status transition.
- Channel controls have no report-age cutoff, but missing reports and disconnected sessions still prevent control.

Start readiness and control checks use decoded manufacturer states, including valid flagged and extended representations. Jump to End can complete on a finished observation at the requested step with step type End; it still requires the matching successful ACK, an unchanged TestID and no alarm. Stop and Continue retain the measurement; Clear Flag changes stopped to finished. There is no separate invented Pause command.

Continue resumes a stopped, protected, paused or beeping test on the same channel, as the manufacturer client allows; TestID, step, cycle, counters and recording carry on. After a protection stop the channel shows an alarm, and Continue requires that code explicitly:

    status = client.snapshot.get(address)
    await client.control(address, Control.CONTINUE, expected_test_id=status.test_id,
                         acknowledged_alarm=status.alarm_code)

Only the acknowledged alarm is overridden; a different alarm is refused. If the cause persists, the test may run briefly and stop again before a running report arrives. Test time only advances while a channel runs, so a later stopped report with more test time is a certain outcome: `StateError` names the new alarm and the session stays open. The controller restarts the current step's timer on Continue (observed after a Stop). Continuing after a protection stop has not yet been tested on hardware.

The library serializes command handshakes, while previously started independent channels continue running. It does not make electrically parallel channels. Physical isolation of concurrent starts still needs hardware acceptance.

Cancellation after transmission closes the connection and propagates `CancelledError`. A cancelled await is not evidence that the device cancelled the command. `OperationUncertain` similarly prohibits blind retries. A start rejected after earlier setup commands can still have changed mappings or settings.

## Reconnection and recovery

The listener stays available after a peer disconnects, including a connection closed for silence after `receive_timeout_s`. A replacement connection must complete discovery, the identity query and the sink's `bind`. Fresh observations are required before further control.

After every connect, if the sink has a checkpoint, the client sends the controller that position (`0010`), as the manufacturer server does. The controller continues after the last batch it *sent*, so without this the batches in flight when the connection dropped would be skipped. Resent records repeat what the sink already holds. If the controller no longer holds that position, whatever it sends first is committed and an error is logged (records in between may be lost). A rejected request is logged and the session continues. Within a session, a batch whose token is ahead of the previous batch's end is logged as a gap; resent batches are not.

`await client.resume_history(index=None)` sends the same request explicitly, using the sink's checkpoint or an independently established index. It is strict: the first accepted batch must have the requested token, following a successful resend ACK; otherwise the session is closed. A rejected request raises `OperationRejected` and keeps the session. Completion means the first matching batch was committed, not that all missing records were recovered. `await client.history_minimum_index()` reads the oldest position the controller can still resend (in the simulator, only until a resend position has been set).

## Controller settings

Typed controller information, upload cadence, local clock and explicit TestID synchronization are described in [Controller information and settings](controller.md).

## Errors

`NewareError` is the base for `ConnectionLost`, `ProtocolError`, `StateError`, `StorageError`, `OperationRejected` and `OperationUncertain`. `StorageError` means history could not be stored by the sink or decoded. Invalid local input raises `ValueError` or `TypeError`. `ProtocolError` is also a `ValueError`. The protocol modules (`neware8.protocol`, `messages`, `steps`) are importable for diagnostics; only names exported from `neware8` and `neware8.history` are stable.
