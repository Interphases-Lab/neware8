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

## Corrections after 0.2.0

From the simulator's history sender (`CSendDfDataToServer`) and the manufacturer server's
BTS82 class; checked against the captures (`neware8-reverse-engineering/analysis/
rea_followup_20261106.md`).

| Earlier understanding | Corrected |
|---|---|
| The history token is opaque | read position: 94 bytes per record, file number above bit 23 |
| History resumes where it stopped after a reconnect | it resumes after the last batch *sent*; in-flight batches are skipped unless the host sends `0010` (observed). The library now does so after every connect |
| A resend position the controller no longer holds fails | simulator restarts from the oldest retained history |
| `001B`–`0021` belong to an older generation only | BTS82 accepts them; Move, History Continue and Modify Step are live client features |
| `04`/`05` purpose unknown | alarm sound/light status |
| `003C` ends in a flag, always 1 | the start step ID (`m_btStepID`, default 1); starting elsewhere is untested |
| History after finalization always fails | resent records the archive already holds are ignored; new ones still fail |
| Channel profiles are site configuration | derived from SendInfo ranges and address order (`neware8.profiles`); configured profiles only override |

## Open questions

| Area | Known | Not known |
|---|---|---|
| Session | framing, ACKs, heartbeat period ~17 s | cold-start behaviour, the manufacturer server's default timeout |
| Start | sequence, arming and confirm results | whether firmware un-arms a channel after a lost Confirm |
| Download | fragmenting at 1000 bytes, result codes | multipart download on hardware |
| History | record kinds, tokens as byte positions, resend from a token, reconnect skips in-flight batches (observed once), resend of unacknowledged batches | retention limits, file rollover, window size and resend interval on firmware, behaviour for an expired position on firmware |
| Continue after protection | manufacturer client allows it (plain `14`); time freezes while stopped (observed) | firmware behaviour on a protected channel |
| Move, Modify Step | payloads and continue types decoded (`neware8-reverse-engineering/analysis/rea_followup_20261106.md`) | not implemented; untested |
| History Continue | needs the run's ES state file, which only BTS-Plus controllers produce; our firmware also lacks capability bit 23 | not available on this controller, even in the manufacturer software |
| Status header | a data pointer field, value 1 | its meaning |
| Programs | maps 1–3 and 5 | maps 4 and 6–11 (variable items) |
| Protection blocks | bit names | units and scaling of most values |
| Reserve Pause | full request layout | reply result format; not tested on hardware |
| `SendChlCtrl` (`0040`) | `channel:u8, ctrl:u8` | what `ctrl` does |
| Auxiliary inputs | types and units | wiring and physical validation |
| Electrical behaviour | — | accuracy, protection enforcement, isolation between concurrent channels |

Firmware, bootloader and calibration operations are excluded. Hardware tests need a channel
identified as safe and approval for that specific test; package tests never use a cycler.
