# Corrections and open questions

## Corrections in 0.2.0

| Earlier understanding | Corrected |
|---|---|
| Frame bytes 0..7 are a route | start flag, copied from the controller |
| `002A`/`002B` return a 16-byte identity and opaque bytes | serial-number replies: GUID, serial number, production/calibration dates |
| `0008` is "Clear Flag" | `ResetMidChlStatus`; the UI calls it Clear Flag |
| SendInfo body starts with a flag | it is the device type |
| DeviceRange `+53/+54` variable-range slots, `+95` unit mask | current-range table (signed), unit ID |
| Step records have map 1 and map 2 | up to eleven chained maps; every bit of map 1 is a field |
| Map 1 bits 5, 6, 22, 23, 28–30 unknown | power and resistance limits, BFGS protection, plateau voltages |
| Start begins the test | Start arms and assigns TestIDs; Confirm starts |
| non-`55` history records are opaque | step start/end, test end and log events with LogCode |
| Only some uploads exist | `0053` and `002E` need ACKs the library does not send; `0041`, `003B`, `0027`, `000D` need none |
| No receive timeout | the manufacturer server drops silent connections; the library now does too |
| Forbidden list: `004E`, `0096`, short `49` | also `0006` restart, `0007`, `0018`–`001A`, `0022`, `0029`, `0042`, `0044`, short `42` |

## Open questions

| Area | Known | Not known |
|---|---|---|
| Session | framing, ACKs, heartbeat period ~17 s | cold-start behaviour, the manufacturer server's default timeout |
| Start | sequence, arming and confirm results | whether firmware un-arms a channel after a lost Confirm |
| Download | fragmenting at 1000 bytes, result codes | multipart download on hardware |
| History | record kinds, tokens, resend from a token | retention limits, what an ACK releases, index wrap and epochs |
| Status header | a data pointer field, value 1 | its meaning |
| Programs | maps 1–3 and 5 | maps 4 and 6–11 (variable items) |
| Protection blocks | bit names | units and scaling of most values |
| Reserve Pause | full request layout | reply result format; not tested on hardware |
| `SendChlCtrl` (`0040`) | `channel:u8, ctrl:u8` | what `ctrl` does |
| Auxiliary inputs | types and units | wiring and physical validation |
| Electrical behaviour | — | accuracy, protection enforcement, isolation between concurrent channels |

Firmware, bootloader and calibration operations are excluded. Hardware tests need a channel
identified as safe and approval for that specific test; package tests never use a cycler.
