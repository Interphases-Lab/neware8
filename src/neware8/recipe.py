"""
Compile manufacturer step XML (BTSClient "Step File" v17/v18) into a native BTS82 program.

Only a protected subset is accepted: CC/CV/CCCV charge and discharge, rest, finite backward
cycles and End, each active step with an explicit time bound, recording interval and voltage
and current protections (from the step or from Whole_Prt). Anything else fails explicitly.

XML units: current mA, voltage 0.1 mV, time ms, capacity mA*s. Native units: current and
capacity times the channel's current scale, voltage 0.1 mV, time ms. Conversions must be exact.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from .models import ChannelProfile, StepType as Mode
from .steps import Comparison, Step, Value, encode_program

MAX_XML = 4 * 1024 * 1024
ABSOLUTE_CURRENT = 103  # Head_Info RateType: currents are absolute, not C-rates

CHARGE = (Mode.CC_CHARGE, Mode.CV_CHARGE, Mode.CCCV_CHARGE)
DISCHARGE = (Mode.CC_DISCHARGE, Mode.CV_DISCHARGE, Mode.CCCV_DISCHARGE)
CC = (Mode.CC_CHARGE, Mode.CC_DISCHARGE)
CV = (Mode.CV_CHARGE, Mode.CCCV_CHARGE, Mode.CV_DISCHARGE, Mode.CCCV_DISCHARGE)
SUPPORTED = (*CHARGE, *DISCHARGE, Mode.REST, Mode.CYCLE, Mode.END)

GLOBAL_SETTINGS = {
    "Protect/Volt/Upper",
    "Protect/Volt/Lower",
    "Protect/Curr/Upper",
    "Protect/Curr/Lower",
    "Protect/Cap/Upper",
    "Protect/Delay_Time",
    "Record/Time",
    "Record/Curr",
    "Record/Volt",
}
REQUIRED = {
    "Limit/Time",
    "Record/Time",
    "Protect/Volt/Upper",
    "Protect/Volt/Lower",
    "Protect/Curr/Upper",
    "Protect/Curr/Lower",
}
HEAD_FIELDS = {"Operate", "RateType", "Scale", "Start_Step", "Creator", "PN", "Remark", "MultCap"}


class RecipeError(ValueError):
    pass


def exact(value: Decimal, multiplier: Decimal | int = 1) -> int:
    scaled = Fraction(value) * Fraction(multiplier)
    if scaled.denominator != 1:
        raise RecipeError(f"{value} x {multiplier} is not an integer in native units")
    return scaled.numerator


def xml_text(raw: bytes) -> str:
    match = re.search(rb'encoding=["\']([^"\']+)', raw[:150])
    encoding = match[1].decode("ascii").lower() if match else "utf-8"
    if encoding not in ("gb2312", "utf-8"):
        raise RecipeError(f"unsupported XML encoding {encoding}")
    text = raw.decode(encoding)
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise RecipeError("DTD and entity declarations are not accepted")
    return text


def _leaf_values(section: ET.Element, cycle: bool) -> dict[str, Decimal]:
    """Flatten <Limit>/<Protect>/<Record> into {"Limit/Curr": value, "Protect/Volt/Upper": ...}."""
    values: dict[str, Decimal] = {}
    for part in section:
        if part.attrib or not len(part) or len({c.tag for c in part}) != len(part):
            raise RecipeError(f"unsupported or duplicate <{part.tag}> section")
        for group in part:
            allowed = ("Other",) if cycle else ("Main",)
            if group.tag not in allowed or group.attrib:
                raise RecipeError(f"unsupported <{part.tag}/{group.tag}> group")
            if len({c.tag for c in group}) != len(group):
                raise RecipeError(f"duplicate quantity in <{part.tag}>")
            for leaf in group:
                if len(leaf):  # Protect/<quantity>/<Upper|Lower>
                    if (
                        part.tag != "Protect"
                        or leaf.tag not in ("Volt", "Curr", "Temp", "Cap")
                        or leaf.attrib
                    ):
                        raise RecipeError(f"unsupported nested setting {leaf.tag}")
                    items = [(f"{part.tag}/{leaf.tag}/{bound.tag}", bound) for bound in leaf]
                elif part.tag == "Protect" and leaf.tag in ("Volt", "Curr", "Temp", "Cap"):
                    if leaf.attrib or (leaf.text or "").strip():
                        raise RecipeError(f"protection {leaf.tag} needs Upper/Lower")
                    continue  # empty optional protection group
                elif part.tag == "Protect" and leaf.tag != "Delay_Time":
                    raise RecipeError(f"protection {leaf.tag} needs a quantity and bound")
                else:
                    items = [(f"{part.tag}/{leaf.tag}", leaf)]
                for path, node in items:
                    if len(node) or set(node.attrib) != {"Value"} or path in values:
                        raise RecipeError(f"duplicate or malformed setting {path}")
                    try:
                        value = Decimal(node.get("Value", ""))
                    except InvalidOperation as error:
                        raise RecipeError(f"{path} is not a number") from error
                    if not value.is_finite():
                        raise RecipeError(f"{path} is not finite")
                    values[path] = value
    return values


@dataclass(frozen=True, slots=True)
class CompiledRecipe:
    xml: bytes
    native: bytes
    step_count: int
    maximum_duration_s: float
    sha256: str
    profile: ChannelProfile
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True, slots=True)
class Recipe:
    """Original Neware XML bytes, compiled against explicit channel ratings."""

    xml: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.xml, bytes):
            raise TypeError("Recipe requires XML bytes")
        if not 0 < len(self.xml) <= MAX_XML:
            raise ValueError("XML must contain 1 byte..4 MiB")

    @classmethod
    def from_file(cls, path: str | Path) -> "Recipe":
        source = Path(path)
        if source.suffix.lower() != ".xml":
            raise ValueError("only Neware .xml input is supported")
        with source.open("rb") as stream:
            return cls(stream.read(MAX_XML + 1))

    def compile(self, profile: ChannelProfile) -> CompiledRecipe:
        try:
            steps, duration_ms = _compile(xml_text(self.xml), profile)
        except ET.ParseError as error:
            raise RecipeError(f"invalid Neware XML: {error}") from error
        return CompiledRecipe(
            self.xml,
            encode_program(steps),
            len(steps),
            duration_ms / 1000,
            hashlib.sha256(self.xml).hexdigest(),
            profile,
            tuple(steps),
        )


def _head(config: ET.Element) -> dict[str, Decimal]:
    head = config.find("Head_Info")
    if head is None or head.attrib:
        raise RecipeError("missing <Head_Info>")
    if len({e.tag for e in head}) != len(head) or {e.tag for e in head} - HEAD_FIELDS:
        raise RecipeError("unsupported <Head_Info> fields")
    values = {}
    for element in head:
        allowed = {"Value", "Hide_Ctrl_Step"} if element.tag == "Start_Step" else {"Value"}
        if len(element) or set(element.attrib) - allowed or "Value" not in element.attrib:
            raise RecipeError(f"malformed <Head_Info/{element.tag}>")
        if element.tag in ("Creator", "PN", "Remark"):
            continue
        values[element.tag] = Decimal(element.get("Value", ""))
        if element.get("Hide_Ctrl_Step", "0") != "0":
            raise RecipeError("hidden control steps are not supported")
    for name in ("Operate", "RateType", "Scale", "Start_Step"):
        if name not in values:
            raise RecipeError(f"missing <Head_Info/{name}>")
    if values["Scale"] != 1 or values["Start_Step"] != 1:
        raise RecipeError("only Scale=1 and starting at step 1 are supported")
    if values["RateType"] != ABSOLUTE_CURRENT:
        raise RecipeError("only absolute-current programs (RateType=103) are supported")
    if "MultCap" in values and values["MultCap"] <= 0:
        raise RecipeError("MultCap must be positive")
    return values


def _compile(text: str, profile: ChannelProfile) -> tuple[list[Step], int]:
    root = ET.fromstring(text)
    if root.tag != "root" or len(root) != 1 or root.attrib:
        raise RecipeError("expected <root> with one <config>")
    config = root[0]
    if (
        config.tag != "config"
        or config.get("type") != "Step File"
        or config.get("version") not in ("17", "18")
    ):
        raise RecipeError("expected a manufacturer Step File, version 17 or 18")
    if set(config.attrib) - {"type", "version", "client_version", "date", "Guid"}:
        raise RecipeError("unsupported <config> attributes")
    tags = [element.tag for element in config]
    if len(set(tags)) != len(tags) or set(tags) - {"Head_Info", "Step_Info", "SMBUS", "Whole_Prt"}:
        raise RecipeError("unsupported or duplicate top-level sections")
    head = _head(config)
    smbus = config.find("SMBUS")
    if smbus is not None and (
        smbus.attrib
        or len(smbus) != 1
        or smbus[0].tag != "SMBUS_Info"
        or smbus[0].attrib != {"Num": "0", "AdjacentInterval": "0"}
        or len(smbus[0])
    ):
        raise RecipeError("SMBUS settings are not supported")

    scale = Decimal(profile.current_scale)
    voltage_rating = Decimal(str(profile.voltage_rating_v)) * 10000
    current_rating = Decimal(str(profile.current_rating_a)) * 1000
    defaults: dict[str, Decimal] = {}
    whole = config.find("Whole_Prt")
    if whole is not None:
        if (
            whole.attrib
            or len({e.tag for e in whole}) != len(whole)
            or {e.tag for e in whole} - {"Protect", "Record"}
        ):
            raise RecipeError("unsupported <Whole_Prt> sections")
        defaults = _leaf_values(whole, cycle=False)
        if set(defaults) - GLOBAL_SETTINGS:
            raise RecipeError(
                f"unsupported global settings: {sorted(set(defaults) - GLOBAL_SETTINGS)}"
            )

    info = config.find("Step_Info")
    if info is None or set(info.attrib) != {"Num"} or int(info.get("Num", "")) != len(info):
        raise RecipeError("<Step_Info> Num does not match its steps")
    steps: list[Step] = []
    durations: list[int] = []
    cycles: list[tuple[int, int]] = []
    for index, element in enumerate(info, 1):
        if (
            set(element.attrib) != {"Step_ID", "Step_Type"}
            or element.tag != f"Step{index}"
            or int(element.get("Step_ID", "")) != index
        ):
            raise RecipeError("steps must be numbered sequentially from 1")
        mode = int(element.get("Step_Type", ""))
        if mode == Mode.SLOPE:
            raise RecipeError("voltage scans are outside this cycler's scope")
        if mode not in SUPPORTED:
            raise RecipeError(f"step {index}: mode {mode} is not supported")
        tags = [child.tag for child in element]
        if len(set(tags)) != len(tags) or set(tags) - {"Limit", "Protect", "Record"}:
            raise RecipeError(f"step {index}: unsupported sections")
        values = _leaf_values(element, cycle=mode == Mode.CYCLE)
        _check_rates(values, mode, head.get("MultCap"))
        if mode not in (Mode.CYCLE, Mode.END):
            values = {**defaults, **values}

        if mode == Mode.CYCLE:
            if set(values) != {"Limit/Start_Step", "Limit/Cycle_Count"}:
                raise RecipeError(f"step {index}: Cycle needs exactly Start_Step and Cycle_Count")
            start, count = exact(values["Limit/Start_Step"]), exact(values["Limit/Cycle_Count"])
            if not 1 <= start < index or not 1 <= count <= 2**31 - 1:
                raise RecipeError(
                    f"step {index}: cycles must jump backwards a finite number of times"
                )
            if steps[start - 1].mode == Mode.CYCLE or any(a < start <= b for a, b in cycles):
                raise RecipeError(f"step {index}: cycle crosses another cycle")
            durations.append(sum(durations[start - 1 :]) * (count - 1))
            cycles.append((start, index))
        elif mode == Mode.END:
            if values or index != len(info):
                raise RecipeError("End must be the last step and have no settings")
            durations.append(0)
        else:
            _check_active(index, mode, values, voltage_rating, current_rating)
            durations.append(exact(values["Limit/Time"]))

        native = {
            "operate": Value(exact(head["Operate"])),
            "rate_type": Value(exact(head["RateType"])),
        }
        for path, value in values.items():
            name, multiplier, comparison = _mapping(path, mode, scale)
            native[name] = Value(exact(value, multiplier), comparison)
        steps.append(Step(index, index, mode, native))
    if not steps or steps[-1].mode != Mode.END:
        raise RecipeError("the program must end with an End step")
    return steps, sum(durations)


def _mapping(path: str, mode: int, scale: Decimal) -> tuple[str, Decimal | int, Comparison]:
    """Native field, multiplier and comparison for one XML setting (Common.StepItem.EncodeMap1)."""
    C = Comparison
    charge, discharge = mode in CHARGE, mode in DISCHARGE
    table: dict[str, tuple[str, Decimal | int, Comparison]] = {
        "Limit/Start_Step": ("cycle_start_step", 1, C.EQUAL),
        "Limit/Cycle_Count": ("cycle_count", 1, C.EQUAL),
        "Limit/Curr": ("current", scale, C.LESS_OR_EQUAL if charge else C.GREATER_OR_EQUAL),
        "Limit/Volt": ("voltage", 1, C.LESS_OR_EQUAL if discharge else C.GREATER_OR_EQUAL),
        "Limit/Time": ("time_ms", 1, C.GREATER_OR_EQUAL),
        "Limit/Stop_Volt": (
            "cutoff_voltage",
            1,
            C.LESS_OR_EQUAL if mode == Mode.CC_DISCHARGE else C.GREATER_OR_EQUAL,
        ),
        "Limit/Stop_Curr": ("cutoff_current", scale, C.LESS_OR_EQUAL),
        "Limit/Cap": ("capacity", scale, C.GREATER_OR_EQUAL),
        "Record/Time": ("record_time_ms", 1, C.GREATER_OR_EQUAL),
        "Record/Volt": ("record_voltage", 1, C.GREATER_OR_EQUAL),
        "Record/Curr": ("record_current", scale, C.GREATER_OR_EQUAL),
        "Protect/Volt/Upper": ("protect_voltage_upper", 1, C.GREATER_OR_EQUAL),
        "Protect/Volt/Lower": ("protect_voltage_lower", 1, C.LESS_OR_EQUAL),
        "Protect/Curr/Upper": ("protect_current_upper", scale, C.GREATER_OR_EQUAL),
        "Protect/Curr/Lower": ("protect_current_lower", scale, C.LESS),
        "Protect/Cap/Upper": ("protect_capacity_upper", scale, C.GREATER_OR_EQUAL),
        "Protect/Delay_Time": ("protect_delay_ms", 1, C.GREATER),
    }
    if path not in table:
        raise RecipeError(f"unsupported setting {path}")
    return table[path]


def _check_rates(values: dict[str, Decimal], mode: int, capacity: Decimal | None) -> None:
    """Drop redundant C-rate metadata after checking it agrees with the absolute current."""
    for rate_path, current_path in (
        ("Limit/Rate", "Limit/Curr"),
        ("Limit/Stop_Rate", "Limit/Stop_Curr"),
    ):
        if rate_path not in values:
            continue
        rate = values.pop(rate_path)
        if capacity is None:
            raise RecipeError("C-rate settings need MultCap")
        if mode not in (*CHARGE, *DISCHARGE) or (rate_path == "Limit/Stop_Rate" and mode not in CV):
            raise RecipeError(f"{rate_path} does not apply to mode {mode}")
        if (
            rate <= 0
            or current_path not in values
            or rate * capacity != values[current_path] * 3600
        ):
            raise RecipeError("C-rate, MultCap and absolute current disagree")


def _check_active(
    index: int,
    mode: int,
    values: dict[str, Decimal],
    voltage_rating: Decimal,
    current_rating: Decimal,
) -> None:
    allowed = {"Limit/Time"}
    if mode in CC:
        allowed |= {"Limit/Curr", "Limit/Stop_Volt", "Limit/Cap"}
    elif mode in CV:
        allowed |= {"Limit/Curr", "Limit/Volt", "Limit/Stop_Curr", "Limit/Cap"}
    extra = {key for key in values if key.startswith("Limit/")} - allowed
    if extra:
        raise RecipeError(f"step {index}: {sorted(extra)} do not apply to mode {mode}")
    if not REQUIRED <= values.keys():
        raise RecipeError(
            f"step {index}: needs explicit time, recording interval and voltage "
            "and current protections"
        )
    if not 0 < values["Record/Time"] <= values["Limit/Time"]:
        raise RecipeError(f"step {index}: recording interval must be within the step time")
    for path in ("Limit/Cap", "Protect/Cap/Upper"):
        if path in values and values[path] <= 0:
            raise RecipeError(f"step {index}: {path} must be positive")
    if (
        "Protect/Delay_Time" in values
        and not 0 <= values["Protect/Delay_Time"] < values["Limit/Time"]
    ):
        raise RecipeError(f"step {index}: protection delay must be shorter than the step")
    low_v, high_v = values["Protect/Volt/Lower"], values["Protect/Volt/Upper"]
    low_i, high_i = values["Protect/Curr/Lower"], values["Protect/Curr/Upper"]
    if (
        not -voltage_rating <= low_v < high_v <= voltage_rating
        or not -current_rating <= low_i < 0 < high_i <= current_rating
    ):
        raise RecipeError(f"step {index}: protections exceed the channel ratings")
    for path in ("Limit/Volt", "Limit/Stop_Volt"):
        if path in values and not low_v <= values[path] <= high_v:
            raise RecipeError(f"step {index}: {path} is outside the voltage protection")
    if mode != Mode.REST:
        limit = min(-low_i, high_i) if mode in DISCHARGE else high_i
        if "Limit/Curr" not in values or not 0 < values["Limit/Curr"] < limit:
            raise RecipeError(f"step {index}: current missing or outside the current protection")
    if mode in CV:
        if not {"Limit/Volt", "Limit/Stop_Curr"} <= values.keys():
            raise RecipeError(f"step {index}: CV needs a voltage and a cutoff current")
        if not 0 < values["Limit/Stop_Curr"] < values["Limit/Curr"]:
            raise RecipeError(f"step {index}: cutoff current must be below the current limit")
