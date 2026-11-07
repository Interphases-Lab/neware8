# History: delivery and decoding

neware8 is the driver. It delivers history to the application and keeps no measurement data itself. The application stores what it needs, and neware8 provides pure functions that decode the records the way the device means them.

## The `HistorySink` contract

`Client(history=sink)` takes an object with four async methods (`neware8.history.HistorySink`):

| Method | Called | The application must |
|---|---|---|
| `bind(controller)` | After every connect, with the 16-byte controller GUID, before any history is handled | Refuse (raise) if its data belongs to another controller |
| `register(tests)` | In the Start reply, before any later frame is read | Durably record the new `StartedTest`s (address, TestID, physical ID, charge range, start time), so it can keep their history |
| `commit(batch)` | For every history batch after binding | Durably store the records it keeps **and** `batch.token`, before returning |
| `checkpoint()` | After every connect, and by `resume_history()` | Return the last committed `batch.token`, or None |

The client acknowledges a batch only after `commit` returned. A refused `bind` or `commit` ends the session without an acknowledgement; the controller sends the batch again after the reconnect. A refused `register` fails the start, whose outcome is then uncertain. Batches that arrive before `bind` completed are not acknowledged.

What a sink keeps is its own decision. Typically it keeps the main records (`history.is_main`) whose `(unit, channel, test_id)` belongs to a test it registered, keyed by `sequence`. Auxiliary and event records can be ignored.

Two device behaviours a sink must handle:

- **Resends.** After every connect the client asks the controller to resend from the checkpoint. The controller continues after the last batch it *sent*, so batches that were in flight when the connection dropped would otherwise be skipped. The checkpoint is the start of the last committed batch, so a resend repeats that batch. A record that is already stored with identical bytes must be accepted again. The same sequence with different bytes is a conflict; refuse the whole batch.
- **Expired positions.** If the controller no longer holds the checkpoint, it resends from the oldest history it has (observed in the simulator). The client stores whatever arrives first and logs an error, because records in between may be lost. A position that skips forward within a session is logged as a gap.

Commit records and the checkpoint in one transaction, or records first. A crash between the two then only causes a resend, which the duplicate rule makes safe. The controller's retention limits and whether its final history is complete are unknown: contiguous sequences starting at one do not prove that the last record was received.

## Decoding

`history.recorded_point(record, charge_range)` scales one record:

- voltage: 0.1 mV / 10000
- current: by the record's current range, positive when charging
- total time: ms / 1000

A record with a zero current range needs the channel's `charge_range_raw`; without it, current is None.

`history.decode(records, charge_range=..., previous=..., totals=...)` decodes consecutive stored records of one test, in ascending sequence order. It applies the boundary correction below and checks that time never decreases. With a `RunningTotals` it also fills each point's whole-test totals and step count. Decoding stops before a boundary record whose neighbours are not stored yet. Keep the last yielded sequence as the cursor, and continue from it with `previous` set to the record just before the next one. `RunningTotals` objects can be copied to resume totals at any position.

### Step-boundary time correction

Some first records of a new step report both total and step elapsed time as zero. For an isolated boundary record with consecutive preceding and following records, the following record's total time minus its step time establishes the new step's start time. Conditions:

- The following record must belong to the same step execution and have a positive step time.
- The calculated start must lie between the preceding and following total times.

For example, surrounding totals of 10 s and 15 s with a following step time of 4 s give a boundary time of 11 s. Sampling intervals need not be uniform. Only the decoded time changes; the original record bytes stay unchanged. A genuine time zero at sequence 1 is preserved.

Nonzero regressions, inconsistent clocks, and zero-time records that do not match this pattern are not interpolated; `decode` raises `StorageError`. Missing samples are not invented, pauses are not removed, and device cycle numbers are not renumbered.

### Capacity and energy

The four history counters are **step totals**. They reset at each step execution, including consecutive steps in the same direction and CC-to-CV transitions. Charging and discharging counters are separate positive magnitudes. They are not whole-test totals, and successive samples must not be added together.

`CounterTotals` adds each completed step's final counters once, then the current step's counters, giving step, cycle and test totals together with their net and cumulative values. Whole-test totals continue across cycles, and cycle totals restart when the device cycle index changes. Repeated program step IDs are told apart by the cycle index and the eight-bit step-change counter (`history.occurrence`). Each sample is scaled by its own range, including automatic range changes within a step. Sums are exact rationals until they are reported.

Missing data makes totals unknown (None), never zero-filled or silently rebased:

- A sequence gap or a missing test beginning makes the following test and current-cycle totals None. The step counters remain usable, and cycle totals resume at the next consecutively observed cycle transition.
- A negative counter, or an unexpected decrease within a step, makes the affected quantity unknown for that step and any totals that depend on it. Unaffected quantities remain available.

Recomputing after a gap was filled restores the totals. `RunningTotals` reports the whole-test totals and the number of step executions observed; after a gap that count is a lower bound.

See [recorded counter checks](protocol/counters.md) for the observed reset behaviour and portable fixtures.

### Mapping to the Battery Data Format

For applications that export BDF, these are the established sources:

| BDF column | Source |
|---|---|
| `test_time_second` | Total milliseconds / 1000, with the boundary correction |
| `voltage_volt` | Signed native voltage / 10000 |
| `current_ampere` | Signed native current / (range scale * 1000); positive means charging |
| `cycle_count`, `step_id` | Device cycle index and program step identifier, without renumbering |
| `step_time_second` | Step milliseconds / 1000 |
| `step_type` | `StepType(record.step_type)`; unknown modes have no name |
| Step, cycle and test capacities and energies | `CounterTotals` |

Not established from the record fields:

| BDF quantity group | Reason |
|---|---|
| `unix_time_second` | Device calendar fields have no established time zone; host receipt time is not the measurement time |
| Ambient, surface and T1-T5 temperatures | The main record temperature has no established sensor placement or auxiliary-channel identity |
| Internal resistance and impedance | The history resistance field's unit and measurement method are not established; no validated impedance series is available |
| Pressure and frequency | No corresponding validated main-history measurements |
| Schedule capacity/energy | Separate schedule-defined assignment events are not mapped |
