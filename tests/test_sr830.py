"""Tests for the SR830 lock-in amplifier driver.

Commands are asserted against the SR830 manual, Chapter 5.
"""

import pytest

from labdrivers.core import RangeError, RecordingTransport
from labdrivers.srs.sr830 import (
    SAMPLE_RATES,
    SENSITIVITIES,
    TIME_CONSTANTS,
    Sr830,
)


def build(responses=None, interface=None, **kwargs):
    transport = RecordingTransport(responses or {})
    lockin = Sr830(transport=transport, interface=interface, **kwargs)
    transport.clear()
    return lockin, transport


@pytest.fixture
def lockin():
    return build()


# Interface


def test_output_interface_is_set_at_construction():
    """Without OUTX the SR830 may answer on the port it was last told to use,
    which is the usual cause of a lock-in that connects but never replies."""
    transport = RecordingTransport()
    Sr830(transport=transport, interface="gpib")
    assert transport.writes == ["OUTX 1"]

    transport = RecordingTransport()
    Sr830(transport=transport, interface="rs232")
    assert transport.writes == ["OUTX 0"]


def test_the_output_port_follows_the_connection():
    """A lock-in reached over RS-232 while OUTX still says GPIB takes every
    command and answers into the other connector, which looks exactly like a
    dead instrument. The default follows how the connection was made."""

    class Wired(RecordingTransport):
        def __init__(self, resource_name, **kwargs):
            super().__init__(**kwargs)
            self.resource_name = resource_name

    serial = Wired("ASRL3::INSTR", default="0")
    Sr830(transport=serial)
    assert serial.writes == ["OUTX 0"]

    for resource in (
        "GPIB0::8::INSTR",
        "TCPIP0::192.168.0.20::INSTR",
        "USB0::0xB506::0x2000::1::INSTR",
    ):
        wire = Wired(resource, default="0")
        Sr830(transport=wire)
        assert wire.writes == ["OUTX 1"], resource


def test_an_explicit_interface_beats_the_connection():
    class Wired(RecordingTransport):
        def __init__(self, resource_name, **kwargs):
            super().__init__(**kwargs)
            self.resource_name = resource_name

    wire = Wired("ASRL3::INSTR", default="0")
    Sr830(transport=wire, interface="gpib")
    assert wire.writes == ["OUTX 1"]


def test_a_connection_that_does_not_say_is_left_alone():
    # A server holds the real connection and has already chosen the port, so
    # a client must not overwrite it from the other end.
    transport = RecordingTransport(default="0")
    Sr830(transport=transport)
    assert transport.writes == []


def test_interface_can_be_left_alone():
    transport = RecordingTransport()
    Sr830(transport=transport, interface=None)
    assert transport.writes == []


# Reference and phase


def test_phase(lockin):
    instrument, transport = lockin
    instrument.phase = 45.0
    assert transport.last_command == "PHAS 45.0"


@pytest.mark.parametrize("value", [-360.1, 730.0])
def test_phase_outside_the_manual_range_is_rejected(lockin, value):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="phase shift"):
        instrument.phase = value


def test_frequency(lockin):
    instrument, transport = lockin
    instrument.frequency = 1234.5
    assert transport.last_command == "FREQ 1234.5"


@pytest.mark.parametrize("value", [0.0001, 102001])
def test_frequency_outside_the_manual_range_is_rejected(lockin, value):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="reference frequency"):
        instrument.frequency = value


def test_harmonic(lockin):
    instrument, transport = lockin
    instrument.harmonic = 3
    assert transport.last_command == "HARM 3"
    with pytest.raises(RangeError, match="detection harmonic"):
        instrument.harmonic = 20000


def test_amplitude(lockin):
    instrument, transport = lockin
    instrument.amplitude = 1.0
    assert transport.last_command == "SLVL 1.0"
    with pytest.raises(RangeError, match="sine output amplitude"):
        instrument.amplitude = 0.001


def test_reference_source(lockin):
    instrument, transport = lockin
    instrument.reference_source = "internal"
    assert transport.last_command == "FMOD 1"
    instrument.reference_source = "external"
    assert transport.last_command == "FMOD 0"


def test_reference_slope(lockin):
    instrument, transport = lockin
    instrument.reference_slope = "ttl rising"
    assert transport.last_command == "RSLP 1"


# Ladders


def test_sensitivity_ladder_matches_the_manual():
    """SENS runs 0 to 26, from 2 nV to 1 V full scale."""
    assert len(SENSITIVITIES) == 27
    assert SENSITIVITIES[0] == 2e-9
    assert SENSITIVITIES[-1] == 1.0
    assert SENSITIVITIES == sorted(SENSITIVITIES)


def test_time_constant_ladder_matches_the_manual():
    """OFLT runs 0 to 19, from 10 us to 30 ks."""
    assert len(TIME_CONSTANTS) == 20
    assert TIME_CONSTANTS[0] == 10e-6
    assert TIME_CONSTANTS[-1] == 30e3
    assert TIME_CONSTANTS == sorted(TIME_CONSTANTS)


def test_sample_rate_ladder_matches_the_manual():
    """SRAT runs 0 to 13 in hertz, with 14 meaning external trigger."""
    assert len(SAMPLE_RATES) == 14
    assert SAMPLE_RATES[0] == 0.0625
    assert SAMPLE_RATES[-1] == 512.0


def test_sensitivity_snaps_to_the_nearest_setting(lockin):
    instrument, transport = lockin
    instrument.sensitivity = 1e-6  # exactly index 8
    assert transport.last_command == "SENS 8"


def test_sensitivity_between_settings_picks_the_closest(lockin):
    instrument, transport = lockin
    instrument.sensitivity = 1.1e-6  # nearer 1 uV than 2 uV
    assert transport.last_command == "SENS 8"


def test_sensitivity_reads_back_as_a_real_value():
    instrument, _ = build({"SENS?": "8"})
    assert instrument.sensitivity == 1e-6


def test_time_constant_snaps(lockin):
    instrument, transport = lockin
    instrument.time_constant = 0.1
    assert transport.last_command == "OFLT 8"


def test_time_constant_reads_back():
    instrument, _ = build({"OFLT?": "10"})
    assert instrument.time_constant == 1.0


def test_filter_slope(lockin):
    instrument, transport = lockin
    instrument.filter_slope = 24
    assert transport.last_command == "OFSL 3"
    with pytest.raises(RangeError, match="low pass filter slope"):
        instrument.filter_slope = 9


def test_filter_slope_reads_back():
    instrument, _ = build({"OFSL?": "2"})
    assert instrument.filter_slope == 18


def test_reserve(lockin):
    instrument, transport = lockin
    instrument.reserve = "low noise"
    assert transport.last_command == "RMOD 2"


# Input


def test_input_configuration(lockin):
    instrument, transport = lockin
    instrument.input_configuration = "a-b"
    assert transport.last_command == "ISRC 1"
    instrument.input_configuration = "i100m"
    assert transport.last_command == "ISRC 3"


def test_input_grounding_and_coupling(lockin):
    instrument, transport = lockin
    instrument.input_grounding = "ground"
    assert transport.last_command == "IGND 1"
    instrument.input_coupling = "dc"
    assert transport.last_command == "ICPL 1"


def test_line_filter(lockin):
    instrument, transport = lockin
    instrument.line_filter = "both"
    assert transport.last_command == "ILIN 3"


def test_synchronous_filter(lockin):
    instrument, transport = lockin
    instrument.synchronous_filter = True
    assert transport.last_command == "SYNC 1"


# Reading


def test_snapshot_reads_several_values_at_one_instant():
    instrument, transport = build({"SNAP? 1,2": "1.0,2.0"})
    assert instrument.snapshot("x", "y") == [1.0, 2.0]
    assert transport.last_command == "SNAP? 1,2"


def test_snapshot_supports_six_parameters():
    instrument, transport = build({"SNAP? 1,2,3,4,9,5": "1,2,3,4,5,6"})
    values = instrument.snapshot("x", "y", "r", "theta", "frequency", "aux1")
    assert len(values) == 6


@pytest.mark.parametrize("count", [1, 7])
def test_snapshot_length_is_bounded(lockin, count):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="between 2 and 6"):
        instrument.snapshot(*(["x"] * count))


def test_individual_outputs():
    instrument, transport = build({"OUTP? 1": "0.5", "OUTP? 3": "0.7"})
    assert instrument.x == 0.5
    assert instrument.magnitude == 0.7
    assert transport.queries == ["OUTP? 1", "OUTP? 3"]


def test_bad_output_parameter_is_rejected(lockin):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="output parameter"):
        instrument.output("z")


# Auxiliary


def test_auxiliary_output(lockin):
    instrument, transport = lockin
    instrument.set_auxiliary_output(2, -5.0)
    assert transport.last_command == "AUXV 2,-5.0"


def test_auxiliary_output_range(lockin):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="auxiliary output voltage"):
        instrument.set_auxiliary_output(1, 11.0)


def test_auxiliary_channel_is_bounded(lockin):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="auxiliary output channel"):
        instrument.set_auxiliary_output(5, 0.0)


def test_auxiliary_input():
    instrument, transport = build({"OAUX? 3": "1.5"})
    assert instrument.auxiliary_input(3) == 1.5


# Display


def test_set_display(lockin):
    instrument, transport = lockin
    instrument.set_display(1, "r", ratio="aux1")
    assert transport.last_command == "DDEF 1,1,1"


def test_channel_two_has_its_own_display_options(lockin):
    instrument, transport = lockin
    instrument.set_display(2, "theta")
    assert transport.last_command == "DDEF 2,1,0"
    with pytest.raises(RangeError, match="channel 2 display"):
        instrument.set_display(2, "x")


def test_offset_and_expand(lockin):
    instrument, transport = lockin
    instrument.set_offset_and_expand("x", offset=50.0, expand=10)
    assert transport.last_command == "OEXP 1,50.0,1"


def test_offset_range(lockin):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="offset"):
        instrument.set_offset_and_expand("x", offset=200.0)


def test_bad_expand_is_rejected(lockin):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="expand"):
        instrument.set_offset_and_expand("x", expand=5)


def test_auto_functions(lockin):
    instrument, transport = lockin
    instrument.auto_gain()
    instrument.auto_phase()
    instrument.auto_offset("r")
    assert transport.writes == ["AGAN", "APHS", "AOFF 3"]


# Buffer


def test_sample_rate_snaps(lockin):
    instrument, transport = lockin
    instrument.sample_rate = 512
    assert transport.last_command == "SRAT 13"


def test_sample_rate_accepts_trigger(lockin):
    instrument, transport = lockin
    instrument.sample_rate = "trigger"
    assert transport.last_command == "SRAT 14"


def test_sample_rate_reads_back_trigger():
    instrument, _ = build({"SRAT?": "14"})
    assert instrument.sample_rate == "trigger"


def test_scan_control(lockin):
    instrument, transport = lockin
    instrument.start_scan()
    instrument.pause_scan()
    instrument.reset_scan()
    assert transport.writes == ["STRT", "PAUS", "REST"]


def test_read_buffer_defaults_to_everything_stored():
    instrument, transport = build({"SPTS?": "100", "TRCA? 1,0,100": "1.0,2.0"})
    assert instrument.read_buffer(1) == [1.0, 2.0]
    assert transport.last_command == "TRCA? 1,0,100"


def test_read_buffer_with_an_explicit_window():
    instrument, transport = build({"TRCA? 2,10,5": "1,2,3,4,5"})
    assert len(instrument.read_buffer(2, start=10, count=5)) == 5


# Status


def test_overload_bits_are_decoded():
    instrument, _ = build({"LIAS?": "1"})
    assert instrument.input_overload() is True
    assert instrument.reference_unlocked() is False

    instrument, _ = build({"LIAS?": "8"})
    assert instrument.reference_unlocked() is True
    assert instrument.input_overload() is False


def test_overloaded_covers_the_three_overload_bits():
    instrument, _ = build({"LIAS?": "4"})
    assert instrument.overloaded() is True
    instrument, _ = build({"LIAS?": "8"})
    assert instrument.overloaded() is False, "an unlocked reference is not an overload"


# Setups


def test_save_and_recall_setup(lockin):
    instrument, transport = lockin
    instrument.save_setup(3)
    instrument.recall_setup(3)
    assert transport.writes == ["SSET 3", "RSET 3"]


@pytest.mark.parametrize("buffer", [0, 10])
def test_setup_buffer_is_bounded(lockin, buffer):
    instrument, _ = lockin
    with pytest.raises(RangeError, match="setup buffer"):
        instrument.save_setup(buffer)


def test_local_and_remote(lockin):
    instrument, transport = lockin
    instrument.go_to_local()
    instrument.lock_front_panel()
    assert transport.writes == ["LOCL 0", "LOCL 2"]
