# Commands and payloads

Offsets are relative to the payload (after the command bytes). `GUID` is 16 bytes chosen by
the host per operation. A *target* is `unit:u8, mask:u32` with bit `channel-1` set for each
channel; a target list is `count:u8` followed by targets. Classes are in `messages.py`.

## SendInfo metadata

Short `72`, controller → host, ACK `F2` unit 0. Header:

| Offset | Field |
|---|---|
| 0 | version:u16 (`0x75`) |
| 2 | copyright[6] (`Neware`) |
| 8 | format:u8, 7 on BTS82 |
| 9 | end flag: 1 more parts follow, 2 last part |
| 10 | part number, from 1 |
| 11 | body fragment |

Bodies of consecutive parts concatenate into `device type:u8, block count:u16`, then
`count` blocks of 627 bytes (`DeviceRange`):

| Offset | Field |
|---|---|
| 0..31 | channel bit mask for each unit 1..32 |
| 32 | channel type: 1 main, others auxiliary sensor types |
| 33, 37 | charge / discharge range: signed range encodings (e.g. −10) |
| 41, 45, 49 | voltage, temperature, resistance ranges |
| 53 | number of current ranges (≤ 10) |
| 54 | current ranges: 10 × i32 range encodings, e.g. −1, −5, −10 on a 10 mA channel |
| 94 | frequency type |
| 95 | unit ID:u32 |
| 99 | controller GUID (the same as in the `002A` reply) |
| 115 | 32 unit GUIDs of 16 bytes |

## Starting a test

The manufacturer client sends seven requests, each after the previous reply (reproduced
byte-exact in `tests/test_protocol.py`). The unit byte is the target unit except for `003D`.

| # | Request | Payload | Reply |
|---|---|---|---|
| 1 | short `73` channel map | `GUID, count:u16, count × (unit, channel, aux, physical)` | GUID |
| 2 | short `39` aux init | six zero bytes (`temp:i16, height, polarity, recount, connected`) | empty |
| 3 | `0036` download | `GUID, targets, total bytes:u32, steps:u16, end flag:u8, index:u16, fragment` | `GUID, targets, end flag, index` |
| 4 | `0054` per-test protections | `GUID, targets, length:u32, protection block` | `GUID, targets` |
| 5 | `003D` device protections, unit 0 | `length:u32, protection block` | empty |
| 6 | `003C` start | `GUID, targets, flag:u8 = 1` | `GUID, count:u16, count × (unit, channel, TestID:u32)` |
| 7 | `003A` confirm | `GUID, count:u16, count × (unit, channel, TestID)` | `GUID, count:u16, count × (unit, channel, result)` |

What the controller does with them:

* **Download** collects fragments; fragment 1 resets the buffer. Result 2: fragments overflow
  `total bytes`. Result 1: the final program does not decode. The manufacturer client cuts
  the program at whole step records into fragments of at most 1000 bytes; the buffer holds
  4096. A download ACK is not a readback.
* **Start does not start anything.** It *arms* each targeted channel and returns a new
  TestID per channel. It rejects the whole request if a channel is missing, running or
  already armed.
* **Confirm starts armed channels.** Per-channel result: 0 started, 1 no such channel,
  2 TestID differs from the one assigned, 3 channel not armed. A lost Confirm leaves the
  channel armed, so a second Start on it is rejected; whether the controller ever clears
  the arming by itself is unknown.

### Protection blocks

`map:u32, values in bit order, CRC32` (zlib CRC of map and values).

`003D` device-wide map — the library sends only bit 10 with value 0, as the manufacturer
client does by default:

| Bit | Setting |
|---|---|
| 0, 1 | fire temperature upper / lower |
| 2, 3 | fixed-error voltage upper / lower |
| 5 | pressure release P1, P2, T1, T2 |
| 7 | zero record |
| 8, 9 | auxiliary fixed-error voltage upper / lower |
| 10 | `PrtWhenPanErr`: protect when a fan fails; value 0 enables it |
| 11 | pressure check condition and lower voltage |
| 12 | (device type 26 only) |
| 13 | CC delay sampling |

`0054` per-test map — the library sends an empty map. Known bits: map 1 bit 0 valid aux
channel count, bit 9 aux temperature range, bit 10 scram voltage; map 2 bits 27/28
temperature early warning/warning, bit 30 free-current limit; map 3 parameters for CV
continue-cutoff delay, CCCV zero-current protection and capacity-retention cycle.

## Channel control

| Request | Payload | Reply |
|---|---|---|
| short `11` Stop | `mask:u64` | mask; a rejected Stop has an empty body |
| short `14` Continue | `mask:u64` | mask |
| `0008` ResetMidChlStatus ("Clear Flag") | `mask:u64` | mask |
| short `6F` Jump | `mask:u64, step:u8` | mask |

The unit byte selects the unit. Stop keeps the TestID and times; Continue resumes; Clear
Flag turns a stopped or protected channel into finished. There is no separate pause command
(pausing at a planned point is Reserve Pause, below).

## Settings and information

| Request | Payload | Reply |
|---|---|---|
| short `5E`, unit 0 | interval:u8, seconds | `DE`, unit `FE`, empty |
| short `03`, unit 0 | year%100, month, day, hour, minute, second, ms:u16 | `83`, unit `FE`, empty |
| short `74`, unit 0 | TestID:u32 | `F4`, the value echoed |
| short `56`, unit `FE` / `55`, unit *n* | — | NUL-terminated ASCII in 30 bytes; the controller reply may add a length-prefixed trailer |
| `0024`, unit 0 | — | status:u8; nonzero asks the host to set the clock |
| `002A`, unit 0 | — | 52 bytes, below |
| `002B`, unit *n* | — | 92 bytes, below |

The clock has no time zone or century. Dates in the serial replies are six bytes
(two-digit year, month, day, hour, minute, second; zero when unset). Text is GBK.

`002A` controller reply (`GetZwjSnResp`): GUID[16] at 0, GUID time at 16, serial number[12]
at 22, production time at 34, `Cnp`:u32 at 40, `Cnp` time at 44, `Cnp` language:u8 at 50,
compatibility:u8 at 51.

`002B` unit reply (`GetXwjSnResp`): GUID[16] at 0, GUID time at 16, serial number[12] at 22,
production time at 34, valid-until at 40, function mask:u32 at 46, KB change count:u32 at
50, first calibration at 54, latest calibration at 60, calibration software:u32 at 66,
calibration tooling:u32 at 70, calibration worker[8] at 74, `Cnp`:u32 at 82, `Cnp` time at 86.

Our controller reports factory placeholder serial numbers; the GUID time and calibration
dates decode to plausible 2022–2023 dates.

## History recovery

`000F`, unit 0, empty → `min index:u32`. `0010`, unit 0, `index:u32` → empty reply, then
the controller resends history starting with the batch whose token is `index`. The
manufacturer server requests its saved token without adding one and ignores batches until
the first one carries that token. How long the controller keeps history and what an ACK
releases are unknown.

## Reserve Pause (not tested on hardware)

The client's "reserve pause" schedules a running channel to pause at a step and cycle, at a
wall-clock time, or after a timeout. `0037`: `GUID, count:u8, count × (unit:u8, mask:u64),
action:u8 (1 set, 2 cancel), schedule`. Unlike other commands the masks are 64 bits.

Schedule: `flags:u16` (bit 0 steps, bit 1 times, bit 2 timeout, bit 3 pre-time, bit 15 set
if any), then for bit 0 `count:i32, count × (step:u16, cycle:u32)`, for bit 1
`count:i32, count × (year:i16, month, day, hour, minute, second)`, for bit 2 `timeout:u32`.
Step `FFFF` means none and `FFFE` the current step; cycle `FFFFFFFF` none and `FFFFFFFE`
the current cycle. The reply is `GUID, action:u8, count:u16, results` (result format not
decoded). `0038` reads a channel's schedule (`GUID, channel:u8` → `GUID, channel,
schedule`). Status records carry the active schedule (`reserve_pause_step/cycle/type`).

## Uploads the library does not acknowledge

The manufacturer server ACKs `0053` (external device list: `type:u8, count:u16, device
IDs`) with an empty `8053`, and `002E` (`BackupData81`: `file type, data flag, offset:u32,
length:u32, data`) with `802E` carrying the data flag. Neither appears on our controller;
the library logs a warning once if it sees them rather than claiming data it does not store.
