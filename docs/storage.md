# Recording and BDF export

SQLite is the intermediate format. Transactions provide crash recovery,
deduplication, conflict detection and indexed paging while measurements arrive.
`journal_mode=WAL` and `synchronous=FULL` are required. Final snapshots use
PyArrow's Parquet writer with Zstandard compression and 4096-record batches.
There is no alternate backend when a required dependency is missing.

The on-disk schema is version 2. Unsupported versions fail instead of implicitly
migrating or recreating the database. Use a new data directory for development
databases created with schema version 1. Use a local filesystem with working SQLite
locking and durability semantics, and a single owning application per directory.

## Ownership and retention

The store binds to the 16-byte controller identity returned by a fresh `002A`
exchange. Each logical unit/channel has one run slot and a generated local run
identifier distinct from the controller's TestID. The raw XML, physical ID,
explicit zero-range scaling override and measurement start receipt time are stored.

Only matching eligible unit/channel/TestID records enter the database. Unknown
record variants for an eligible run remain in `opaque_records`. Unowned data is
intentionally discarded. Repeated main records with identical sequence and bytes
are ignored; conflicting bytes reject the whole transaction and prevent ACK.

After compilation and readiness checks, a new start attempt clears the previous
channel's rows and ownership before its first device write. The assigned TestID
then establishes its new owned run. Validation failures preserve old data;
failures after the attempt begins do not restore it. External measurements are
never imported. All exports created through the library,
including custom destination paths, are registered and deleted when the run is
replaced. Other channels' runs and files are unaffected. Logical deletion
does not promise secure erasure of filesystem blocks or backups.

File deletions use a durable pending-deletion list committed with channel
replacement. Reopening the store completes any deletion interrupted by a crash.
SHA-256 digests in SQLite identify managed files before overwrite or deletion;
a path replaced by an unrelated file produces an error. Publication records both
the previous and candidate digest before atomic replacement, so either file is
recognized if publication is interrupted. Export and
replacement are serialized to prevent an in-flight export recreating an old run.
The library cannot remove copies made independently by another application.

The last durable device-level history token is separate from channel sequence
numbers. Records are committed before saving the token, and both happen before
ACK. A crash between those commits can cause replay; deduplication makes that safe.
No arithmetic on tokens or assumptions about retention epochs are used.

## BDF mapping

The mapping follows the [Battery Data Alliance BDF definition](https://github.com/battery-data-alliance/battery-data-format),
reviewed against its ontology 1.3.0 vocabulary. Each `.bdf.parquet` file contains
one channel run and only standard BDF columns. Native main-record bytes are
not retained after finalization; archived `RecordedPoint.raw` is empty.
Run ownership, source XML, controller identity and file digests stay in SQLite.
`Export.missing_sequence_ranges` reports gaps through the Python API.

When a channel reports finished, a background task waits for
`history_grace_period_s` (default 30 seconds). Matching history uploads restart this
quiet period. It then publishes the final Parquet file and transactionally
removes decoded record rows from SQLite. Record
paging and subsequent exports use the retained file. Failed publication leaves
the working records intact. Finished runs are scheduled again on reconnect,
covering interrupted finalization. Undecoded auxiliary records are also deleted
after successful publication; they are not represented in BDF.
History received after finalization raises `StorageError` and is not
acknowledged; the archive remains unchanged. During the grace period, duplicate
and conflicting records retain their usual semantics. Shutdown cancels pending
grace periods, preserving working records for reconnect, and waits for any
publication already in progress. SQLite reuses released pages; its file need not shrink.

| BDF column | Source |
|---|---|
| `test_time_second` | Device total milliseconds / 1000, with the boundary correction below |
| `voltage_volt` | Signed native voltage / 10000 |
| `current_ampere` | Signed native current / (range scale * 1000); positive means charging |
| `cycle_count` | Device cycle index, without renumbering |
| `step_id` | Device program step identifier, without renumbering |
| `record_index` | Original history sequence; missing sequence numbers remain visible |
| `step_time_second` | Device step milliseconds / 1000 |
| `step_type` | Known device step mode name; null for an unknown mode |
| `step_record_index` | One-based position among exported records within the observed step execution |
| `power_watt` | Voltage multiplied by signed current |
| `step_charging_capacity_ah`, `step_discharging_capacity_ah` | Device step capacity counters / (record range scale * 3600000) |
| `step_charging_energy_wh`, `step_discharging_energy_wh` | Device step energy counters / (record range scale * 3600000) |
| `charging_capacity_ah`, `discharging_capacity_ah`, `charging_energy_wh`, `discharging_energy_wh` | Completed step totals plus the current step's counters, accumulated since test start |
| Corresponding `cycle_` columns | Same accumulation, reset when the device cycle index changes |
| `cumulative_capacity_ah`, `cumulative_energy_wh` and their `step_`/`cycle_` counterparts | Charging plus discharging quantities at the corresponding scope |
| `net_capacity_ah`, `net_energy_wh` and their `step_`/`cycle_` counterparts | Charging minus discharging quantities at the corresponding scope |

The remaining BDF quantities were checked against the available record fields:

| BDF quantity group | Reason for omission |
|---|---|
| `step_count` | The native change counter is only eight bits; a global step-execution count across wraps and missing history is not established |
| `unix_time_second` | Device calendar fields have no established timezone; host receipt time is not the measurement time |
| Ambient, surface and T1-T5 temperatures | The main record temperature has no established sensor placement or auxiliary-channel identity |
| Internal resistance and impedance | The history resistance field's unit and measurement method are not established; no validated impedance series is available |
| Schedule capacity/energy quantities | Separate schedule-defined assignment events are not mapped |
| Pressure and frequency | No corresponding validated main-history measurements |

Unknown current scaling prevents export because BDF requires current. Unknown
values remain available in the original SQLite records.

## Capacity and energy

The four history counters are **step totals**, resetting at each step execution,
including consecutive steps in the same direction and CC-to-CV transitions.
Charging and discharging counters are separate positive magnitudes. They are not
whole-test totals, and successive samples must not be added together.

Exports add each completed step's final counters once, then add the current
step's counters. Whole-test totals continue across cycles; cycle totals restart
when the device cycle index changes. Repeated program step IDs are distinguished
using the cycle index and step-change counter. Each sample is scaled using its
own range, including automatic range changes within a step. Exact rational sums
are converted to floating-point BDF values only when writing rows. This uses
recorded device counters, without integrating sampled current or voltage.

A history sequence gap or a missing test beginning makes subsequent test and
current-cycle totals null. The device's step counters remain usable. Cycle totals
can resume at the next consecutively observed cycle transition. Negative counters
or an unexpected decrease within a step make the affected quantity unknown for
that step and any dependent totals; unaffected quantities remain available.
Unknown values are null, never zero-filled or silently rebased. A later export
recalculates from the SQLite snapshot, so filling a history gap restores totals.
The API's recorded-point pages remain voltage/current/time records; the full
capacity and energy series are provided by BDF exports, including mid-run exports.

See [recorded counter checks](protocol/counters.md) for the observed reset behavior
and portable fixtures.

## Step-boundary time correction

Some first records of a new step report both total and step elapsed time as
zero. For an isolated boundary record with consecutive preceding and following
records, the following record's total time minus its step time establishes the
new step's start time. The following record must belong to the same step
execution and have positive step time. The calculated start must lie between
the preceding and following total times. For example, surrounding totals of
10 s and 15 s with a following step time of 4 s give a boundary time of 11 s.
Sampling intervals need not be uniform.

The same correction is used in paginated `read()` results and BDF exports,
including across Parquet batch boundaries. It changes only the returned elapsed
time; the original 94-byte record remains unchanged in SQLite and in
`RecordedPoint.raw`. A genuine time zero at sequence 1 is preserved.

If an adjacent record is still missing, `read()` returns only the resolved
prefix, and the caller keeps its last returned sequence as its cursor. The
boundary point is returned once the missing neighbor arrives. An explicit
export raises `StorageError` while the boundary is unresolved and preserves
any previous file. Automatic final export waits for subsequent history without
marking the recorder as failed.

Nonzero regressions, inconsistent clocks and zero-time records that do not
match this boundary pattern are not interpolated. Export rejects unresolved
time regressions and decreasing cycle counts. Missing samples are not invented,
pauses are not removed, and device cycle numbers are not renumbered.

## Publication and completeness

Export reads a stable SQLite snapshot on a separate connection. It closes and
syncs a temporary Parquet file, then atomically replaces the requested destination.
Failure preserves the previous destination and durable source data. Independent
exports are serialized to avoid one snapshot overwriting a newer snapshot.

Finished state triggers export, but late matching records remain eligible and
refresh it. A snapshot can have sequence gaps or no records. Even contiguous
sequences starting at one do not prove the final device record was received.
`DataCompleteness` therefore exposes `UNKNOWN` or `GAPS`, never an unsupported
claim of completeness. A strict high-level workflow can require operator review
or an independently established completion criterion before publishing results.
