# Step programs

A test program is a sequence of step records, downloaded with `0036` (see
[commands](commands.md#starting-a-test)). `steps.py` encodes and decodes them; `recipe.py`
compiles manufacturer XML into them.

## Record layout

```
[size:u16][index:u16][step id:u8][mode:u8][map:u32 ...][fields][trailer:u32]
```

* `size` is the whole record. `index` is the 1-based position, `step id` the XML step ID.
* Map words follow each other **while bit 31 is set**; the controller decodes up to eleven.
  The manufacturer client always writes at least two. Ordinary programs use maps 1 and 2.
* Every other set bit adds one field, in map then bit order. Most fields are a comparison
  byte (1 =, 2 ≠, 3 >, 4 ≥, 5 <, 6 ≤) and a little-endian integer. The controller skips the
  comparison byte — it is implied by the field — but the client always writes it.
* The 4-byte trailer is 1 in every manufacturer program; its meaning is unknown.
* Accepted modes: 1–11, 13, 16–22, 24–29 (`models.StepType`); IR (12) and sub-step (23) are
  rejected.

`+cmp` marks a comparison byte.

### Map 1

| Bit | Field | Type |
|---|---|---|
| 0, 1 | current, voltage (setpoints) | i32 +cmp |
| 2, 3 | time limit (ms), capacity limit | i64 +cmp |
| 4, 5, 6 | temperature, power, resistance limits | i32 +cmp |
| 7, 8, 9 | cutoff current, cutoff voltage, cutoff ΔV | i32 +cmp |
| 10, 11, 12 | cycle start step, cycle count, jump step | i32 +cmp |
| 13, 14 | operate, rate type (from `Head_Info`) | i32 |
| 15, 16 | protection voltage upper, lower | i32 +cmp |
| 17, 18 | protection current upper, lower | i32 +cmp |
| 19, 20 | protection temperature upper, lower | i32 +cmp |
| 21 | protection delay, ms | i32 +cmp |
| 22, 23 | "BFGS" protection time, voltage | i32 +cmp |
| 24 | record interval, ms | i64 +cmp |
| 25, 26, 27 | record on Δcurrent, Δvoltage, Δtemperature | i32 +cmp |
| 28, 29, 30 | plateau voltages 1..3 | i32 +cmp |

### Map 2

| Bit | Field | Type |
|---|---|---|
| 0, 1 | auxiliary lists | 8 × i32 |
| 2..7 | aux limits: temperature, voltage, current, upper/lower | i32 +cmp |
| 8..13 | aux protections: temperature, voltage, current, upper/lower | i32 +cmp |
| 14 | aux record interval, ms | i64 +cmp |
| 15, 16, 17 | aux record on Δcurrent, Δvoltage, Δtemperature | i32 +cmp |
| 25 | protection capacity upper | i64 +cmp |
| 26 | energy limit | i64 +cmp |
| 27, 28 | aux voltage limit, aux voltage-difference protection | i32 +cmp |

Bits 18–24 exist in newer clients but BTS82 does not read them.

### Maps 3 and 5

Decoded by size; named fields include charge time, ATL protection voltage, current and
voltage absolute/percent protections, voltage jump, capacity multiples, current rise and
voltage drop protections; map 5 holds the auxiliary counterparts. Unnamed fields are
`map3_bitN`. Some are arrays of 5 or 7 i32 values.

### Maps 4 and 6..11

These hold variable-size items (length-prefixed blobs, repeated groups) and are not decoded:
a record using them fails with a clear error. The manufacturer client fills them for pulse,
simulation, pressure, temperature-box and other features this library does not compile.

## XML subset

`recipe.py` accepts manufacturer Step File XML version 17 or 18 (UTF-8 or GB2312, no DTD):
CC, CV and CCCV charge and discharge, rest, finite backward cycles and End. Every active step
needs a time limit, a record interval and voltage and current protections, from the step or
from `Whole_Prt`. Only `RateType=103` (absolute current) is accepted; C-rate metadata is
checked against the absolute current and dropped. Anything else — unknown settings, other
modes, crossing cycles, inexact conversions — fails before a byte is produced. Voltage scans
are outside this cycler's scope.

| XML | Native field | Multiplier | Comparison |
|---|---|---|---|
| `Limit/Curr` (mA) | current | current scale | ≤ charge, ≥ discharge |
| `Limit/Volt` (0.1 mV) | voltage | 1 | ≤ discharge, ≥ otherwise |
| `Limit/Time` (ms) | time limit | 1 | ≥ |
| `Limit/Stop_Volt` | cutoff voltage | 1 | ≤ CC discharge, ≥ otherwise |
| `Limit/Stop_Curr` | cutoff current | current scale | ≤ |
| `Limit/Cap` (mA·s) | capacity limit | current scale | ≥ |
| `Limit/Start_Step`, `Cycle_Count` | cycle start, cycle count | 1 | = |
| `Record/Time`, `Volt`, `Curr` | record interval, Δvoltage, Δcurrent | 1, 1, scale | ≥ |
| `Protect/Volt/Upper`, `Lower` | protection voltage | 1 | ≥, ≤ |
| `Protect/Curr/Upper`, `Lower` | protection current | scale | ≥, < |
| `Protect/Cap/Upper` | protection capacity upper (map 2) | scale | ≥ |
| `Protect/Delay_Time` | protection delay | 1 | > |

The Aurora CC/CV `Step.xml` from a manufacturer-run test compiles to the manufacturer's
189-byte program byte for byte (`tests/test_recipe.py`). A program's maximum duration counts
cycles in full.
