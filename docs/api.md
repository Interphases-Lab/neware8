# API and lifecycle

All network methods belong to the event loop that starts the client. The storage
object has a synchronous, thread-safe interface; `Client` moves its blocking
operations to worker threads. Use one client and one data directory per controller,
and one owning application process per directory.

## Connection and observation

For event-driven access to recorded curves, see [committed record
notifications](record-notifications.md). These are separate from channel status.

| API | Behavior |
|---|---|
| `Client(host=..., controller_host=..., storage_directory=..., port=8001)` | Explicit IP addresses; no site defaults. Port 0 requests an available local port. `receive_timeout_s` (60) closes a connection that stays silent; the controller heartbeats about every 17 s. |
| `async with client` / `await client.listen()` | Listen for the configured controller; reject other peers and duplicate connections. |
| `await client.wait_ready(timeout=30)` | Wait for completed discovery and a fresh identity query bound to the store. |
| `client.channels` | Advertised main-channel addresses after complete multipart discovery. |
| `client.statuses` | Read-only snapshot mapping addresses to immutable status records. |
| `client.snapshot` | O(1) read of the immutable whole-controller `StatusSnapshot`. |
| `await client.wait_snapshot(after_revision=..., timeout=20)` | Latest newer snapshot; intermediate revisions can be coalesced. |
| `await client.wait_status(address, after_ns=0, timeout=20)` | Latest observation newer than the requested receipt timestamp. |
| `client.connected`, `client.error` | Connection availability and the most recent background session/export error. |
| `await client.close()` | Release listener, socket and storage; does not stop the cycler. |

Status age uses a monotonic host clock. `received_at_ns` is host receipt time,
not device measurement time. Raw state, step type, alarm code and the complete
100-byte record remain available. Unknown step types return `None` from the
`step_type` property; unknown state encodings retain `state_raw`.

`StatusSnapshot` holds a tuple of channel statuses sorted by address, a monotonically
increasing revision, creation time and connection flag. One replacement is
published per live-status frame, plus connection-state changes. Previous snapshots
remain immutable; unchanged channel records are shared. Reading the snapshot
does not clone records or reparse bytes. The high-level service can serialize it
once for its subscribers. No queue of stale status updates grows for slow readers.

```python
snapshot = client.snapshot
snapshot = await client.wait_snapshot(after_revision=snapshot.revision)
for status in snapshot.channels:
    print(status.address, status.voltage_v, status.current_a, status.age_s)
```

## Recipes and control

`Recipe.from_file(path)` accepts `.xml` only. `Recipe(xml_bytes)` accepts original
XML bytes. `compile(profile)` returns immutable native bytes, the source digest,
step count and maximum duration without opening a connection.

`await client.start(recipe, profile)` serializes the mapping, auxiliary, program,
protection, start and confirmation exchanges. It requires complete discovery,
a status within `upload_interval_raw * missed_upload_intervals` seconds and finished state. A retained diagnostic
code on a finished target does not prevent starting; it remains available in status. Other active
channels must have known physical mappings, fresh status, no unresolved alarms,
and distinct physical IDs. Supply configured profiles to the client for external channels. Download replies must match the requested GUID, target and fragment.
The device-assigned TestID is durably registered before accepting following history.

`await client.start_many(recipe, profiles)` sends one masked start for distinct
channels in the same unit. Every profile is validated and must compile to the
same native program; different programs or units are rejected before any write.
Each channel gets its own run and controller-assigned TestID, returned in input
order. Optional `run_ids` supplies one unique UUID per profile; `on_attempt`
runs once for the group. Masks are covered by synthetic handshake tests; multi-channel hardware acceptance remains outstanding.
The API acknowledges the native sequence; callers confirm live status separately.

Starts awaiting matching status reserve their physical channels. Final export
tasks, including the history grace period, block new starts with
`Final export in progress`. `client.exporting_channels` lists those channels.

`client.check_start(profile)` checks readiness without writes. Start repeats
these checks under its operation lock. After they succeed, previous records and
managed exports are cleared before the first start-sequence write. Failed XML or
readiness checks preserve them. Explicit rejection after an attempt begins leaves
no owned run; uncertain transmission must be reconciled, never retried blindly.

Optional `run_id` assigns a caller-supplied UUID. Optional async `on_attempt`
executes once, after readiness checks and before clearing/writing, for durable
application bookkeeping. It must not call another operation on this client.
Cancellation or failure of that callback prevents clearing and transmission.
`discard_data(address)` explicitly removes retained ownership, records and managed
exports; it never stops a physical measurement.

`await client.control(address, Control.STOP, expected_test_id=observed_test_id)`
stops the selected channel regardless of recording ownership. ClearFlag, Continue
and Jump use the same channel-addressed API. The latest channel report from the active connection must have a matching TestID;
an identity change aborts the command instead of targeting a replacement run.
`Control.CONTINUE`, `Control.CLEAR_FLAG` and `Control.JUMP` use their established
state preconditions. Jump also requires `destination_step=...`, an existing step
in that run's program. Clear Flag completes on a matching successful ACK; its
reported diagnostic is retained. Other controls require ACK and a same-TestID
status transition. Channel controls have no report-age cutoff; missing reports
and disconnected sessions still prevent control.
Start readiness and control checks use decoded manufacturer states, including
valid flagged/extended representations. Jump to End can complete on a finished
observation at the requested step with step type End; it still requires the
matching successful ACK, unchanged TestID and no alarm.
Stop/Continue retain the measurement; Clear Flag changes stopped to finished.
There is no separate invented Pause command.

The library serializes command handshakes, while previously started independent
channels continue running and recording. It does not make electrically parallel
channels. Physical isolation of concurrent starts still needs hardware acceptance.

Cancellation after transmission closes the connection and propagates
`CancelledError`. A canceled await is not evidence that the device canceled the
command. `OperationUncertain` similarly prohibits blind retries. A start rejected
after earlier setup commands can still have changed mappings or settings.

## Runs and data

`await client.runs()` returns current persisted runs. A `Run` is an immutable
snapshot, so call `runs()` again for updated record counts and state.
`await client.read(run, after_sequence=0, limit=4096)` returns bounded pages of
typed `RecordedPoint` objects. Isolated step-boundary zero times are reconstructed
from adjacent records; raw bytes remain unchanged. A boundary awaiting its next
record is withheld, so continue from the last returned sequence when more history
arrives. See [storage and time correction](storage.md).

`await client.export(run, path=None)` publishes a consistent snapshot. Omit the
path for `<storage_directory>/<run-id>.bdf.parquet`. Automatic export occurs when
a matching status becomes finished and is refreshed after late records. Explicit
export also works during a run. None of these operations claims all device history
has arrived. An export error is reported through `client.error` and logging;
explicit export raises the error directly. A boundary awaiting adjacent history
defers automatic export without setting `client.error`.

All files exported through this API, including custom paths, belong to that run
and are deleted when a new measurement replaces it on the channel. Copy data
outside the library independently if an application needs permanent archival.

External measurements are not recorded. Channel controls do not create a run,
replace retained data or enable recording.

## Reconnection and recovery

The listener stays available after a peer disconnects, including a connection closed for
silence after `receive_timeout_s`. A replacement connection
must complete discovery and the identity query. Runs with a matching TestID are
eligible for recording again; mismatches are marked interrupted and never merged.
Fresh observations are required before further control. Profiles supplied for
new operations remain explicit.

`await client.resume_history(index=None)` uses the saved opaque native stream
token verbatim, without adding one. If no token exists, provide an independently
established index. This is an explicit cursor write, not an automatic reconnect
action. The first accepted batch must have the requested token, following a
successful resend ACK. Failure closes the session; no minimum-index fallback is
attempted. Completion means the first matching batch was durably stored, not that
all missing records were recovered. `await client.history_minimum_index()` reads the oldest
position the controller can still resend.

## Controller settings

Typed controller information, upload cadence, local clock and explicit TestID
synchronization are described in [Controller information and settings](controller.md).

## Errors

`NewareError` is the base for `ConnectionLost`, `ProtocolError`, `StateError`,
`StorageError`, `OperationRejected` and `OperationUncertain`. Invalid local input
raises `ValueError` or `TypeError`; an expired external run handle raises `KeyError`.
`ProtocolError` is also a `ValueError`. The protocol modules (`neware8.protocol`, `messages`,
`steps`) are importable for diagnostics; only names exported from `neware8` are stable.
