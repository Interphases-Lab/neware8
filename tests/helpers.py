import json
import struct
from pathlib import Path

from neware8 import ChannelAddress, ChannelProfile
from neware8.messages import HistoryBatch
from neware8.protocol import Frame

FIXTURES = Path(__file__).parent / "fixtures"
HISTORY = json.loads((FIXTURES / "history_batches.json").read_text())
PROFILE = ChannelProfile(ChannelAddress(1, 1), 0, 5.0, 0.01, 10000, -10)
XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<root><config type="Step File" version="17">
<Head_Info><Operate Value="66"/><RateType Value="103"/><Scale Value="1"/><Start_Step Value="1"/></Head_Info>
<Whole_Prt><Protect><Main><Volt><Upper Value="40000"/><Lower Value="-10000"/></Volt><Curr><Upper Value="8"/><Lower Value="-8"/></Curr></Main></Protect><Record><Main><Time Value="2000"/></Main></Record></Whole_Prt>
<Step_Info Num="3"><Step1 Step_ID="1" Step_Type="2"><Limit><Main><Curr Value="1"/><Stop_Volt Value="0"/><Time Value="10000"/></Main></Limit></Step1>
<Step2 Step_ID="2" Step_Type="5"><Limit><Other><Start_Step Value="1"/><Cycle_Count Value="3"/></Other></Limit></Step2>
<Step3 Step_ID="3" Step_Type="6"/></Step_Info></config></root>"""


def captured_record() -> bytes:
    """A real 94-byte main-channel history record from the fixtures."""
    batches = (
        HistoryBatch.from_payload(Frame.from_bytes(bytes.fromhex(p["request"])).payload)
        for p in HISTORY
    )
    return next(r.raw for b in batches for r in b.records if r.data_flag == 0x55 and r.aux == 0)


def history_payload(
    *,
    test_id=1,
    sequence=1,
    current=123,
    channel=1,
    unit=1,
    time_ms=1000,
    step_id=None,
    step_type=None,
    step_change_count=None,
    step_time_ms=None,
    cycle=None,
    token=0x33221101,
):
    """A one-record history batch payload derived from a captured record."""
    raw = bytearray(captured_record())
    struct.pack_into("<BBBI", raw, 1, unit, channel, 0, test_id)
    struct.pack_into("<I", raw, 8, sequence)
    struct.pack_into("<i", raw, 35, current)
    struct.pack_into("<Q", raw, 86, time_ms)
    for offset, fmt, value in (
        (12, "I", cycle),
        (16, "B", step_id),
        (17, "B", step_type),
        (18, "B", step_change_count),
        (23, "Q", step_time_ms),
    ):
        if value is not None:
            struct.pack_into("<" + fmt, raw, offset, value)
    return struct.pack("<BI", 1, token) + bytes(raw)


def batch(payload: bytes) -> HistoryBatch:
    return HistoryBatch.from_payload(payload)
