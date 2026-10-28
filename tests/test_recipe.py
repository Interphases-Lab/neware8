"""XML compilation: byte-exact against captured programs, and the protected-subset rules."""

import json

import pytest

from neware8 import ChannelAddress, ChannelProfile, Recipe
from neware8.steps import decode_program, split_records
from helpers import FIXTURES, PROFILE, XML

# Step.xml from the NDAX of a manufacturer-run resistor CC-CV test (aurora-unicycler export),
# and the program the manufacturer software downloaded for it (aurora_cc_cv.pcapng).
AURORA_XML = (FIXTURES / "aurora_cc_cv_step.xml").read_bytes()
AURORA_PROGRAM = json.loads((FIXTURES / "native_steps.json").read_text())["native"]["raw_hex"]
SITE = ChannelProfile(ChannelAddress(1, 1), 0, 5.0, 0.01, 10000)


def test_manufacturer_xml_compiles_to_the_captured_program():
    compiled = Recipe(AURORA_XML).compile(SITE)
    assert compiled.native.hex() == AURORA_PROGRAM
    assert compiled.step_count == 3


@pytest.mark.parametrize(
    "change",
    [
        (b"<Head_Info>", b"<Unknown/><Head_Info>"),
        (b'Value="8"', b'Value="8000"'),
        (b'<Time Value="2000" />', b'<Unknown Value="2000" />'),
    ],
)
def test_manufacturer_xml_variants_fail(change):
    with pytest.raises(ValueError):
        Recipe(AURORA_XML.replace(*change)).compile(SITE)


def test_cycle_step_bytes_and_duration():
    compiled = Recipe(XML).compile(PROFILE)
    assert compiled.maximum_duration_s == 30  # 10 s step, repeated 3 times
    cycle = split_records(compiled.native)[1]
    assert cycle.hex() == "240002000205006c00800000000001010000000103000000420000006700000001000000"
    assert decode_program(compiled.native)[1].values["cycle_count"].value == 3


@pytest.mark.parametrize(
    "xml",
    [
        XML.replace(b'Value="8"', b'Value="80"'),  # protection above rating
        XML.replace(b"<Whole_Prt>", b"<Whole_Prt><Unknown/>"),
        XML.replace(b'Cycle_Count Value="3"', b'Cycle_Count Value="0"'),
        b"<!DOCTYPE root><root/>",
        b'{"steps": []}',
    ],
)
def test_unsupported_xml_fails(xml):
    with pytest.raises(ValueError):
        Recipe(xml).compile(PROFILE)


@pytest.mark.parametrize("rate_type", [4, 5, 104, 105, 204, 999])
def test_only_absolute_current_programs(rate_type):
    xml = XML.replace(b'RateType Value="103"', f'RateType Value="{rate_type}"'.encode())
    with pytest.raises(ValueError, match="RateType=103"):
        Recipe(xml).compile(PROFILE)


@pytest.mark.parametrize("voltage", [-10000, 40000])
def test_cutoff_may_equal_the_protection(voltage):
    xml = XML.replace(b'Stop_Volt Value="0"', f'Stop_Volt Value="{voltage}"'.encode())
    assert Recipe(xml).compile(PROFILE).step_count == 3


@pytest.mark.parametrize("voltage", [-10001, 40001])
def test_cutoff_outside_the_protection_fails(voltage):
    xml = XML.replace(b'Stop_Volt Value="0"', f'Stop_Volt Value="{voltage}"'.encode())
    with pytest.raises(ValueError, match="outside the voltage protection"):
        Recipe(xml).compile(PROFILE)


def test_inexact_unit_conversion_fails():
    xml = XML.replace(b'Curr Value="1"', b'Curr Value="1.00001"')
    with pytest.raises(ValueError, match="not an integer"):
        Recipe(xml).compile(PROFILE)


def test_voltage_scans_are_out_of_scope():
    xml = XML.replace(b'Step_Type="2"', b'Step_Type="25"')
    with pytest.raises(ValueError, match="voltage scans"):
        Recipe(xml).compile(PROFILE)


@pytest.mark.parametrize("unit,channel", [(0, 1), (1, 0), (33, 1), (1, 9), (True, 1)])
def test_addresses_reject_invalid_values(unit, channel):
    with pytest.raises(ValueError):
        ChannelAddress(unit, channel)
