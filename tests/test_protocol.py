"""Framing and payload codecs against captured manufacturer traffic."""

import json
import struct
from datetime import datetime

import pytest

from neware8 import ChannelState, ProtocolError
from neware8 import messages as msg
from neware8.measurements import channel_state, current_scale, readings
from neware8.protocol import Extended, Frame, FrameReader, Short
from neware8.session import Session
from neware8.steps import Comparison, Step, Value, decode_program, encode_program, split_records
from helpers import FIXTURES, HISTORY

FRAMES = json.loads((FIXTURES / "fixtures_metadata_rtc.json").read_text())["frames"]
START = json.loads((FIXTURES / "start_sequence.json").read_text())["events"]
LIFECYCLE = json.loads((FIXTURES / "lifecycle.json").read_text())["events"]
RECOVERY = json.loads((FIXTURES / "indexed_recovery.json").read_text())
STEPS = json.loads((FIXTURES / "native_steps.json").read_text())["native"]


def frame(hex_text: str) -> Frame:
    return Frame.from_bytes(bytes.fromhex(hex_text))


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(FRAMES))
def test_captured_frames_round_trip(name):
    raw = bytes.fromhex(FRAMES[name])
    assert Frame.from_bytes(raw).to_bytes() == raw


def test_frame_forms():
    connect = frame(FRAMES["command_0000"])
    assert (connect.extended, connect.command, connect.reply, connect.unit) == (
        False,
        0,
        False,
        0xFE,
    )
    reply = frame(FRAMES["command_8072"])
    assert reply.key == (False, Short.SEND_INFO, True) and reply.result == 0
    query = frame(FRAMES["command_002a"])
    assert query.key == (True, Extended.GET_CONTROLLER_SERIAL, False) and query.payload == b""
    status = frame(FRAMES["rt_unit_21"])
    assert status.key == (True, Extended.STATUS, False) and status.unit == 21


def test_start_flag_is_copied_into_replies():
    upload = frame(HISTORY[0]["request"])
    ack = upload.answer(msg.HistoryBatch.ack_payload(0), unit=0)
    assert ack.start_flag == upload.start_flag and ack.device == upload.device


@pytest.mark.parametrize("size", [1, 2, 11, 12, 15, 31, 4096])
def test_reader_handles_fragmented_and_coalesced_frames(size):
    expected = [bytes.fromhex(pair["request"]) for pair in HISTORY]
    stream, combined, frames = FrameReader(), b"".join(expected), []
    for offset in range(0, len(combined), size):
        frames += stream.feed(combined[offset : offset + size])
    stream.finish()
    assert [f.to_bytes() for f in frames] == expected


@pytest.mark.parametrize("corruption", ["length", "checksum", "terminator", "partial"])
def test_reader_rejects_malformed_frames(corruption):
    raw = bytearray.fromhex(HISTORY[0]["request"])
    if corruption == "length":
        raw[10:12] = b"\x01\x00"
    elif corruption == "checksum":
        raw[-2] ^= 1
    elif corruption == "terminator":
        raw[-1] = 0
    else:
        raw = raw[:-1]
    stream = FrameReader()
    with pytest.raises(ProtocolError):
        stream.feed(bytes(raw))
        stream.finish()


@pytest.mark.parametrize(
    "command",
    [
        Extended.SET_CONTROLLER_RESTART,
        Extended.UPDATE_UNIT,
        Extended.SET_FILE,
        Extended.TFTP_FILE,
        Extended.CALIBRATE,
    ],
)
def test_forbidden_extended_commands_are_never_built(command):
    with pytest.raises(ValueError, match="forbidden"):
        Frame(bytes(8), 1, 0, command, extended=True).to_bytes()


@pytest.mark.parametrize("command", [Short.UPDATE_CONTROLLER, Short.CALIBRATE_ZERO])
def test_forbidden_short_commands_are_never_built(command):
    with pytest.raises(ValueError, match="forbidden"):
        Frame(bytes(8), 1, 0, command).to_bytes()


def test_unknown_commands_stay_raw():
    unknown = Frame(bytes(8), 1, 1, 0x7777, b"abc", extended=True)
    decoded = Frame.from_bytes(unknown.to_bytes())
    assert decoded == unknown and decoded.name == "0x7777"


# --------------------------------------------------------------------------- #
# Session and uploads
# --------------------------------------------------------------------------- #
def test_session_answers_connect_and_metadata_as_captured():
    session = Session()
    assert [f.to_bytes().hex() for f in session.receive(frame(FRAMES["command_0000"]))] == [
        FRAMES["command_8000"]
    ]
    assert session.connect == msg.Connect(client_type=24, version=0x011A)
    assert [f.to_bytes().hex() for f in session.receive(frame(FRAMES["command_0072"]))] == [
        FRAMES["command_8072"]
    ]
    assert session.info is not None and len(session.channels) == 80


def test_metadata_block_fields():
    info = frame(FRAMES["command_0072"])
    fragment = msg.InfoFragment.from_payload(info.payload)
    assert (
        fragment.version,
        fragment.copyright,
        fragment.format,
        fragment.final,
        fragment.part,
    ) == (0x75, b"Neware", 7, True, 1)
    block = msg.DeviceInfo.from_body(fragment.body).blocks[0]
    assert block.channel_type == msg.DeviceInfo.MAIN
    assert block.charge_range == -10 and block.current_ranges == (-1, -5, -10)


def test_multipart_metadata_must_be_consecutive():
    body = msg.InfoFragment.from_payload(frame(FRAMES["command_0072"]).payload).body
    header = struct.pack("<H6sB", 0x75, b"Neware", 7)
    assembler = msg.InfoAssembler()
    assert assembler.add(msg.InfoFragment.from_payload(header + b"\x01\x01" + body[:700])) is None
    info = assembler.add(msg.InfoFragment.from_payload(header + b"\x02\x02" + body[700:]))
    assert info is not None and len(info.main_channels()) == 80
    with pytest.raises(ProtocolError, match="out of sequence"):
        assembler.add(msg.InfoFragment.from_payload(header + b"\x02\x03" + body))


def test_status_record_layout():
    upload = msg.StatusUpload.from_payload(frame(FRAMES["rt_unit_21"]).payload)
    assert len(upload.records) == 8
    record = upload.records[0]
    assert record.channel == 1 and record.aux == 0 and record.data_flag == 0x55
    assert record.reserve_pause_step in (0, msg.PauseSchedule.NO_STEP)


def test_history_batches_and_acks_match_captures():
    for pair in HISTORY:
        upload = frame(pair["request"])
        batch = msg.HistoryBatch.from_payload(upload.payload)
        ack = upload.answer(msg.HistoryBatch.ack_payload(batch.token), unit=0)
        assert ack.to_bytes().hex() == pair["ack"]


def test_history_record_kinds_and_events():
    records = [
        r
        for pair in HISTORY
        for r in msg.HistoryBatch.from_payload(frame(pair["request"]).payload).records
    ]
    kinds = {r.data_flag for r in records}
    assert kinds == {
        msg.RecordKind.SAMPLE,
        msg.RecordKind.EVENT,
        msg.RecordKind.STEP_START,
        msg.RecordKind.STEP_END,
        msg.RecordKind.TEST_END,
    }
    events = [r.event_code for r in records if r.data_flag == msg.RecordKind.EVENT]
    assert events[0] == 8 and 434 in events and 1 in events  # Start, time-limit jump, Finish


# --------------------------------------------------------------------------- #
# Starting a test (captured manufacturer start, re-encoded byte for byte)
# --------------------------------------------------------------------------- #
def test_manufacturer_start_sequence_reencodes_exactly():
    requests = [frame(e["raw_hex"]) for e in START if e["source"] == "server"]
    replies = [frame(e["raw_hex"]) for e in START if e["source"] == "controller"]
    keys = [(f.extended, f.command) for f in requests]
    assert keys == [
        (False, Short.SET_CHANNEL_MAP),
        (False, Short.SET_AUX),
        (True, Extended.DOWNLOAD_STEPS),
        (True, Extended.SET_TEST_PROTECTION),
        (True, Extended.SET_GLOBAL_PROTECTION),
        (True, Extended.START),
        (True, Extended.CONFIRM_START),
    ]
    mapping, aux, download, protection, global_, start, confirm = requests
    assert msg.ChannelMap.from_payload(mapping.payload).to_payload() == mapping.payload
    assert msg.AuxInit.from_payload(aux.payload) == msg.AuxInit()
    assert msg.StepDownload.from_payload(download.payload).to_payload() == download.payload
    assert msg.TestProtection.from_payload(protection.payload).to_payload() == protection.payload
    assert msg.GlobalProtection.pan_error_protection().to_payload() == global_.payload
    guid = mapping.payload[:16]
    targets = (msg.Target(1, 1),)
    assert msg.Start(guid, targets).to_payload() == start.payload
    assert msg.ConfirmStart.from_payload(confirm.payload).to_payload() == confirm.payload

    assert replies[0].payload == guid
    assert msg.StepDownloadReply.from_payload(replies[2].payload) == msg.StepDownloadReply(
        guid, targets, True, 1
    )
    assert replies[3].payload == msg.TestProtection(guid, targets).reply_payload()
    started = msg.StartReply.from_payload(replies[5].payload, replies[5].result)
    assert started.channels == msg.ConfirmStart.from_payload(confirm.payload).channels
    assert msg.ConfirmStartReply.from_payload(replies[6].payload).channels == (
        msg.ChannelResult(1, 1, 0),
    )


def test_program_split_keeps_whole_records():
    records = [bytes([10, 0]) + bytes(8)] * 250  # 10-byte records, 2500 bytes
    program = b"".join(records)
    fragments = msg.split_program(program, records)
    assert [len(f) for f in fragments] == [1000, 1000, 500] and b"".join(fragments) == program


# --------------------------------------------------------------------------- #
# Lifecycle, settings and replies
# --------------------------------------------------------------------------- #
def test_lifecycle_selections_match_captures():
    for event in LIFECYCLE:
        captured = frame(event["raw_hex"])
        if captured.payload or not captured.result:
            selection = msg.Selection.from_payload(captured.payload)
            assert selection.mask == 1 and selection.to_payload() == captured.payload


def test_rtc_payload_matches_capture():
    captured = frame(FRAMES["command_0003"])
    when = datetime(2026, 7, 24, 14, 47, 37, 7000)
    assert msg.rtc_payload(when) == captured.payload
    assert msg.parse_rtc_payload(captured.payload) == when
    with pytest.raises(ValueError):
        msg.rtc_payload(datetime(1999, 1, 1))


def test_indexed_recovery_requests_match_fixture():
    start_flag = bytes.fromhex(RECOVERY["identity"][0].replace(" ", ""))
    device = int(RECOVERY["identity"][1])
    minimum = Frame(start_flag, device, 0, Extended.GET_HISTORY_MIN_INDEX, extended=True)
    resend = Frame(
        start_flag,
        device,
        0,
        Extended.RESEND_HISTORY,
        RECOVERY["index"].to_bytes(4, "little"),
        extended=True,
    )
    assert minimum.to_bytes().hex() == RECOVERY["minimum_request"]
    assert resend.to_bytes().hex() == RECOVERY["resend_request"]


def serial_payload(serial: str, size: int) -> bytes:
    payload = bytearray(size)
    payload[:16] = bytes(range(16))
    payload[16:22] = bytes([22, 10, 20, 14, 8, 59])
    payload[22 : 22 + len(serial)] = serial.encode()
    return bytes(payload)


def test_serial_replies():
    controller = msg.ControllerSerial.from_payload(serial_payload("PSN000000001", 52))
    assert controller.serial_number == "PSN000000001"
    assert controller.guid_time.to_datetime() == datetime(2022, 10, 20, 14, 8, 59)
    assert controller.production_time.to_datetime() is None  # unset
    unit = msg.UnitSerial.from_payload(serial_payload("UNIT01", 92))
    assert unit.serial_number == "UNIT01" and len(unit.raw) == 92


def test_controller_version_fixture():
    fixture = json.loads((FIXTURES / "controller_info.json").read_text())["frames"]
    controller = frame(fixture["controller_version"])
    assert msg.version_text(controller.payload) == "4S_2.15.6.0_20220517_095718"
    assert (
        msg.version_text(frame(fixture["unit_version"]).payload) == "M04310106_221114_111541_FD3F1"
    )


def test_reserve_pause_payload():
    schedule = msg.PauseSchedule(steps=((3, msg.PauseSchedule.CURRENT_CYCLE),))
    assert schedule.to_bytes() == struct.pack("<HiHI", 0x8001, 1, 3, 0xFFFFFFFE)
    request = msg.ReservePause(bytes(16), ((1, 1),), msg.ReservePause.SET, schedule)
    assert request.to_payload()[16:27] == b"\x01\x01" + (1).to_bytes(8, "little") + b"\x01"
    assert msg.PauseSchedule().to_bytes() == b"\x00\x00"


# --------------------------------------------------------------------------- #
# Step records
# --------------------------------------------------------------------------- #
def test_captured_program_decodes_and_reencodes_exactly():
    program = bytes.fromhex(STEPS["raw_hex"])
    steps = decode_program(program)
    assert [s.mode for s in steps] == [s["step_type"] for s in STEPS["steps"]]
    assert encode_program(steps) == program
    first = steps[0]
    assert first.values["time_ms"].comparison == Comparison.GREATER_OR_EQUAL
    assert "operate" in first.values and first.values["operate"].comparison is None


def test_step_maps_chain_and_unknown_bits_fail():
    step = Step(
        1,
        1,
        1,
        {
            "current": Value(10, Comparison.LESS_OR_EQUAL),
            "charge_time": Value(5, Comparison.GREATER_OR_EQUAL),
        },
    )
    raw = step.to_bytes()
    maps = struct.unpack_from("<3I", raw, 6)
    assert maps == (1 | 1 << 31, 1 << 31, 1)  # map 3 needs maps 1 and 2 to continue
    assert Step.from_bytes(raw) == step
    corrupt = bytearray(raw)
    struct.pack_into("<I", corrupt, 14, 1 << 3)  # map 3 bit 3 is 5 x i32: overruns
    with pytest.raises(ProtocolError):
        Step.from_bytes(bytes(corrupt))
    with pytest.raises(ValueError, match="unknown"):
        Step(1, 1, 1, {"bogus": Value(1)}).to_bytes()


def test_program_splitting_rejects_garbage():
    with pytest.raises(ProtocolError):
        split_records(b"\x05\x00abc")


# --------------------------------------------------------------------------- #
# Interpretation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [
        (0x10, ChannelState.RUNNING),
        (0x20, ChannelState.STOPPED),
        (0x30, ChannelState.PROTECTED),
        (0x100, ChannelState.PAUSED),
        (0x200, ChannelState.FINISHED),
        (0x300, ChannelState.OFFLINE_PAUSED),
        (0x400, ChannelState.BEEP),
        (0x500, ChannelState.SYNCHRONOUS_CONTROL),
        (0x610, ChannelState.RUNNING),
        (0x700, ChannelState.LIGHT),
        (0xD00, ChannelState.RESERVED_PAUSE),
        (0xF00, ChannelState.FINISHED_PROTECTED),
        (0x1001, ChannelState.UNKNOWN),
    ],
)
def test_work_type_states(raw, expected):
    assert channel_state(raw) == expected


@pytest.mark.parametrize(
    "range_raw, scale",
    [(-10, 10000), (-1, 100000), (-5, 100000), (-100, 1000), (10, 1000), (1, 10000)],
)
def test_current_scale(range_raw, scale):
    assert current_scale(range_raw & 0xFFFFFFFF) == scale


def test_readings_need_a_range_for_current():
    record = msg.StatusUpload.from_payload(frame(FRAMES["rt_unit_23"]).payload).records[0]
    values = readings(record)
    assert values.voltage_v == record.voltage_raw / 10000
    if record.range_raw == 0:
        assert values.current_a is None
        assert readings(record, -10).current_a == record.current_raw / 1_000_000


def test_alarm_details_reuse_energy_slots():
    from neware8.alarms import alarm_details, overloaded_energy

    assert overloaded_energy(207) == {"charge_energy", "discharge_energy"}
    pressure = alarm_details(207, (1000000 << 32) | 900000, 50000)
    assert pressure == {
        "start_pressure_kpa": 100.0,
        "end_pressure_kpa": 90.0,
        "leakage_rate_kpa_per_min": 5.0,
    }
    assert alarm_details(250, 0, 0x0305) == {"other_unit": 3, "other_channel": 5}
    assert alarm_details(1, 0, 0) is None
