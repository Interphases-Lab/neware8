# Committed record notifications

`Client.record_revision` is a process-local, monotonically increasing counter.
`Client.record_versions` is a cached immutable mapping from retained run IDs to
`RecordRevision(revision, reset_revision)`. Reading these properties does not
query SQLite. `await client.wait_records(after_revision=revision, timeout=30)`
waits for a newer revision, coalescing notifications for slow consumers.

The client publishes after committing incoming measurement records and on
ownership assignment/replacement or recovery. Duplicate uploads do not publish
record changes. A removed run ID invalidates a subscription. Closing the client
wakes waiting callers with `ConnectionLost` when no newer revision is available.
The revision counter and mapping are not a durable cursor or event log.

Subscribers read the global revision before inspecting the per-run mapping,
then wait with that revision to avoid losing a wakeup. Read bounded pages with
`Client.read()` only when the selected run's version changes or initial replay
has more pages. Multiple consumers can share those pages in their service layer.

A zero-time step boundary awaiting its next record is withheld by `read()`.
Keep the last returned sequence as the cursor. The next record notification
allows the boundary and its following points to be read with corrected time.

`reset_revision` advances when a commit inserts a record at or below the
previously stored highest sequence. Consumers must replay from sequence zero
when that value changes, or otherwise reconcile the earlier records explicitly.
This accounts for late history filling older gaps, including after a storage
restart. After reconnecting to a restarted service, start a fresh replay unless
the application independently reconciles earlier history.

`MeasurementStore.append(batch, eligible_runs)` takes a decoded `messages.HistoryBatch` and
returns committed `RecordChange` values containing
the run ID, lowest/highest inserted sequence, and `backfill` flag. This return
type changed in 0.2.0. Backfill detection uses an indexed maximum-sequence query
once per changed run in the batch. Notifications carry no measurement payload,
perform no plotting, and create no per-subscriber work in the receive loop.
