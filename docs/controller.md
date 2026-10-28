# Controller information and settings

All methods use the existing native TCP session and share the operation lock
with measurement control. Nothing in this section is applied automatically on
startup or reconnection.

| Method | Meaning and limits |
|---|---|
| `controller_info()` | Read the controller GUID, serial number, production time, firmware version label and raw RTC status. Nonzero `rtc_status_raw` requests clock setting; individual bits are unknown. |
| `unit_info(unit)` | Read a discovered unit's GUID, serial number, production, validity and latest calibration times and firmware label, unit 1..32. |
| `history_minimum_index()` | Read the oldest history position the controller can still resend. Read-only. |
| `set_upload_interval(interval_raw)` | Change periodic status uploads for the whole controller, in seconds (1..255). Zero is unsupported. This does not change recipe recording rules or history. |
| `set_clock(local_time)` | Set an explicit naive `datetime` in controller-local time, years 2000..2099 with millisecond precision. No timezone inference or conversion. |
| `initialize_test_id(seed, source=...)` | Advanced device-wide maximum TestID synchronization, unsigned 32-bit value. Requires a nonempty description of the complete inventory or database establishing that maximum. |

`ControllerInfo` and `UnitInfo` are frozen dataclasses. They decode the serial-number
replies (`002A`/`002B`, see [commands](protocol/commands.md#settings-and-information)) and keep
the complete replies and firmware-version payloads as bytes. Our controller reports factory
placeholder serial numbers. A firmware
version query only reads information; firmware modification is not exposed.

Clock and TestID changes require complete discovery and status within the configured
number of upload intervals for **every discovered channel**, all finished and alarm-free.
Paused, stopped, running, missing, stale and unknown channel states do not
satisfy this requirement. These checks include channels without an application
profile. TestID seeds below any locally known run or status TestID are rejected;
local observations alone cannot prove the device-wide maximum.

Successful setting calls mean the device acknowledged the write. TestID
initialization additionally requires the returned value to equal the seed.
Clock readback, setting persistence and counter wrap behavior remain unknown.
Lost or malformed acknowledgements close the connection and raise
`OperationUncertain`; there is no automatic retry. Explicit rejections raise
`OperationRejected`. Reads and writes are serialized without stopping status
or measurement reception.

The interval is in seconds. Freshness is `upload_interval_raw` (default 5)
multiplied by `missed_upload_intervals` (default 3), configured on `Client`.
The initial interval must match the controller's setting; constructing a client
does not write device settings. An acknowledged interval change updates the
freshness threshold immediately. Supply the new interval on subsequent restarts.

```python
from datetime import datetime

info = await client.controller_info()
unit = await client.unit_info(1)
await client.set_upload_interval(2)
await client.set_clock(datetime(2026, 9, 18, 14, 30))
```

Choose the clock value explicitly for the installation. These examples do not
initialize TestIDs; that requires a separately established complete inventory.
