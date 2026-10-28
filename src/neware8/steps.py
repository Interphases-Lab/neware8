"""
Step records of a BTS82 test program (the payload assembled from DOWNLOAD_STEPS fragments).

A record is::

    [size:u16][index:u16][step id:u8][mode:u8][map:u32 ...][fields][trailer:u32]

Map words follow each other while bit 31 is set (up to eleven on BTS82). Every other set bit
adds one field, in bit order. Most fields are a comparison byte followed by a little-endian
integer; the controller skips the comparison byte (it is implied by the field), but the
manufacturer client always writes it.

Field sizes come from the controller simulator's map decoders (CMiddleSocket82, vtable
0xE4..0x10C); names from the client's Common.StepItem.DecodeMapN. Maps 4 and 6..11 contain
variable-size items and are not decoded: records using them fail explicitly.
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum

from .models import StepType
from .protocol import ProtocolError

CONTINUE = 1 << 31
HEADER = struct.Struct("<HHBB")


class Comparison(IntEnum):
    EQUAL = 1
    NOT_EQUAL = 2
    GREATER = 3
    GREATER_OR_EQUAL = 4
    LESS = 5
    LESS_OR_EQUAL = 6


# Step modes the BTS82 controller accepts (simulator DeCodeStepFile); IR (12) and others are not.
ACCEPTED_MODES = frozenset(StepType) - {
    StepType.UNDEFINED,
    StepType.INTERNAL_RESISTANCE,
    StepType.SUBSTEP,
}


@dataclass(frozen=True)
class Field:
    name: str
    format: str  # struct code of one element
    comparison: bool = True
    count: int = 1  # >1: array of `format` elements

    @property
    def size(self) -> int:
        return self.count * struct.calcsize("<" + self.format) + int(self.comparison)


def _i32(name: str) -> Field:
    return Field(name, "i")


def _i64(name: str) -> Field:
    return Field(name, "q")


# Map number -> bit -> field. Bit 31 of every map is the continuation flag.
FIELDS: dict[int, dict[int, Field]] = {
    1: {
        0: _i32("current"),
        1: _i32("voltage"),
        2: _i64("time_ms"),
        3: _i64("capacity"),
        4: _i32("temperature"),
        5: _i32("power"),
        6: _i32("resistance"),
        7: _i32("cutoff_current"),
        8: _i32("cutoff_voltage"),
        9: _i32("cutoff_delta_voltage"),
        10: _i32("cycle_start_step"),
        11: _i32("cycle_count"),
        12: _i32("jump_step"),
        13: Field("operate", "i", comparison=False),
        14: Field("rate_type", "i", comparison=False),
        15: _i32("protect_voltage_upper"),
        16: _i32("protect_voltage_lower"),
        17: _i32("protect_current_upper"),
        18: _i32("protect_current_lower"),
        19: _i32("protect_temperature_upper"),
        20: _i32("protect_temperature_lower"),
        21: _i32("protect_delay_ms"),
        22: _i32("bfgs_time"),
        23: _i32("bfgs_voltage"),
        24: _i64("record_time_ms"),
        25: _i32("record_current"),
        26: _i32("record_voltage"),
        27: _i32("record_temperature"),
        28: _i32("plateau_voltage_1"),
        29: _i32("plateau_voltage_2"),
        30: _i32("plateau_voltage_3"),
    },
    2: {
        0: Field("aux_list_a", "i", comparison=False, count=8),
        1: Field("aux_list_b", "i", comparison=False, count=8),
        2: _i32("aux_limit_temperature_upper"),
        3: _i32("aux_limit_temperature_lower"),
        4: _i32("aux_limit_voltage_upper"),
        5: _i32("aux_limit_voltage_lower"),
        6: _i32("aux_limit_current_upper"),
        7: _i32("aux_limit_current_lower"),
        8: _i32("aux_protect_temperature_upper"),
        9: _i32("aux_protect_temperature_lower"),
        10: _i32("aux_protect_voltage_upper"),
        11: _i32("aux_protect_voltage_lower"),
        12: _i32("aux_protect_current_upper"),
        13: _i32("aux_protect_current_lower"),
        14: _i64("aux_record_time_ms"),
        15: _i32("aux_record_current"),
        16: _i32("aux_record_voltage"),
        17: _i32("aux_record_temperature"),
        # Bits 18..24 exist in newer clients but the BTS82 decoder does not read them.
        25: _i64("protect_capacity_upper"),
        26: _i64("energy"),
        27: _i32("aux_limit_voltage"),
        28: _i32("aux_protect_voltage_difference"),
    },
    3: {
        0: _i32("charge_time"),
        1: _i32("atl_protect_voltage_upper"),
        2: _i32("atl_protect_voltage_lower"),
        3: Field("map3_bit3", "i", comparison=False, count=5),
        4: Field("map3_bit4", "i", comparison=False, count=5),
        5: Field("map3_bit5", "i", comparison=False, count=5),
        6: _i32("protect_current_absolute"),
        7: _i32("protect_current_percent"),
        8: Field("map3_bit8", "i", count=5),
        9: Field("map3_bit9", "i", count=5),
        10: Field("map3_bit10", "i", count=5),
        11: _i32("current_error_count"),
        12: _i32("protect_voltage_absolute"),
        13: _i32("protect_voltage_percent"),
        14: Field("map3_bit14", "i", count=5),
        15: Field("map3_bit15", "i", count=5),
        16: Field("map3_bit16", "i", count=5),
        17: _i32("voltage_error_count"),
        18: _i32("map3_bit18"),
        19: Field("map3_bit19", "i", comparison=False, count=5),
        20: Field("map3_bit20", "i", comparison=False, count=5),
        21: Field("map3_bit21", "i", comparison=False, count=5),
        22: _i32("voltage_jump"),
        23: _i32("po_voltage_upper"),
        24: _i32("po_voltage_lower"),
        25: _i64("capacity_multiple"),
        26: _i32("voltage_inspect"),
        27: _i64("capacity_multiple_upper"),
        28: _i64("capacity_multiple_lower"),
        29: _i32("protect_current_rise"),
        30: _i32("protect_voltage_drop"),
    },
    5: {
        0: _i32("aux_charge_time"),
        1: _i32("aux_atl_protect_voltage_upper"),
        2: _i32("aux_atl_protect_voltage_lower"),
        3: _i32("aux_protect_cc_temperature_1"),
        4: _i32("aux_protect_cc_temperature_2"),
        **{bit: Field(f"map5_bit{bit}", "i", comparison=False, count=5) for bit in (5, 6, 7)},
        **{bit: Field(f"map5_bit{bit}", "i", comparison=False, count=7) for bit in (8, 9, 10)},
        11: _i32("aux_voltage_decline_error_count"),
        12: _i32("aux_protect_voltage_absolute"),
        13: _i32("aux_protect_voltage_percent"),
        **{bit: Field(f"map5_bit{bit}", "i", comparison=False, count=5) for bit in (14, 15, 16)},
        **{bit: Field(f"map5_bit{bit}", "i", comparison=False, count=7) for bit in (17, 18, 19)},
        20: _i32("aux_voltage_speed_error_count"),
        21: _i32("aux_voltage_jump"),
        22: _i32("aux_temperature_jump"),
        23: _i32("aux_protect_cv_temperature_1"),
        24: _i32("aux_protect_cv_temperature_2"),
    },
}
BY_NAME = {f.name: (number, bit, f) for number, bits in FIELDS.items() for bit, f in bits.items()}


@dataclass(frozen=True)
class Value:
    value: int | tuple[int, ...]
    comparison: Comparison | None = None


@dataclass(frozen=True)
class Step:
    index: int  # 1-based position in the program
    step_id: int
    mode: int
    values: dict[str, Value] = field(default_factory=dict)
    trailer: int = 1  # u32 after the fields; 1 in every manufacturer program

    def to_bytes(self) -> bytes:
        if self.mode not in ACCEPTED_MODES:
            raise ValueError(f"the controller does not accept step mode {self.mode}")
        unknown = set(self.values) - BY_NAME.keys()
        if unknown:
            raise ValueError(f"unknown step fields: {', '.join(sorted(unknown))}")
        maps = [0] * (max([BY_NAME[name][0] for name in self.values], default=1))
        body = b""
        for number, bits in FIELDS.items():
            for bit, f in bits.items():
                item = self.values.get(f.name)
                if item is None:
                    continue
                maps[number - 1] |= 1 << bit
                values = item.value if isinstance(item.value, tuple) else (item.value,)
                if len(values) != f.count:
                    raise ValueError(f"{f.name} needs {f.count} values")
                if f.comparison:
                    if item.comparison is None:
                        raise ValueError(f"{f.name} needs a comparison")
                    body += bytes([item.comparison])
                elif item.comparison is not None:
                    raise ValueError(f"{f.name} has no comparison byte")
                try:
                    body += struct.pack(f"<{f.count}{f.format}", *values)
                except struct.error as error:
                    raise ValueError(f"{f.name} = {item.value} out of range") from error
        words = [word | (CONTINUE if i < len(maps) - 1 else 0) for i, word in enumerate(maps)]
        if len(words) < 2:  # the manufacturer client always writes at least two map words
            words = [words[0] | CONTINUE, 0]
        size = HEADER.size + 4 * len(words) + len(body) + 4
        return (
            HEADER.pack(size, self.index, self.step_id, self.mode)
            + struct.pack(f"<{len(words)}I", *words)
            + body
            + struct.pack("<I", self.trailer)
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Step":
        if len(raw) < HEADER.size + 8:
            raise ProtocolError("truncated step record")
        size, index, step_id, mode = HEADER.unpack_from(raw)
        if size != len(raw):
            raise ProtocolError(f"step record size {size} != {len(raw)}")
        maps, offset = [], HEADER.size
        while True:
            if offset + 4 > size - 4:
                raise ProtocolError("step record maps overrun")
            word = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            maps.append(word & ~CONTINUE)
            if not word & CONTINUE:
                break
        values: dict[str, Value] = {}
        for number, word in enumerate(maps, 1):
            bits = FIELDS.get(number, {})
            unknown = word & ~sum(1 << bit for bit in bits)
            if unknown:
                raise ProtocolError(
                    f"step {step_id}: map {number} bits 0x{unknown:08x} are not decodable"
                )
            for bit, f in bits.items():
                if not word >> bit & 1:
                    continue
                if offset + f.size > size - 4:
                    raise ProtocolError(f"step {step_id}: field {f.name} overruns the record")
                comparison = None
                if f.comparison:
                    comparison = Comparison(raw[offset])
                    offset += 1
                items = struct.unpack_from(f"<{f.count}{f.format}", raw, offset)
                offset += f.size - int(f.comparison)
                values[f.name] = Value(items[0] if f.count == 1 else items, comparison)
        if offset != size - 4:
            raise ProtocolError(f"step {step_id}: {size - 4 - offset} unexplained field bytes")
        return cls(index, step_id, mode, values, struct.unpack_from("<I", raw, offset)[0])


def split_records(program: bytes) -> list[bytes]:
    records, offset = [], 0
    while offset < len(program):
        if len(program) - offset < 2:
            raise ProtocolError("truncated step program")
        size = struct.unpack_from("<H", program, offset)[0]
        if size < HEADER.size + 8 or offset + size > len(program):
            raise ProtocolError(f"invalid step record size {size} at {offset}")
        records.append(program[offset : offset + size])
        offset += size
    return records


def decode_program(program: bytes) -> list[Step]:
    return [Step.from_bytes(record) for record in split_records(program)]


def encode_program(steps: "list[Step]") -> bytes:
    if not 1 <= len(steps) <= 255:
        raise ValueError("a program has 1..255 steps")
    return b"".join(step.to_bytes() for step in steps)
