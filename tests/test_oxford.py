"""Tests for the Oxford Instruments drivers.

The Mercury family speaks a line protocol whose replies echo the command, so
the fake responses here are shaped like real Mercury replies.
"""

import pytest

from labdrivers.core import InstrumentError, RangeError, RecordingTransport
from labdrivers.oxford.ips120 import Ips120
from labdrivers.oxford.itc503 import Itc503
from labdrivers.oxford.mercury import MercuryInstrument
from labdrivers.oxford.mercuryips import DEFAULT_FIELD_LIMITS, MercuryIps
from labdrivers.oxford.mercuryips_teslatron import MercuryIpsTeslatron
from labdrivers.oxford.mercuryitc import MercuryItc
from labdrivers.oxford.mercuryitc_heliox import MercuryItcHeliox
from labdrivers.oxford.triton200 import DEFAULT_HEATER_RANGES, Triton200


def valid(command):
    """A Mercury reply confirming a SET was accepted."""
    return f"STAT:{command[4:]}:VALID"


def build(cls, responses=None, **kwargs):
    transport = RecordingTransport(responses or {})
    instrument = cls(transport=transport, **kwargs)
    transport.clear()
    return instrument, transport


# Protocol


def test_set_is_confirmed_valid():
    """A Mercury answers VALID or INVALID, and only VALID means it took effect."""
    command = "SET:DEV:GRPZ:PSU:SIG:FSET:1.0"
    instrument, _ = build(
        MercuryInstrument, {command: "STAT:SET:DEV:GRPZ:PSU:SIG:FSET:1.0:VALID"}
    )
    instrument.set_noun("DEV:GRPZ:PSU:SIG:FSET", 1.0)


def test_rejected_set_raises():
    """A reply alone does not mean acceptance, and INVALID must raise.

    Checking only that something came back makes a rejected setpoint
    indistinguishable from an accepted one.
    """
    command = "SET:DEV:GRPZ:PSU:SIG:FSET:99"
    instrument, _ = build(
        MercuryInstrument, {command: "STAT:SET:DEV:GRPZ:PSU:SIG:FSET:99:INVALID"}
    )
    with pytest.raises(InstrumentError, match="rejected"):
        instrument.set_noun("DEV:GRPZ:PSU:SIG:FSET", 99)


@pytest.mark.parametrize(
    "reply,unit,expected",
    [
        ("STAT:DEV:GRPZ:PSU:SIG:FLD:0.5000T", "T", 0.5),
        ("STAT:DEV:GRPZ:PSU:SIG:RFST:0.2000T/m", "T/m", 0.2),
        ("STAT:DEV:GRPZ:PSU:SIG:CURR:-12.5A", "A", -12.5),
        ("STAT:DEV:MB1.T1:TEMP:SIG:TEMP:4.2000K", "K", 4.2),
    ],
)
def test_values_are_parsed_with_their_units(reply, unit, expected):
    noun = reply.split(":", 1)[1].rsplit(":", 1)[0]
    assert MercuryInstrument.parse_value(reply, noun, unit) == pytest.approx(expected)


def test_ramp_rate_unit_is_not_confused_with_field_unit():
    """Stripping 'T' from a 'T/m' reply would leave '/m' and fail to parse."""
    reply = "STAT:DEV:GRPZ:PSU:SIG:RFST:0.2000T/m"
    noun = "DEV:GRPZ:PSU:SIG:RFST"
    assert MercuryInstrument.parse_value(reply, noun, "T/m") == pytest.approx(0.2)


def test_unparseable_reply_is_reported():
    with pytest.raises(InstrumentError, match="Could not read a number"):
        MercuryInstrument.parse_value("STAT:DEV:X:SIG:FLD:NOTANUMBER", "DEV:X:SIG:FLD")


# Mercury iPS


def test_field_setpoint_is_checked_against_the_axis_limit():
    supply, _ = build(MercuryIps)
    with pytest.raises(RangeError, match="GRPZ field setpoint"):
        supply.z.field_setpoint = 10.0
    with pytest.raises(RangeError, match="GRPX field setpoint"):
        supply.x.field_setpoint = 2.0


def test_field_limits_are_configurable_per_axis():
    """Field limits belong to the magnet, not the supply."""
    supply, _ = build(MercuryIps, field_limits={"GRPZ": 14.0})
    assert supply.z.field_limit == 14.0
    assert supply.x.field_limit == DEFAULT_FIELD_LIMITS["GRPX"]


def test_current_setpoint_includes_the_separating_colon():
    """The value is a separate field, not a suffix on the noun.

    Without the colon the Mercury sees SET:DEV:GRPZ:PSU:SIG:CSET1.5, which
    is not a command it recognizes.
    """
    command = "SET:DEV:GRPZ:PSU:SIG:CSET:1.5"
    supply, transport = build(MercuryIps, {command: valid(command)})
    supply.z.current_setpoint = 1.5
    assert transport.queries == [command]


def test_current_ramp_rate_includes_the_separating_colon():
    command = "SET:DEV:GRPZ:PSU:SIG:RCST:0.5"
    supply, transport = build(MercuryIps, {command: valid(command)})
    supply.z.current_ramp_rate = 0.5
    assert transport.queries == [command]


def test_field_setpoint_command():
    command = "SET:DEV:GRPZ:PSU:SIG:FSET:1.0"
    supply, transport = build(MercuryIps, {command: valid(command)})
    supply.z.field_setpoint = 1.0
    assert transport.queries == [command]


def test_actions_use_the_action_path_not_the_signal_path():
    """Actions live under :PSU:ACTN, and :PSU:SIG:ACTN is a different path."""
    command = "SET:DEV:GRPZ:PSU:ACTN:RTOS"
    supply, transport = build(MercuryIps, {command: valid(command)})
    supply.z.ramp_to_setpoint()
    assert transport.queries == [command]


@pytest.mark.parametrize(
    "action,ramping,holding",
    [
        ("RTOS", True, False),
        ("RTOZ", True, False),
        ("HOLD", False, True),
        ("CLMP", False, False),
    ],
)
def test_ramping_and_holding_are_implemented(action, ramping, holding):
    """Both must report a real state, since measurement loops poll them."""
    supply, _ = build(
        MercuryIps,
        {"READ:DEV:GRPZ:PSU:ACTN": f"STAT:DEV:GRPZ:PSU:ACTN:{action}"},
    )
    assert supply.z.ramping() is ramping
    assert supply.z.holding() is holding


def test_magnet_temperature_reads():
    """The magnet temperature sensor sits on the iPS, not the iTC."""
    supply, _ = build(
        MercuryIps,
        {"READ:DEV:MB1.T1:TEMP:SIG:TEMP": "STAT:DEV:MB1.T1:TEMP:SIG:TEMP:4.2000K"},
    )
    assert supply.magnet_temperature == pytest.approx(4.2)


def test_unknown_axis_is_reported():
    supply, _ = build(MercuryIps, axes=("GRPZ",))
    with pytest.raises(RangeError, match="no GRPX axis"):
        supply.x


def test_circle_sweep_returns_points_on_a_circle():
    supply, _ = build(MercuryIps)
    points = supply.circle_sweep(0.5, 4, plane="xy")
    assert len(points) == 4
    assert points[0]["GRPX"] == pytest.approx(0.5)
    assert points[1]["GRPY"] == pytest.approx(0.5)
    for point in points:
        magnitude = (point["GRPX"] ** 2 + point["GRPY"] ** 2) ** 0.5
        assert magnitude == pytest.approx(0.5)


def test_circle_sweep_respects_the_axis_limit():
    supply, _ = build(MercuryIps)
    with pytest.raises(RangeError, match="GRPX field"):
        supply.circle_sweep(5.0, 8, plane="xy")


# Teslatron fork


def test_teslatron_has_one_axis_and_a_higher_limit():
    supply, _ = build(MercuryIpsTeslatron)
    assert sorted(supply.magnets) == ["GRPZ"]
    assert supply.magnet.field_limit == 12.0


def test_teslatron_field_limit_is_configurable():
    supply, _ = build(MercuryIpsTeslatron, field_limit=14.0)
    assert supply.magnet.field_limit == 14.0


# Mercury iTC


def test_sensor_names_map_to_board_identifiers():
    itc, _ = build(
        MercuryItc,
        {"READ:DEV:DB8.T1:TEMP:SIG:TEMP": "STAT:DEV:DB8.T1:TEMP:SIG:TEMP:1.5K"},
    )
    assert itc.temperature("probe") == pytest.approx(1.5)


def test_sensor_map_is_configurable_for_other_cryostats():
    """Which board a sensor is on is a property of the cryostat's wiring."""
    itc, _ = build(
        MercuryItc,
        {"READ:DEV:DB3.T1:TEMP:SIG:TEMP": "STAT:DEV:DB3.T1:TEMP:SIG:TEMP:77.0K"},
        sensors={"sample": "DB3.T1"},
    )
    assert itc.temperature("sample") == pytest.approx(77.0)


def test_a_raw_board_identifier_is_accepted():
    itc, _ = build(
        MercuryItc,
        {"READ:DEV:DB9.T1:TEMP:SIG:TEMP": "STAT:DEV:DB9.T1:TEMP:SIG:TEMP:2.0K"},
    )
    assert itc.temperature("DB9.T1") == pytest.approx(2.0)


def test_unknown_sensor_lists_what_is_available():
    itc, _ = build(MercuryItc)
    with pytest.raises(RangeError, match="not a known sensor"):
        itc.temperature("banana")


def test_setpoint_command():
    command = "SET:DEV:MB1.T1:TEMP:LOOP:TSET:4.2"
    itc, transport = build(MercuryItc, {command: valid(command)})
    itc.setpoint("vti", 4.2)
    assert transport.queries == [command]


# Heliox fork


def test_heliox_has_its_own_sensor_map():
    heliox, _ = build(
        MercuryItcHeliox,
        {"READ:DEV:DB7.T1:TEMP:SIG:TEMP": "STAT:DEV:DB7.T1:TEMP:SIG:TEMP:0.3K"},
    )
    assert heliox.he3_pot_temperature == pytest.approx(0.3)


def test_sorb_temperature_is_capped():
    """Driving the sorb far above its working range risks the heater."""
    heliox, _ = build(MercuryItcHeliox)
    with pytest.raises(RangeError, match="sorb temperature setpoint"):
        heliox.set_sorb_temperature(200)


# Triton 200


def test_triton_uses_its_own_port():
    fridge, _ = build(Triton200)
    assert fridge.transport is not None


def test_heater_range_ladder_steps_with_the_setpoint():
    fridge, _ = build(Triton200)
    assert fridge.heater_range_for(0.010) == DEFAULT_HEATER_RANGES[0]
    assert fridge.heater_range_for(0.040) == DEFAULT_HEATER_RANGES[1]
    assert fridge.heater_range_for(0.100) == DEFAULT_HEATER_RANGES[2]
    assert fridge.heater_range_for(0.200) == DEFAULT_HEATER_RANGES[3]
    assert fridge.heater_range_for(0.500) == DEFAULT_HEATER_RANGES[4]
    assert fridge.heater_range_for(4.0) == DEFAULT_HEATER_RANGES[5]


def test_heater_ladder_is_configurable():
    """The right ladder depends on the fridge and its heater resistance."""
    fridge, _ = build(
        Triton200,
        heater_ranges=("1", "10", "100"),
        heater_thresholds=(0.1, 1.0),
    )
    assert fridge.heater_range_for(0.05) == "1"
    assert fridge.heater_range_for(0.5) == "10"
    assert fridge.heater_range_for(5.0) == "100"


def test_mismatched_ladder_is_rejected():
    with pytest.raises(RangeError, match="one fewer heater threshold"):
        build(Triton200, heater_ranges=("1", "10"), heater_thresholds=(0.1, 1.0))


def test_unsorted_thresholds_are_rejected():
    with pytest.raises(RangeError, match="increasing order"):
        build(Triton200, heater_ranges=("1", "10", "100"), heater_thresholds=(1.0, 0.1))


def test_temperature_channel_is_bounded():
    fridge, _ = build(Triton200)
    with pytest.raises(RangeError, match="temperature channel"):
        fridge.temperature(99)


def test_valve_state_command():
    command = "SET:DEV:V4:VALV:SIG:STATE:OPEN"
    fridge, transport = build(Triton200, {command: valid(command)})
    fridge.set_valve(4, "open")
    assert transport.queries == [command]


def test_turbo_command():
    command = "SET:DEV:TURB1:PUMP:SIG:STATE:ON"
    fridge, transport = build(Triton200, {command: valid(command)})
    fridge.turbo = True
    assert transport.queries == [command]


# Legacy IPS 120


def test_legacy_command_must_be_echoed():
    """A reply that does not echo the command letter means it was not
    understood."""
    supply, _ = build(Ips120, {"R7": "R+00.5000"})
    assert supply.field == pytest.approx(0.5)

    supply, _ = build(Ips120, {"R7": "?R7"})
    with pytest.raises(InstrumentError, match="illegal command"):
        supply.field


def test_legacy_wrong_echo_is_reported():
    supply, _ = build(Ips120, {"R7": "C"})
    with pytest.raises(InstrumentError, match="echo"):
        supply.field


def test_ips120_field_setpoint_is_checked():
    supply, _ = build(Ips120, field_limit=8.0)
    with pytest.raises(RangeError, match="field setpoint"):
        supply.field_setpoint = 10.0


def test_ips120_field_setpoint_command():
    supply, transport = build(Ips120, {"J1.0000": "J"}, field_limit=8.0)
    supply.field_setpoint = 1.0
    assert transport.queries == ["J1.0000"]


# Legacy ITC 503


def test_itc503_reads_each_sensor():
    controller, _ = build(
        Itc503, {"R1": "R+004.200", "R2": "R+077.000", "R3": "R+300.000"}
    )
    assert controller.temperatures() == {
        1: pytest.approx(4.2),
        2: pytest.approx(77.0),
        3: pytest.approx(300.0),
    }


def test_itc503_setpoint_command():
    controller, transport = build(Itc503, {"T4.2000": "T"})
    controller.setpoint = 4.2
    assert transport.queries == ["T4.2000"]


def test_itc503_pid_limits():
    controller, _ = build(Itc503)
    with pytest.raises(RangeError, match="integral action time"):
        controller.pid(integral=500)


def test_itc503_heater_percent_is_bounded():
    controller, _ = build(Itc503)
    with pytest.raises(RangeError, match="heater output"):
        controller.heater_percent = 150
