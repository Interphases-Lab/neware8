# Recorded capacity and energy counters

In native main-history records (`0015`, flag `55`, auxiliary ID zero), offsets
43/51 contain signed 64-bit charging/discharging capacity counters; offsets 59/67
contain signed 64-bit charging/discharging energy counters. All are little-endian.
Divide each by the record's current-range scale times 3600000 to obtain Ah or Wh.
Zero range requires the explicitly configured channel range, as for current.

## Known behavior

These counters accumulate within a step execution and reset at the next step,
even when two successive steps both charge. CC-to-CV transitions between separate
steps also reset them. A cycle's capacity and energy are sums of its step totals;
a whole test's quantities are sums over all its steps. The step counters already
include accumulation between recorded samples, so sampling intervals are not
used for integration.

Checks against saved measurements include:

| Dataset | Observations |
|---|---|
| Contact check | 141 records, seven steps: three consecutive charge steps, three discharge steps, then charge. |
| CC/CV | 17 records across two charging steps. The range changes within the CV step; scaling each record separately preserves the physical quantity. |
| Cycling | 11746 records, 99 observed step executions across 49 cycle indices. The four counters reset at step boundaries; no within-step decreases were observed in this sample. |

The portable [counter fixture](../../tests/fixtures/counters.json) contains all
contact-check and CC/CV records and the first 1182 cycling records (cycle indices
0 through 2). It records the anonymization applied to identifiers and calendar
fields. Measurements and counter bytes remain unchanged.

For example, the contact-check charge steps end with raw capacity values 999973,
2000057, 3999928 and 5999946, each at scale 100000. Whole-test charging capacity is
their sum divided by 360000000000, approximately 0.00003611084444 Ah. Summing every
sample, or keeping only the final step's value, would give an incorrect result.

## Remaining limits

The observed behavior is limited to the tested installation and ordinary
recorded steps. Missing endpoints, negative values or unexpected within-step
counter resets cannot be replaced by guessed totals. BDF exports mark affected
optional quantities null and recalculate when missing history arrives.
Schedule-specific assignment/reset semantics remain unmapped. These mappings
apply to main measurement records, not live alarm-detail slots or opaque history
record variants.
