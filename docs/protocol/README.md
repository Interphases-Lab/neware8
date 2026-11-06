# Native BTS82 protocol

A best-effort description of the TCP protocol between a Neware BTS82 controller and its
host, as implemented in `src/neware8/protocol.py` and `messages.py`. It does not cover the
BTS Client-to-Server API, UDP 8002, named pipes or the server's NDC file downloads.

Not every documented behaviour has been verified on hardware, and the real controller
firmware may differ. Hardware testing is limited to one installation (ten eight-channel units,
5 V / 10 mA, current scale 10000). See [open questions](evidence.md#open-questions) for what
remains unknown.

- [Commands and payloads](commands.md)
- [Status and history records](records.md)
- [Step programs](programs.md)
- [Capacity and energy counters](counters.md)
- [Corrections and open questions](evidence.md)

## Framing

The controller opens a TCP connection to the host, port 8001. Frames can span TCP segments
and segments can carry several frames. All integers are little-endian.

```
[start flag ×8][device][unit][length:u16][command ...][payload][checksum][0x5A]
```

| Offset | Field |
|---|---|
| 0..7 | Start flag. Constant per controller; replies copy it. |
| 8 | Device ID |
| 9 | Unit: lower unit 1..32, 0 or `FE` for the controller itself |
| 10 | Length of the whole frame |
| 12 | Command, in one of four forms (below) |
| −2 | Sum of bytes 8 .. −3, modulo 256 |
| −1 | `5A` |

| Form | Bytes at 12 | Used for |
|---|---|---|
| short request | `cmd` (< `7F`) | host requests; controller uploads (Connect, SendInfo, heartbeat) |
| short reply | `cmd|80`, `result` | |
| extended request | `7F`, `cmd:u16` | host requests; controller uploads (status, history) |
| extended reply | `FF`, `result`, `cmd|8000:u16` | |

Result 0 is success. Short and extended commands are separate number spaces: short `39`
initialises auxiliary inputs, extended `0039` is the status upload.

## Session

| Step | Behaviour |
|---|---|
| Connect | Controller sends short `00`, body `client type:u8` (24 on BTS82) and `version:u16` (`0x011A`). Host replies `80`, same unit (`FE`), no body. |
| Metadata | Controller sends short `72` (SendInfo), possibly in several parts; host ACKs each with `F2`, **unit 0**. See [commands](commands.md#sendinfo-metadata). |
| Identity | The manufacturer server then reads the controller serial number (`002A`). This library binds its storage to the GUID in that reply. |
| Status | Controller uploads `0039` per unit every *interval* seconds (settable, `5E`). No reply. |
| History | Controller uploads `0015` batches; each needs an ACK carrying its token. Unacknowledged batches are resent. After every connect the host sends `0010` with its last token, because the controller skips batches that were in flight when the connection dropped ([records](records.md#history-extended-0015)). |
| Heartbeat | Controller sends short `30`, empty, about every 17 s, also while status flows. No reply. |
| Timeout | The manufacturer server drops a connection after a period without data, so a reconnecting controller is accepted. This library does the same after `receive_timeout_s` (60 s). |

## Command catalogue

Direction: → host to controller, ← controller to host. "Library" says whether `neware8`
sends or handles the command. Names are the manufacturer's.

### Short commands

| Cmd | Name | Dir | Purpose | Library |
|---|---|---|---|---|
| `00` | Connect | ← | session start | answered |
| `03` | SetRTC | → | set controller clock | `set_clock` |
| `04`/`05` | Get/SetSLStatus70 | → | alarm sound/light status (simulator `OnCmdGet/SetAlarmSl`) | — |
| `0A` | Lighten | → | locate light | — |
| `11` | Stop | → | stop channels | `control` |
| `12` | BroadcastStop | → | stop everything | — |
| `14` | Continue | → | resume stopped channels | `control` |
| `20` | SetDevID | → | change the device byte (persistent) | — |
| `30` | HeartBeat | ← | keep-alive | counted |
| `33` | GetSNZwj | → | legacy serial query | — |
| `39` | SetBgsAux | → | aux init, six zero bytes before every start | start |
| `42` | Cal_SetZeroMVI | → | calibration | **forbidden** |
| `49` | UpdateZwj | → | controller firmware | **forbidden** |
| `55` | GetVerXwj | → | unit firmware version | `unit_info` |
| `56` | GetVerZwj | → | controller firmware version | `controller_info` |
| `58`/`59` | Get/SetPlatVol | → | plateau voltage | — |
| `5E` | SetRTInterval | → | status upload interval, seconds | `set_upload_interval` |
| `6F` | Jump70 | → | jump to a step | `control` |
| `72` | SendInfo70 | ← | channel/range metadata | answered |
| `73` | SetChlMap70 | → | channel → physical map, before start | start |
| `74` | SetTestID70 | → | device-wide TestID counter | `initialize_test_id` |

### Extended commands

BTS82 also accepts the older generations' commands: the manufacturer server's BTS82 class
reuses its BTS80 handlers (Move, History Continue, Modify Step, Occupy), and the simulator's
BTS82 dispatcher falls through to BTS81 … BTS70. Payloads of the rows marked "—" are not
decoded unless stated in [commands](commands.md).

| Cmd | Name | Dir | Purpose | Library |
|---|---|---|---|---|
| `0006` | SetMidReStart | → | restart the controller | **forbidden** |
| `0007` | UpdateXWJ | → | unit firmware | **forbidden** |
| `0008` | ResetMidChlStatus | → | reset channel status (the UI's "Clear Flag") | `control` |
| `000C` | BonDiskCtrl78 | → | disk control | — |
| `000D` | BonDiskStatus78 | ← | disk status | ignored |
| `000F` | GetDfMinIndex | → | oldest resendable history position | `history_minimum_index` |
| `0010` | SetReSendDfData | → | resend history from a position | after every connect; `resume_history` |
| `0015` | SendDFData79 | ← | history batch, ACKed | stored, ACKed |
| `0016`/`0017` | TestPing/TestNda | → | | — |
| `0018`–`001A` | backup files | ↔ | file transfer | **forbidden** |
| `001B`, `001E` | SetStep80, SetStart80 | → | BTS80 download and start; BTS82 uses `0036`/`003C` | — |
| `001C` | SetTempCtrlBoardMap | → | temperature control board map | — |
| `001D` | SetSimData80 | → | simulation-step data | — |
| `001F` | SetMove | → | continue a test on another channel (*History Continue*) | — |
| `0020` | SpecialContinue | → | continue a test (*History Continue*) | — |
| `0021` | StepModify | → | edit a running test's program (*Modify Step*) | — |
| `0022` | ForwardCali | → | calibration | **forbidden** |
| `0023` | OccupyXwj | → | occupy a unit | — |
| `0024` | GetRtcStatus | → | does the controller want its clock set | `controller_info` |
| `0025` | SetRtcStatus | → | | — |
| `0027` | DfStatus80 | ← | history store sizes (total, sent, free) | ignored |
| `0029` | SetFile | → | file transfer | **forbidden** |
| `002A` | GetZwjSn | → | controller GUID, serial number, dates | `controller_info` |
| `002B` | GetXwjSn | → | unit GUID, serial number, calibration dates | `unit_info` |
| `002E` | BackupData81 | ← | data backup, ACKed with its data flag | logged, not ACKed |
| `002F`/`0030` | Lighten81/BonCtrl81 | → | locate light, shelf control | — |
| `0036` | SetStep82 | → | download a step program | start |
| `0037` | ReserveStop82 | → | schedule a pause (step/cycle/time) | codec only |
| `0038` | GetReserveStop82 | → | read the schedule | — |
| `0039` | SendRTData82 | ← | status upload | decoded |
| `003A` | StartConfirm | → | start armed channels | start |
| `003B` | BonStatusN2 | ← | shelf/pressure status | ignored |
| `003C` | SetStart82 | → | arm channels, assign TestIDs | start |
| `003D` | SetGrobalProtect82 | → | device-wide protections | start |
| `003E` | SetLeakRate82 | → | leak-rate test settings | — |
| `003F` | SendTempMap | → | temperature-box map | — |
| `0040` | SendChlCtrl | → | `(channel, ctrl)`; purpose unknown | — |
| `0041` | ZwjTotalRT | ← | fans, temperatures, disks | ignored |
| `0042` | TftpFile | → | file transfer | **forbidden** |
| `0043` | DBC | → | CAN DBC (simulator `OnCmd_DBC`) | — |
| `0044` | Cali | → | calibration | **forbidden** |
| `0045` | GetXwjVerEx | → | all unit versions | — |
| `0047`/`0048` | SetDBC/SendDbc | → | CAN DBC configuration | — |
| `0051`/`0052` | SendOtherDevMap/Ctrl | → | external devices | — |
| `0053` | UploadOtherDevInfo | ← | external device list, ACKed | logged, not ACKed |
| `0054` | SetStepGlobal | → | per-test protections | start |

The library never builds a forbidden command (`Frame.to_bytes` refuses). Supervisory `004E`
and `0096` belong to the Client-to-Server layer and are also excluded.
