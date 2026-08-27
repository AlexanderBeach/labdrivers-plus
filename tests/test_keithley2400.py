"""Tests for the Keithley 2400 series driver.

Every command the driver sends is asserted against the string printed in the
2400 Series SourceMeter User's Manual, Section 18. This is the check that a
command transcribed from a 500-page manual was transcribed correctly.
"""

import pytest

from labdrivers.core import InstrumentError, RangeError, RecordingTransport
from labdrivers.keithley.keithley2400 import (
    FRONT_PANEL_KEYS,
    MAXIMUM_BUFFER_POINTS,
    MODEL_LIMITS,
    Keithley2400,
)

IDENTITY_2400 = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30 Mar 17 2006"
IDENTITY_2410 = "KEITHLEY INSTRUMENTS INC.,MODEL 2410,1234567,C30 Mar 17 2006"
IDENTITY_2440 = "KEITHLEY INSTRUMENTS INC.,MODEL 2440,1234567,C30 Mar 17 2006"


def build(responses=None, identity=IDENTITY_2400, **kwargs):
    """Make a driver wired to a recording transport, with canned replies."""
    replies = {"*IDN?": identity}
    replies.update(responses or {})
    transport = RecordingTransport(replies)
    instrument = Keithley2400(transport=transport, **kwargs)
    transport.clear()  # drop the *IDN? from model detection
    return instrument, transport


@pytest.fixture
def source():
    instrument, transport = build(
        {
            ":SOUR:FUNC:MODE?": "VOLT",
            ":OUTP:STAT?": "0",
            ":SOUR:VOLT:LEV?": "0.0",
        }
    )
    return instrument, transport


# Model detection and limits


def test_model_detected_from_identification():
    instrument, _ = build(identity=IDENTITY_2410)
    assert instrument.model == "2410"
    assert instrument.maximum_voltage == 1100.0


def test_unknown_identification_falls_back_to_2400():
    instrument, _ = build(identity="SOME OTHER INSTRUMENT")
    assert instrument.model == "2400"


def test_model_can_be_given_explicitly():
    """A 2400-LV identifies as a plain 2400 but only sources 21 V."""
    instrument, _ = build(model="2400-LV")
    assert instrument.maximum_voltage == 21.0


def test_unknown_model_is_rejected():
    with pytest.raises(RangeError, match="not a Keithley 2400 series model"):
        build(model="2600")


@pytest.mark.parametrize(
    "model,current,voltage", [(m, *v) for m, v in MODEL_LIMITS.items()]
)
def test_every_model_has_manual_limits(model, current, voltage):
    instrument, _ = build(model=model)
    assert (instrument.maximum_current, instrument.maximum_voltage) == (
        current,
        voltage,
    )


def test_limits_are_enforced_per_model():
    """500 V is fine on a 2410 and impossible on a 2400."""
    on_2410, transport = build({":SOUR:FUNC:MODE?": "VOLT"}, identity=IDENTITY_2410)
    on_2410.source_value = 500
    assert transport.last_command == ":SOUR:VOLT:LEV 500"

    on_2400, _ = build({":SOUR:FUNC:MODE?": "VOLT"}, identity=IDENTITY_2400)
    with pytest.raises(RangeError, match="must be between"):
        on_2400.source_value = 500


def test_current_limit_is_enforced_per_model():
    on_2440, transport = build({":SOUR:FUNC:MODE?": "CURR"}, identity=IDENTITY_2440)
    on_2440.source_value = 5.0
    assert transport.last_command == ":SOUR:CURR:LEV 5.0"

    on_2400, _ = build({":SOUR:FUNC:MODE?": "CURR"}, identity=IDENTITY_2400)
    with pytest.raises(RangeError, match="must be between"):
        on_2400.source_value = 5.0


# Source


def test_source_function_commands(source):
    instrument, transport = source
    instrument.source_function = "current"
    assert transport.last_command == ":SOUR:FUNC:MODE CURR"
    instrument.source_function = "voltage"
    assert transport.last_command == ":SOUR:FUNC:MODE VOLT"


def test_source_function_reads_back():
    instrument, _ = build({":SOUR:FUNC:MODE?": "CURR"})
    assert instrument.source_function == "current"


def test_bad_source_function_lists_the_options(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="'voltage'"):
        instrument.source_function = "sideways"


def test_memory_mode_has_no_level():
    instrument, _ = build({":SOUR:FUNC:MODE?": "MEM"})
    with pytest.raises(RangeError, match="memory mode"):
        instrument.source_value = 1.0


def test_source_mode_follows_the_active_function():
    instrument, transport = build({":SOUR:FUNC:MODE?": "CURR"})
    instrument.source_mode = "sweep"
    assert transport.last_command == ":SOUR:CURR:MODE SWE"


def test_source_delay(source):
    instrument, transport = source
    instrument.source_delay = 0.1
    assert transport.last_command == ":SOUR:DEL 0.1"
    with pytest.raises(RangeError, match="must be between"):
        instrument.source_delay = -1


def test_source_auto_range(source):
    instrument, transport = source
    instrument.source_auto_range = True
    assert transport.last_command == ":SOUR:VOLT:RANG:AUTO 1"
    instrument.source_auto_range = "off"
    assert transport.last_command == ":SOUR:VOLT:RANG:AUTO 0"


# Compliance


def test_compliance_commands(source):
    instrument, transport = source
    instrument.current_compliance = 1e-3
    assert transport.last_command == ":SENS:CURR:PROT:LEV 0.001"
    instrument.voltage_compliance = 21
    assert transport.last_command == ":SENS:VOLT:PROT:LEV 21"


def test_compliance_beyond_the_model_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="current compliance"):
        instrument.current_compliance = 5.0


def test_in_compliance_checks_both_limits():
    instrument, _ = build({":SENS:CURR:PROT:TRIP?": "1", ":SENS:VOLT:PROT:TRIP?": "0"})
    assert instrument.in_current_compliance is True
    assert instrument.in_voltage_compliance is False
    assert instrument.in_compliance is True


# Measurement


def test_measure_functions_are_quoted_and_comma_separated(source):
    instrument, transport = source
    instrument.measure_functions = ["voltage", "current"]
    assert transport.commands == [
        ":SENS:FUNC:CONC ON",
        ":SENS:FUNC:OFF:ALL",
        ":SENS:FUNC:ON 'VOLT:DC','CURR:DC'",
    ]


def test_a_single_measure_function_may_be_a_bare_string(source):
    instrument, transport = source
    instrument.measure_functions = "resistance"
    assert transport.last_command == ":SENS:FUNC:ON 'RES'"


def test_measure_functions_read_back():
    instrument, _ = build({":SENS:FUNC:ON?": '"VOLT:DC","CURR:DC"'})
    assert sorted(instrument.measure_functions) == ["current", "voltage"]


def test_integration_time(source):
    instrument, transport = source
    instrument.set_integration_time("current", 0.1)
    assert transport.last_command == ":SENS:CURR:DC:NPLC 0.1"


@pytest.mark.parametrize("cycles", [0.001, 11])
def test_integration_time_outside_the_manual_range_is_rejected(source, cycles):
    instrument, _ = source
    with pytest.raises(RangeError, match="integration time"):
        instrument.set_integration_time("voltage", cycles)


def test_four_wire_sense(source):
    instrument, transport = source
    instrument.four_wire_sense = True
    assert transport.last_command == ":SYST:RSEN 1"


def test_auto_zero_accepts_once(source):
    instrument, transport = source
    instrument.auto_zero = "once"
    assert transport.last_command == ":SYST:AZER:STAT ONCE"
    instrument.auto_zero = False
    assert transport.last_command == ":SYST:AZER:STAT 0"


# Filter


def test_enable_filter_sends_type_count_and_state(source):
    instrument, transport = source
    instrument.enable_filter(count=20, filter_type="moving")
    assert transport.commands == [
        ":SENS:AVER:TCON MOV",
        ":SENS:AVER:COUN 20",
        ":SENS:AVER:STAT 1",
    ]


@pytest.mark.parametrize("count", [0, 101])
def test_filter_count_outside_1_to_100_is_rejected(source, count):
    instrument, _ = source
    with pytest.raises(RangeError, match="filter count"):
        instrument.filter_count = count


def test_bad_filter_type_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="filter type"):
        instrument.filter_type = "exponential"


# Output


def test_output_accepts_the_usual_spellings(source):
    instrument, transport = source
    for value, expected in [
        (True, 1),
        (1, 1),
        ("on", 1),
        (False, 0),
        (0, 0),
        ("off", 0),
    ]:
        instrument.output = value
        assert transport.last_command == f":OUTP:STAT {expected}"


def test_output_getter_reads_a_string_reply_correctly():
    """The reply arrives as a string and must be converted, not compared."""
    instrument, _ = build({":OUTP:STAT?": "0"})
    assert instrument.output is False
    instrument, _ = build({":OUTP:STAT?": "1"})
    assert instrument.output is True


def test_bad_output_value_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="can either be"):
        instrument.output = "maybe"


def test_output_off_mode(source):
    instrument, transport = source
    instrument.output_off_mode = "high impedance"
    assert transport.last_command == ":OUTP:SMOD HIMP"


# Reading


def test_read_sets_elements_then_triggers():
    instrument, transport = build({":READ?": "1.0,2.0"})
    assert instrument.read("voltage", "current") == [1.0, 2.0]
    assert transport.commands == [":FORM:ELEM VOLT,CURR", ":READ?"]


def test_read_without_elements_does_not_reconfigure():
    instrument, transport = build({":READ?": "1.0"})
    instrument.read()
    assert transport.commands == [":READ?"]


def test_measure_uses_the_function_specific_query():
    instrument, transport = build({":MEAS:RES?": "1000.0"})
    assert instrument.measure("resistance") == [1000.0]
    assert transport.last_command == ":MEAS:RES?"


def test_bad_data_element_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="data element"):
        instrument.data_elements = ["voltage", "temperature"]


# Buffer


def test_start_buffer_clears_sizes_and_arms(source):
    instrument, transport = source
    instrument.start_buffer(size=100)
    assert transport.commands == [
        ":TRAC:CLE",
        ":TRAC:POIN 100",
        ":TRAC:FEED:CONT NEXT",
    ]


def test_buffer_size_is_capped_at_the_manual_maximum(source):
    instrument, transport = source
    instrument.buffer_size = MAXIMUM_BUFFER_POINTS
    assert transport.last_command == f":TRAC:POIN {MAXIMUM_BUFFER_POINTS}"
    with pytest.raises(RangeError, match="buffer size"):
        instrument.buffer_size = MAXIMUM_BUFFER_POINTS + 1


def test_read_buffer_returns_floats():
    instrument, _ = build({":TRAC:DATA?": "1.0,2.0,3.0"})
    assert instrument.read_buffer() == [1.0, 2.0, 3.0]


# Trigger and arm


def test_trigger_settings(source):
    instrument, transport = source
    instrument.trigger_count = 10
    assert transport.last_command == ":TRIG:COUN 10"
    instrument.trigger_delay = 0.25
    assert transport.last_command == ":TRIG:DEL 0.25"
    instrument.trigger_source = "trigger link"
    assert transport.last_command == ":TRIG:SOUR TLIN"


def test_arm_count_accepts_infinite(source):
    instrument, transport = source
    instrument.arm_count = "infinite"
    assert transport.last_command == ":ARM:COUN INF"


def test_arm_timer_range_matches_the_manual(source):
    instrument, transport = source
    instrument.arm_timer = 0.001
    assert transport.last_command == ":ARM:TIM 0.001"
    with pytest.raises(RangeError, match="arm timer"):
        instrument.arm_timer = 0.0001


# Sweeps


def test_configure_sweep_by_points(source):
    instrument, transport = source
    assert instrument.configure_sweep(0, 1, points=11) == 11
    assert transport.writes == [
        ":SOUR:VOLT:MODE SWE",
        ":SOUR:VOLT:STAR 0",
        ":SOUR:VOLT:STOP 1",
        ":SOUR:SWE:SPAC LIN",
        ":SOUR:SWE:DIR UP",
        ":SOUR:SWE:RANG BEST",
        ":SOUR:SWE:POIN 11",
        ":TRIG:COUN 11",
    ]


def test_configure_sweep_by_step_computes_the_point_count(source):
    instrument, transport = source
    assert instrument.configure_sweep(0, 1, step=0.1) == 11
    assert ":SOUR:VOLT:STEP 0.1" in transport.writes
    assert transport.last_command == ":TRIG:COUN 11"


def test_configure_sweep_needs_exactly_one_of_points_or_step(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="either points= or step="):
        instrument.configure_sweep(0, 1)
    with pytest.raises(RangeError, match="either points= or step="):
        instrument.configure_sweep(0, 1, points=11, step=0.1)


def test_zero_step_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="cannot be zero"):
        instrument.configure_sweep(0, 1, step=0)


def test_sweep_levels_are_checked_against_the_model(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="sweep stop level"):
        instrument.configure_sweep(0, 500, points=11)


def test_list_sweep(source):
    instrument, transport = source
    assert instrument.configure_list_sweep([0, 0.5, 1.0]) == 3
    assert ":SOUR:LIST:VOLT 0,0.5,1.0" in transport.writes
    assert transport.last_command == ":TRIG:COUN 3"


def test_list_sweep_length_is_limited(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="between 1 and 100 levels"):
        instrument.configure_list_sweep([0.0] * 101)
    with pytest.raises(RangeError, match="between 1 and 100 levels"):
        instrument.configure_list_sweep([])


# Ramping


def test_ramp_to_steps_and_ends_on_target():
    instrument, transport = build(
        {":SOUR:FUNC:MODE?": "VOLT", ":SOUR:VOLT:LEV?": "0.0"}
    )
    instrument.ramp_to(1.0, steps=10, delay=0)
    levels = [
        float(command.split()[-1])
        for command in transport.writes
        if command.startswith(":SOUR:VOLT:LEV ")
    ]
    assert len(levels) == 10
    assert levels[-1] == pytest.approx(1.0)
    assert levels == sorted(levels), "a ramp must be monotonic"


def test_ramp_target_is_checked_against_the_model(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="must be between"):
        instrument.ramp_to(500, steps=10, delay=0)


# Limit tests


def test_limit_test_configuration(source):
    instrument, transport = source
    instrument.set_limit_test(2, lower=-1e-3, upper=1e-3)
    assert transport.commands == [
        ":CALC2:LIM2:LOW:DATA -0.001",
        ":CALC2:LIM2:UPP:DATA 0.001",
        ":CALC2:LIM2:STAT 1",
    ]


def test_inverted_limits_are_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="must not be above"):
        instrument.set_limit_test(1, lower=1, upper=-1)


def test_limit_test_number_is_bounded(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="limit test number"):
        instrument.set_limit_test(13, lower=0, upper=1)


def test_statistic_selects_then_reads():
    instrument, transport = build({":CALC3:DATA?": "0.5"})
    assert instrument.statistic("mean") == [0.5]
    assert transport.commands == [":CALC3:FORM MEAN", ":CALC3:DATA?"]


# Front panel


def test_every_documented_key_has_a_name():
    """The manual lists 32 codes, of which 25 is unassigned."""
    assert len(FRONT_PANEL_KEYS) == 31
    assert set(FRONT_PANEL_KEYS.values()) == set(range(1, 33)) - {25}


def test_press_key_by_name(source):
    instrument, transport = source
    instrument.press_key("output")
    assert transport.last_command == ":SYST:KEY 24"
    instrument.press_key("measure_voltage")
    assert transport.last_command == ":SYST:KEY 15"


def test_press_key_by_code(source):
    instrument, transport = source
    instrument.press_key(15)
    assert transport.last_command == ":SYST:KEY 15"


def test_unknown_key_name_lists_the_options(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="not a front-panel key"):
        instrument.press_key("turbo")


def test_key_code_outside_the_panel_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="front-panel key code"):
        instrument.press_key(33)


def test_display_text_is_shown_and_enabled(source):
    instrument, transport = source
    instrument.display_text = "MEASURING"
    assert transport.commands == [
        ':DISP:WIND1:TEXT:DATA "MEASURING"',
        ":DISP:WIND1:TEXT:STAT 1",
    ]


def test_display_text_longer_than_the_screen_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="at most 20 characters"):
        instrument.display_text = "X" * 21


def test_display_digits_range(source):
    instrument, transport = source
    instrument.display_digits = 7
    assert transport.last_command == ":DISP:DIG 7"
    with pytest.raises(RangeError, match="display digits"):
        instrument.display_digits = 8


def test_beep(source):
    instrument, transport = source
    instrument.beep(500, 1)
    assert transport.last_command == ":SYST:BEEP 500,1"


def test_beep_outside_the_documented_range_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="beeper frequency"):
        instrument.beep(10, 1)
    with pytest.raises(RangeError, match="beeper duration"):
        instrument.beep(500, 10)


def test_local_and_remote(source):
    instrument, transport = source
    instrument.go_to_local()
    assert transport.last_command == ":SYST:LOC"
    instrument.lock_front_panel()
    assert transport.last_command == ":SYST:RWL"


# System and common commands


def test_line_frequency(source):
    instrument, transport = source
    instrument.line_frequency = 60
    assert transport.last_command == ":SYST:LFR 60"
    with pytest.raises(RangeError, match="line frequency"):
        instrument.line_frequency = 55


def test_reset_and_preset(source):
    instrument, transport = source
    instrument.reset()
    instrument.preset()
    assert transport.commands == ["*RST", ":SYST:PRES"]


def test_error_queue_is_reported():
    replies = iter(['-222,"Parameter data out of range"', '0,"No error"'])
    instrument, _ = build({"*IDN?": IDENTITY_2400})
    instrument.transport.responses = lambda command: next(replies)
    with pytest.raises(InstrumentError, match="out of range"):
        instrument.check_errors()


def test_verify_identity_rejects_a_different_instrument():
    instrument, _ = build(identity="KEITHLEY INSTRUMENTS INC.,MODEL 6221,1,C1")
    instrument.IDENTIFIER = "MODEL 2400"
    with pytest.raises(InstrumentError, match="identifies itself as"):
        instrument.verify_identity()


def test_repr_names_the_model():
    instrument, _ = build(identity=IDENTITY_2410)
    assert "2410" in repr(instrument)


@pytest.mark.parametrize(
    "action",
    [
        lambda k: setattr(k, "source_value", 1.0),
        lambda k: setattr(k, "source_range", 1.0),
        lambda k: setattr(k, "sweep_start", 1.0),
        lambda k: setattr(k, "sweep_stop", 1.0),
        lambda k: setattr(k, "sweep_step", 0.1),
        lambda k: k.configure_list_sweep([0.0, 1.0]),
        lambda k: k.ramp_to(1.0, steps=2, delay=0),
    ],
    ids=[
        "source_value",
        "source_range",
        "sweep_start",
        "sweep_stop",
        "sweep_step",
        "configure_list_sweep",
        "ramp_to",
    ],
)
def test_no_source_setter_writes_a_level_in_memory_mode(action):
    """Memory mode has no level, so :SOUR:MEM:LEV is not a real command.

    Every setter that writes a level must refuse rather than emit it.
    """
    instrument, transport = build({":SOUR:FUNC:MODE?": "MEM"})
    with pytest.raises(RangeError, match="memory mode"):
        action(instrument)
    assert transport.writes == [], "nothing should have been sent"
