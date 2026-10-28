# Status and history records

Live status and recorded history are separate streams. Status is a snapshot every few
seconds; history is the recorded test data. The library never fills history gaps with
status.

## Status: extended `0039`

Controller → host, header unit = lower unit, no reply. Payload: `data pointer:u32`, `count:u8`,
then `count` 100-byte records. The data pointer has the same name as the history token but is
always 1 on our controller and ignored by the manufacturer server; its meaning is unknown.

| Offset | Field | Type | |
|---|---|---|---|
| 0 | data flag | u8 | `55` for main channels |
| 1, 2 | channel, aux ID | u8 | aux 0 = main channel |
| 3 | cycle | u32 | |
| 7, 8 | step ID, step type | u8 | |
| 9 | work type | u32 | state, below |
| 13 | step time | u64 | ms |
| 21, 25 | voltage, current | i32 | 0.1 mV; current scaled by range |
| 29, 31 | resistance, temperature | u16, i16 | temperature 0.1 °C |
| 33, 41 | charge, discharge capacity | u64 | scaled by range |
| 49, 57 | charge, discharge energy | u64 | scaled by range; some alarms reuse them |
| 65 | current range | u32 | signed range encoding |
| 69 | alarm (protection) code | u32 | LogCode, `alarms.py` |
| 73 | total time | u64 | ms |
| 81, 85 | sequence, history sequence | u32 | |
| 89 | TestID | u32 | |
| 93, 95, 99 | reserve-pause step, cycle, type | u16, u32, u8 | `FFFF`/`FFFFFFFF` = none |

## History: extended `0015`

Controller → host, unit `FE`. Payload: `count:u8`, `token:u32`, then `count` 94-byte records.
The host ACKs with `FF 00 15 80`, unit 0, carrying the token. The library stores records
durably before it ACKs; an unacknowledged batch is sent again later.

| Offset | Field | Type |
|---|---|---|
| 0 | data flag (record kind, below) | u8 |
| 1, 2, 3 | unit, channel, aux ID | u8 |
| 4 | TestID | u32 |
| 8 | sequence, per test | u32 |
| 12 | cycle | u32 |
| 16, 17, 18 | step ID, step type, step change count | u8 |
| 19 | work type | u32 |
| 23 | step time, ms | u64 |
| 31, 35 | voltage, current | i32 |
| 39, 41 | resistance (or event code), temperature | u16, i16 |
| 43, 51 | charge, discharge capacity | i64 |
| 59, 67 | charge, discharge energy | i64 |
| 75 | year:u16, month, day, hour, minute, second (controller clock, no time zone) | 7 bytes |
| 82 | current range | i32 |
| 86 | total time, ms | u64 |

### Record kinds

The data flag distinguishes record kinds in one stream:

| Flag | Kind | Content |
|---|---|---|
| `55` 'U' | sample | a recorded measurement point |
| `59` 'Y' | step start | values at the start of a step |
| `58` 'X' | step end | values at the end of a step |
| `6A` 'j' | test end | |
| `AA` | event | a log event; the **resistance field holds its LogCode** |

Event codes seen in one CC/CV test: 8 Start, 645 Channel status changed, 438 Cut-off voltage
condition jump, 434 Time limit jump, 1 Finish. So the history says *why* each step ended.
The manufacturer server treats `55` with sequence 1, `6A`, and `AA` with code 29 (Move) or
2 (Stop) as test boundaries. The library records `55` samples and keeps the other kinds as
opaque rows; `HistoryRecord.event_code` decodes events.

Records are unique per unit, channel, TestID and sequence; a repeated identical record is a
duplicate, a different one is an error. Sequences can have gaps; contiguous sequences do not
prove that history is complete.

## Engineering units

* Voltage: raw / 10000 V. Temperature: raw / 10 °C. Times: raw / 1000 s.
* Current: raw / (scale × 1000) A, positive is charge. Capacity and energy (as signed
  64-bit): raw / (scale × 3 600 000) Ah or Wh.
* `scale` comes from the record's signed range encoding, converted to float32, with these
  thresholds (`measurements.current_scale`): negative magnitudes below
  10/100/1000/10⁶/10⁷/10⁸ give 100000/10000/1000/100/10⁸/10⁷, otherwise 10⁶; positive
  below 10/100/1000 give 10000/1000/100, otherwise 10. So −10 (a 10 mA channel) gives
  10000. A record with range 0 needs the channel's charge range from the metadata or the
  profile; without it current stays unknown rather than guessed.

## Work type

`work_type & 0x30` is the base state: `00` finished, `10` running, `20` stopped, `30`
protected. `work_type >> 8` selects an extended state unless it is 0 or 6: 1 paused,
2 finished, 3 offline paused, 4 beep, 5 synchronous control, 7 light, 8 vacuum, 9 release,
10 leakage rate, 11 jam, 12 wait timeout, 13 reserved pause, 14 wait start, 15 finished
with protection.

## Alarm details in energy slots

For some alarm codes the two energy fields carry alarm details instead
(`alarms.alarm_details`): pressure alarms 207/208 (start/end pressure, leakage rate), alarms
about another channel 250/251/252/533/558/597 (its unit and channel), and threshold alarms
693–696/712/713 (time, sample and threshold as floats). Energy is then reported as unknown.
