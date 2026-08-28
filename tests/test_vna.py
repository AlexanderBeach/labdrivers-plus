"""Tests for the vector network analyzer driver.

Commands are asserted against the CMT VNA Programming Manual, whose command set
the Keysight ENA shares.
"""

import pytest

from labdrivers.core import RangeError, RecordingTransport
from labdrivers.coppermountain.vna import FORMATS, PARAMETERS, Vna


def build(responses=None, **kwargs):
    transport = RecordingTransport(responses or {})
    vna = Vna(transport=transport, **kwargs)
    transport.clear()
    return vna, transport


@pytest.fixture
def vna():
    return build()


# Stimulus


def test_frequency_commands(vna):
    instrument, transport = vna
    instrument.start_frequency = 1e9
    assert transport.last_command == ":SENS1:FREQ:STAR 1000000000.0"
    instrument.stop_frequency = 2e9
    assert transport.last_command == ":SENS1:FREQ:STOP 2000000000.0"


def test_frequency_outside_the_analyzer_range_is_rejected(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="start frequency"):
        instrument.start_frequency = 1e12


def test_frequency_limits_are_configurable():
    """Analyzers in this family differ in range, not in commands."""
    instrument, transport = build(maximum_frequency=20e9)
    instrument.stop_frequency = 18e9
    assert transport.last_command == ":SENS1:FREQ:STOP 18000000000.0"


def test_inverted_frequency_limits_are_rejected():
    with pytest.raises(RangeError, match="must be below the maximum"):
        build(minimum_frequency=10e9, maximum_frequency=1e9)


def test_channel_selects_the_command_path(vna):
    instrument, transport = vna
    instrument.channel = 3
    instrument.points = 201
    assert transport.last_command == ":SENS3:SWE:POIN 201"


def test_channel_is_bounded(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="measurement channel"):
        instrument.channel = 20


def test_points_below_two_is_not_a_sweep(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="sweep points"):
        instrument.points = 1


def test_if_bandwidth_and_power(vna):
    instrument, transport = vna
    instrument.if_bandwidth = 1e3
    assert transport.last_command == ":SENS1:BAND 1000.0"
    instrument.power = -10
    assert transport.last_command == ":SOUR1:POW -10"


def test_if_bandwidth_snaps_to_an_offered_step(vna):
    """The analyzer only offers a 1/1.5/2/3/5/7 ladder, so an arbitrary value
    has to land on one of its steps."""
    instrument, transport = vna
    instrument.if_bandwidth = 1400
    assert transport.last_command == ":SENS1:BAND 1500.0"
    instrument.if_bandwidth = 1100
    assert transport.last_command == ":SENS1:BAND 1000.0"


def test_power_outside_the_range_is_rejected(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="stimulus power"):
        instrument.power = 40


def test_sweep_type(vna):
    instrument, transport = vna
    instrument.sweep_type = "logarithmic"
    assert transport.last_command == ":SENS1:SWE:TYPE LOGarithmic"


def test_configure_sweep_sets_everything(vna):
    instrument, transport = vna
    instrument.configure_sweep(
        start=1e9, stop=2e9, points=401, power=-10, if_bandwidth=1e3
    )
    assert transport.writes == [
        ":SENS1:FREQ:STAR 1000000000.0",
        ":SENS1:FREQ:STOP 2000000000.0",
        ":SENS1:SWE:POIN 401",
        ":SENS1:BAND 1000.0",
        ":SOUR1:POW -10",
    ]


def test_configure_sweep_by_center_and_span(vna):
    instrument, transport = vna
    instrument.configure_sweep(center=1.5e9, span=1e9)
    assert ":SENS1:FREQ:CENT 1500000000.0" in transport.writes
    assert ":SENS1:FREQ:SPAN 1000000000.0" in transport.writes


def test_a_sweep_cannot_be_given_both_ways(vna):
    """Start/stop and center/span describe the same thing twice."""
    instrument, _ = vna
    with pytest.raises(RangeError, match="not by both"):
        instrument.configure_sweep(start=1e9, stop=2e9, center=1.5e9)


# Traces


def test_set_trace_defines_selects_and_formats():
    instrument, transport = build({":CALC1:PAR:COUN?": "2"})
    instrument.set_trace(1, "s21", "log magnitude")
    assert transport.writes == [
        ":CALC1:PAR1:DEF S21",
        ":CALC1:PAR1:SEL",
        ":CALC1:TRAC1:FORM MLOGarithmic",
    ]


def test_set_trace_raises_the_trace_count_when_needed():
    """Assigning to a trace the channel does not hold has no effect, so the
    count has to come up first."""
    instrument, transport = build({":CALC1:PAR:COUN?": "1"})
    instrument.set_trace(3, "s11")
    assert transport.writes[0] == ":CALC1:PAR:COUN 3"
    assert ":CALC1:PAR3:DEF S11" in transport.writes


def test_unknown_parameter_lists_the_options(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="measurement parameter"):
        instrument.set_trace(1, "s33")


def test_unknown_format_lists_the_options(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="trace format"):
        instrument.set_trace_format(1, "interpretive dance")


def test_every_documented_parameter_and_format_is_offered():
    assert set(PARAMETERS) >= {"s11", "s12", "s21", "s22"}
    assert "log magnitude" in FORMATS
    assert FORMATS["log magnitude"] == "MLOGarithmic"
    assert len(FORMATS) == 16


# Reading data


def test_formatted_data_drops_the_unused_second_value():
    """Formatted data carries two numbers per point, and the second is zero
    for every single-axis format."""
    instrument, _ = build(
        {
            ":SENS1:FREQ:DATA?": "1e9,2e9,3e9",
            ":CALC1:TRAC1:DATA:FDAT?": "-10,0,-20,0,-30,0",
        }
    )
    frequencies, values = instrument.read_trace(1)
    assert frequencies == pytest.approx([1e9, 2e9, 3e9])
    assert values == pytest.approx([-10, -20, -30])


def test_formatted_data_that_is_already_one_per_point_is_left_alone():
    instrument, _ = build(
        {":SENS1:FREQ:DATA?": "1e9,2e9", ":CALC1:TRAC1:DATA:FDAT?": "-10,-20"}
    )
    _, values = instrument.read_trace(1)
    assert values == pytest.approx([-10, -20])


def test_complex_data_is_paired_into_complex_numbers():
    instrument, _ = build(
        {
            ":SENS1:FREQ:DATA?": "1e9,2e9",
            ":CALC1:TRAC1:DATA:SDAT?": "0.5,0.1,-0.3,0.2",
        }
    )
    frequencies, values = instrument.read_complex_trace(1)
    assert frequencies == pytest.approx([1e9, 2e9])
    assert values == [complex(0.5, 0.1), complex(-0.3, 0.2)]


def test_complex_data_supports_magnitude_and_phase():
    """Complex values are what any further analysis needs."""
    instrument, _ = build(
        {":SENS1:FREQ:DATA?": "1e9", ":CALC1:TRAC1:DATA:SDAT?": "0.0,1.0"}
    )
    _, values = instrument.read_complex_trace(1)
    assert abs(values[0]) == pytest.approx(1.0)


def test_read_all_traces_keys_by_parameter():
    instrument, _ = build(
        {
            ":CALC1:PAR:COUN?": "2",
            ":SENS1:FREQ:DATA?": "1e9,2e9",
            ":CALC1:PAR1:DEF?": "S11",
            ":CALC1:PAR2:DEF?": "S21",
            ":CALC1:TRAC1:DATA:SDAT?": "1,0,1,0",
            ":CALC1:TRAC2:DATA:SDAT?": "0,1,0,1",
        }
    )
    frequencies, traces = instrument.read_all_traces()
    assert sorted(traces) == ["s11", "s21"]
    assert len(frequencies) == 2


# Sweep control


def test_single_sweep_stops_continuous_first():
    instrument, transport = build({"*ESR?": "1"})
    instrument.single_sweep(wait=True)
    assert transport.writes[0] == ":INIT1:CONT 0"
    assert ":INIT1:IMM" in transport.writes
    assert "*OPC" in transport.writes


def test_trigger_source(vna):
    instrument, transport = vna
    instrument.trigger_source = "bus"
    assert transport.last_command == ":TRIG:SOUR BUS"
    with pytest.raises(RangeError, match="trigger source"):
        instrument.trigger_source = "vibes"


def test_measure_sweeps_then_reads():
    instrument, transport = build(
        {
            "*ESR?": "1",
            ":SENS1:FREQ:DATA?": "1e9,2e9",
            ":CALC1:TRAC1:DATA:FDAT?": "-10,0,-20,0",
        }
    )
    frequencies, values = instrument.measure(1)
    assert ":INIT1:IMM" in transport.writes
    assert values == pytest.approx([-10, -20])


# Markers


def test_set_marker(vna):
    instrument, transport = vna
    instrument.set_marker(1, 1.5e9)
    assert transport.writes == [
        ":CALC1:PAR1:SEL",
        ":CALC1:MARK1 ON",
        ":CALC1:MARK1:X 1500000000.0",
    ]


def test_marker_frequency_is_checked_against_the_analyzer_range(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="marker frequency"):
        instrument.set_marker(1, 1e12)


def test_marker_search(vna):
    instrument, transport = build(
        {":CALC1:MARK1:X?": "1.5e9", ":CALC1:MARK1:Y?": "-3.0,0"}
    )
    frequency, value = instrument.marker_search(1, "minimum")
    assert ":CALC1:MARK1:FUNC:TYPE MINimum" in transport.writes
    assert ":CALC1:MARK1:FUNC:EXEC" in transport.writes
    assert (frequency, value) == (pytest.approx(1.5e9), pytest.approx(-3.0))


def test_marker_number_is_bounded(vna):
    instrument, _ = vna
    with pytest.raises(RangeError, match="marker number"):
        instrument.set_marker(20, 1e9)
