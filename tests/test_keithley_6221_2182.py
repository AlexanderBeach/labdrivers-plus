"""Tests for the Keithley 6221 current source and 2182 nanovoltmeter.

These two work as a pair over the trigger link, so they are tested together.
Commands are asserted against the 6220/6221 Reference Manual and the
2182/2182A User's Manual.
"""

import pytest

from labdrivers.core import RangeError, RecordingTransport
from labdrivers.keithley.keithley2182 import CHANNEL_VOLTAGE_LIMITS, Keithley2182
from labdrivers.keithley.keithley6221 import MAXIMUM_CURRENT, Keithley6221


def build_6221(responses=None):
    transport = RecordingTransport(responses or {})
    source = Keithley6221(transport=transport)
    transport.clear()  # drop the FORM:SREG sent at construction
    return source, transport


def build_2182(responses=None, **kwargs):
    transport = RecordingTransport(responses or {})
    meter = Keithley2182(transport=transport, **kwargs)
    transport.clear()
    return meter, transport


@pytest.fixture
def source():
    return build_6221()


@pytest.fixture
def meter():
    return build_2182()


# 6221: construction and output


def test_status_registers_are_set_to_ascii():
    """Binary status registers cannot be parsed by the status queries."""
    transport = RecordingTransport()
    Keithley6221(transport=transport)
    assert transport.writes == ["FORM:SREG ASC"]


def test_output(source):
    instrument, transport = source
    instrument.output = True
    assert transport.last_command == "OUTP:STAT 1"
    instrument.output = "off"
    assert transport.last_command == "OUTP:STAT 0"


def test_output_getter_reads_a_string_reply():
    """The reply arrives as a string and must be converted, not compared."""
    instrument, _ = build_6221({"OUTP:STAT?": "1"})
    assert instrument.output is True
    instrument, _ = build_6221({"OUTP:STAT?": "0"})
    assert instrument.output is False


def test_source_current(source):
    instrument, transport = source
    instrument.source_current = 1e-3
    assert transport.last_command == "SOUR:CURR 0.001"


def test_source_current_beyond_105_milliamps_is_rejected(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="source current"):
        instrument.source_current = 0.2


def test_compliance(source):
    instrument, transport = source
    instrument.compliance = 10
    assert transport.last_command == "SOUR:CURR:COMP 10"


@pytest.mark.parametrize("value", [0.05, 106])
def test_compliance_outside_the_manual_range_is_rejected(source, value):
    instrument, _ = source
    with pytest.raises(RangeError, match="compliance voltage"):
        instrument.compliance = value


# 6221: waveform


def test_wave_function(source):
    instrument, transport = source
    instrument.wave_function = "square"
    assert transport.last_command == "SOUR:WAVE:FUNC SQU"
    instrument.wave_function = "arbitrary2"
    assert transport.last_command == "SOUR:WAVE:FUNC ARB2"


def test_wave_amplitude(source):
    instrument, transport = source
    instrument.wave_amplitude = 1e-6
    assert transport.last_command == "SOUR:WAVE:AMPL 1e-06"


@pytest.mark.parametrize("value", [1e-13, 0.2])
def test_wave_amplitude_outside_the_range_is_rejected(source, value):
    instrument, _ = source
    with pytest.raises(RangeError, match="waveform amplitude"):
        instrument.wave_amplitude = value


def test_wave_frequency(source):
    instrument, transport = source
    instrument.wave_frequency = 17.777
    assert transport.last_command == "SOUR:WAVE:FREQ 17.777"


@pytest.mark.parametrize("value", [1e-4, 2e5])
def test_wave_frequency_outside_the_range_is_rejected(source, value):
    instrument, _ = source
    with pytest.raises(RangeError, match="waveform frequency"):
        instrument.wave_frequency = value


def test_waveform_lifecycle(source):
    instrument, transport = source
    instrument.arm_waveform()
    instrument.start_waveform()
    instrument.abort_waveform()
    assert transport.writes == ["SOUR:WAVE:ARM", "SOUR:WAVE:INIT", "SOUR:WAVE:ABOR"]


def test_wave_duration_accepts_infinite(source):
    instrument, transport = source
    instrument.wave_duration_time = "infinite"
    assert transport.last_command == "SOUR:WAVE:DUR:TIME INF"


def test_phase_marker(source):
    instrument, transport = source
    instrument.set_phase_marker(True, level=90, line=2)
    assert transport.writes == [
        "SOUR:WAVE:PMAR:STAT 1",
        "SOUR:WAVE:PMAR:LEV 90",
        "SOUR:WAVE:PMAR:OLIN 2",
    ]


def test_phase_marker_line_must_be_a_trigger_link_line(source):
    instrument, _ = source
    with pytest.raises(RangeError, match="trigger link lines"):
        instrument.set_phase_marker(True, level=90, line=7)


# 6221: delta modes need a 2182


def test_delta_requires_a_nanovoltmeter():
    """Delta, pulse delta and differential conductance all read the voltage
    through a 2182A on the trigger link, so arming without one must fail."""
    instrument, _ = build_6221({"SOUR:DELT:NVPR?": "0"})
    with pytest.raises(RangeError, match="needs a Keithley 2182A"):
        instrument.configure_delta(high=1e-3)


def test_pulse_delta_requires_a_nanovoltmeter():
    instrument, _ = build_6221({"SOUR:PDEL:NVPR?": "0"})
    with pytest.raises(RangeError, match="needs a Keithley 2182A"):
        instrument.configure_pulse_delta(high=1e-3)


def test_differential_conductance_requires_a_nanovoltmeter():
    instrument, _ = build_6221({"SOUR:DCON:NVPR?": "0"})
    with pytest.raises(RangeError, match="needs a Keithley 2182A"):
        instrument.configure_differential_conductance(0, 1e-3, 1e-4, 1e-5)


def test_configure_delta_defaults_to_a_symmetric_reversal():
    """Symmetric reversal is what cancels the thermoelectric offset."""
    instrument, transport = build_6221({"SOUR:DELT:NVPR?": "1"})
    instrument.configure_delta(high=1e-3)
    assert "SOUR:DELT:HIGH 0.001" in transport.writes
    assert "SOUR:DELT:LOW -0.001" in transport.writes


def test_configure_delta_accepts_an_explicit_low():
    instrument, transport = build_6221({"SOUR:DELT:NVPR?": "1"})
    instrument.configure_delta(high=1e-3, low=-2e-3)
    assert "SOUR:DELT:LOW -0.002" in transport.writes


def test_delta_count_accepts_infinite():
    instrument, transport = build_6221({"SOUR:DELT:NVPR?": "1"})
    instrument.configure_delta(high=1e-3, count="infinite")
    assert "SOUR:DELT:COUN INF" in transport.writes


def test_pulse_measurement_delay_may_not_outlast_the_pulse():
    """A reading taken after the pulse ends measures the baseline, not the pulse."""
    instrument, _ = build_6221({"SOUR:PDEL:NVPR?": "1"})
    with pytest.raises(RangeError, match="not be longer than the pulse"):
        instrument.configure_pulse_delta(
            high=1e-3, width=100e-6, measurement_delay=200e-6
        )


def test_configure_pulse_delta_sends_the_full_setup():
    instrument, transport = build_6221({"SOUR:PDEL:NVPR?": "1"})
    instrument.configure_pulse_delta(
        high=1e-3, low=0, width=200e-6, measurement_delay=100e-6, interval=5, count=10
    )
    assert "SOUR:PDEL:HIGH 0.001" in transport.writes
    assert "SOUR:PDEL:WIDT 0.0002" in transport.writes
    assert "SOUR:PDEL:SDEL 0.0001" in transport.writes
    assert "SOUR:PDEL:INT 5" in transport.writes
    assert "SOUR:PDEL:COUN 10" in transport.writes
    assert "SOUR:PDEL:SWE OFF" in transport.writes


def test_differential_conductance_setup():
    instrument, transport = build_6221({"SOUR:DCON:NVPR?": "1"})
    instrument.configure_differential_conductance(
        start=-1e-3, stop=1e-3, step=1e-4, delta=1e-5
    )
    assert transport.writes == [
        "SOUR:DCON:STAR -0.001",
        "SOUR:DCON:STOP 0.001",
        "SOUR:DCON:STEP 0.0001",
        "SOUR:DCON:DELT 1e-05",
        "SOUR:DCON:DEL 0.002",
        "SOUR:DCON:CAB ON",
    ]


def test_zero_step_is_rejected():
    instrument, _ = build_6221({"SOUR:DCON:NVPR?": "1"})
    with pytest.raises(RangeError, match="cannot be zero"):
        instrument.configure_differential_conductance(0, 1e-3, 0, 1e-5)


def test_measurement_unit(source):
    instrument, transport = source
    instrument.measurement_unit = "ohms"
    assert transport.last_command == "UNIT OHMS"
    with pytest.raises(RangeError, match="measurement unit"):
        instrument.measurement_unit = "furlongs"


def test_filter_count_range_matches_the_manual(source):
    instrument, transport = source
    instrument.filter_count = 300
    assert transport.last_command == "SENS:AVER:COUN 300"
    with pytest.raises(RangeError, match="filter count"):
        instrument.filter_count = 301


# 2182: channels


def test_channel_selection(meter):
    instrument, transport = meter
    instrument.channel = 2
    assert transport.last_command == ":SENS:CHAN 2"
    instrument.channel = 0  # internal temperature sensor
    assert transport.last_command == ":SENS:CHAN 0"


def test_channel_outside_zero_to_two_is_rejected(meter):
    instrument, _ = meter
    with pytest.raises(RangeError, match="measurement channel"):
        instrument.channel = 3


def test_channel_one_uses_the_default_path(meter):
    """Channel 1 is the implicit path, channel 2 is spelled out."""
    instrument, transport = meter
    instrument.set_voltage_range(1.0, channel=1)
    assert transport.last_command == ":SENS:VOLT:RANG 1.0"
    instrument.set_voltage_range(1.0, channel=2)
    assert transport.last_command == ":SENS:VOLT:CHAN2:RANG 1.0"


def test_channel_voltage_limits_differ():
    """Channel 1 reads to 120 V, channel 2 only to 12 V."""
    assert CHANNEL_VOLTAGE_LIMITS == {1: 120.0, 2: 12.0}
    instrument, _ = build_2182()
    instrument.set_voltage_range(100, channel=1)
    with pytest.raises(RangeError, match="channel 2 voltage range"):
        instrument.set_voltage_range(100, channel=2)


def test_internal_sensor_is_not_a_voltage_channel(meter):
    instrument, _ = meter
    with pytest.raises(RangeError, match="measurement channel"):
        instrument.set_voltage_range(1.0, channel=0)


# 2182: integration and filtering


def test_integration_time_limit_follows_the_line_frequency():
    """The manual gives 0.01 to 60 PLC at 60 Hz, 0.01 to 50 PLC at 50 Hz."""
    on_60, transport = build_2182(line_frequency=60)
    on_60.integration_time = 60
    assert transport.last_command == ":SENS:VOLT:NPLC 60"

    on_50, _ = build_2182(line_frequency=50)
    with pytest.raises(RangeError, match="integration time at 50 Hz"):
        on_50.integration_time = 60


def test_bad_line_frequency_is_rejected():
    with pytest.raises(RangeError, match="50 or 60"):
        build_2182(line_frequency=55)


def test_digital_filter_setup(meter):
    instrument, transport = meter
    instrument.set_digital_filter(count=20, filter_type="repeating", window=0.1)
    assert transport.writes == [
        ":SENS:VOLT:DFIL:COUN 20",
        ":SENS:VOLT:DFIL:TCON REPeat",
        ":SENS:VOLT:DFIL:WIND 0.1",
        ":SENS:VOLT:DFIL:STAT 1",
    ]


@pytest.mark.parametrize("count", [0, 101])
def test_digital_filter_count_is_bounded(meter, count):
    instrument, _ = meter
    with pytest.raises(RangeError, match="filter count"):
        instrument.set_digital_filter(count=count)


def test_filter_window_is_a_percentage(meter):
    instrument, _ = meter
    with pytest.raises(RangeError, match="filter window"):
        instrument.set_digital_filter(window=20)


def test_analog_filter(meter):
    instrument, transport = meter
    instrument.set_analog_filter(True, channel=2)
    assert transport.last_command == ":SENS:VOLT:CHAN2:LPAS 1"


# 2182: relative


def test_acquire_relative_nulls_and_enables(meter):
    instrument, transport = meter
    instrument.acquire_relative(channel=1)
    assert transport.writes == [":SENS:VOLT:REF:ACQ", ":SENS:VOLT:REF:STAT 1"]


def test_relative_limits_follow_the_channel(meter):
    instrument, _ = meter
    with pytest.raises(RangeError, match="channel 2 relative value"):
        instrument.set_relative(50, channel=2)


# 2182: reading


def test_fresh_and_latest_are_different_queries():
    instrument, transport = build_2182(
        {":SENS:DATA:FRES?": "1e-9", ":SENS:DATA:LAT?": "2e-9"}
    )
    assert instrument.fresh() == 1e-9
    assert instrument.latest() == 2e-9
    assert transport.queries == [":SENS:DATA:FRES?", ":SENS:DATA:LAT?"]


def test_read_returns_a_single_float():
    instrument, _ = build_2182({":READ?": "1.234E-09"})
    assert instrument.read() == pytest.approx(1.234e-9)


def test_trigger_source(meter):
    instrument, transport = meter
    instrument.trigger_source = "external"
    assert transport.last_command == ":TRIG:SOUR EXT"
    with pytest.raises(RangeError, match="trigger source"):
        instrument.trigger_source = "telepathy"


def test_trigger_count_accepts_infinite(meter):
    instrument, transport = meter
    instrument.trigger_count = "infinite"
    assert transport.last_command == ":TRIG:COUN INF"
    with pytest.raises(RangeError, match="trigger count"):
        instrument.trigger_count = 10000


# 2182: temperature


def test_thermocouple_types(meter):
    instrument, transport = meter
    instrument.thermocouple = "k"
    assert transport.last_command == ":SENS:TEMP:TC K"
    with pytest.raises(RangeError, match="thermocouple type"):
        instrument.thermocouple = "Z"


def test_temperature_unit(meter):
    instrument, transport = meter
    instrument.temperature_unit = "kelvin"
    assert transport.last_command == ":UNIT:TEMP K"


def test_reference_junction_range(meter):
    instrument, _ = meter
    with pytest.raises(RangeError, match="reference junction temperature"):
        instrument.reference_junction_temperature = 100


# 2182: front panel


def test_display_text_is_shorter_than_on_the_2400(meter):
    """The 2182's display holds 12 characters, not the 2400's 20."""
    instrument, transport = meter
    instrument.display_text = "MEASURING"
    assert transport.writes == [':DISP:TEXT:DATA "MEASURING"', ":DISP:TEXT:STAT 1"]
    with pytest.raises(RangeError, match="at most 12 characters"):
        instrument.display_text = "X" * 13


def test_neither_driver_exposes_a_source_it_does_not_have():
    """A 2182 has no source and a 6221 has no voltmeter."""
    assert not hasattr(Keithley2182, "source_current")
    assert not hasattr(Keithley2182, "compliance")
    assert not hasattr(Keithley6221, "voltage_range")
    assert not hasattr(Keithley6221, "measure_functions")


def test_maximum_current_matches_the_manual():
    assert MAXIMUM_CURRENT == 0.105
