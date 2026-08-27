"""Tests for the oscilloscope drivers.

Commands are asserted against the InfiniiVision 3000 X-Series Programmer's
Guide. The waveform tests matter most: a trace read without applying the
preamble looks plausible and is wrong by whatever the scale and offset are.
"""

import pytest

from labdrivers.core import InstrumentError, RangeError, RecordingTransport
from labdrivers.keysight.infiniivision import (
    MEASUREMENT_FAILED,
    PREAMBLE_FIELDS,
    InfiniiVision,
)

IDENTITY_4CH = "AGILENT TECHNOLOGIES,MSO-X 3054A,MY12345678,02.35.2013"
IDENTITY_2CH = "KEYSIGHT TECHNOLOGIES,MSO-X 3012T,MY12345678,07.20.2017"

# format=word, type=normal, 5 points, count 1, 1 ns/sample starting at 0 with
# reference 0, 1 mV/count with 0 V origin and reference 128.
PREAMBLE = "1,0,5,1,1e-9,0,0,1e-3,0,128"


def build(responses=None, identity=IDENTITY_4CH, default=None, **kwargs):
    replies = {"*IDN?": identity}
    replies.update(responses or {})
    transport = RecordingTransport(replies, default=default)
    scope = InfiniiVision(transport=transport, **kwargs)
    transport.clear()
    return scope, transport


@pytest.fixture
def scope():
    return build()


# Model detection


def test_channel_count_comes_from_the_model_number():
    """An MSO-X 3054A has four channels, and an MSO-X 3012T has two."""
    assert build(identity=IDENTITY_4CH)[0].channel_count == 4
    assert build(identity=IDENTITY_2CH)[0].channel_count == 2


def test_channel_count_can_be_given_explicitly():
    scope, _ = build(channels=2)
    assert scope.channel_count == 2


def test_a_channel_the_scope_does_not_have_is_rejected():
    scope, _ = build(identity=IDENTITY_2CH)
    with pytest.raises(RangeError, match="input channel"):
        scope.channel_scale(3)


# Run control


def test_run_control_commands(scope):
    instrument, transport = scope
    instrument.run()
    instrument.stop()
    instrument.single()
    instrument.digitize(1)
    assert transport.writes == [":RUN", ":STOP", ":SINGle", ":DIGitize CHANnel1"]


# Vertical


def test_set_channel_sends_probe_before_scale(scope):
    """Probe attenuation rescales the vertical axis, so it has to be set
    first or the scale that follows means something different."""
    instrument, transport = scope
    instrument.set_channel(1, scale=0.5, offset=0.1, coupling="dc", probe=10)
    assert transport.writes.index(":CHANnel1:PROBe 10") < transport.writes.index(
        ":CHANnel1:SCALe 0.5"
    )
    assert ":CHANnel1:OFFSet 0.1" in transport.writes
    assert ":CHANnel1:COUPling DC" in transport.writes
    assert transport.writes[-1] == ":CHANnel1:DISPlay 1"


def test_channel_coupling_reads_back():
    scope, _ = build({":CHANnel1:COUPling?": "AC"})
    assert scope.channel_coupling(1) == "ac"


def test_impedance(scope):
    instrument, transport = scope
    instrument.set_channel_impedance(2, "50")
    assert transport.last_command == ":CHANnel2:IMPedance FIFTy"
    instrument.set_channel_impedance(2, "1m")
    assert transport.last_command == ":CHANnel2:IMPedance ONEMeg"


def test_bad_impedance_lists_the_options(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="input impedance"):
        instrument.set_channel_impedance(1, "75")


def test_vertical_scale_is_bounded(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="vertical scale"):
        instrument.set_channel_scale(1, 100)


# Trigger


def test_edge_trigger_setup(scope):
    instrument, transport = scope
    instrument.set_edge_trigger(source=2, level=0.25, slope="negative", sweep="normal")
    assert transport.writes == [
        ":TRIGger:MODE EDGE",
        ":TRIGger:EDGE:SOURce CHANnel2",
        ":TRIGger:EDGE:LEVel 0.25",
        ":TRIGger:EDGE:SLOPe NEGative",
        ":TRIGger:SWEep NORMal",
    ]


def test_external_trigger_source(scope):
    instrument, transport = scope
    instrument.set_edge_trigger(source="external", level=1.0)
    assert ":TRIGger:EDGE:SOURce EXTernal" in transport.writes


def test_nonsense_trigger_source_is_rejected(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="trigger source"):
        instrument.set_edge_trigger(source="telepathy")


def test_triggered_reads_the_event_register():
    scope, transport = build({":TER?": "1"})
    assert scope.triggered() is True
    assert transport.queries == [":TER?"]


# Acquisition


def test_average_sets_type_and_count(scope):
    instrument, transport = scope
    instrument.average(64)
    assert transport.writes == [":ACQuire:TYPE AVERage", ":ACQuire:COUNt 64"]


def test_average_count_is_bounded(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="average count"):
        instrument.average_count = 1


# Preamble and waveform scaling


def test_preamble_field_order_matches_the_manual():
    assert PREAMBLE_FIELDS == (
        "format",
        "type",
        "points",
        "count",
        "x_increment",
        "x_origin",
        "x_reference",
        "y_increment",
        "y_origin",
        "y_reference",
    )


def test_preamble_is_decoded_into_names():
    scope, _ = build({":WAVeform:PREamble?": PREAMBLE})
    preamble = scope.preamble()
    assert preamble["format"] == "word"
    assert preamble["type"] == "normal"
    assert preamble["points"] == 5
    assert preamble["y_increment"] == pytest.approx(1e-3)
    assert preamble["y_reference"] == pytest.approx(128)


def test_a_short_preamble_is_reported():
    scope, _ = build({":WAVeform:PREamble?": "1,0,5"})
    with pytest.raises(InstrumentError, match="preamble fields"):
        scope.preamble()


def test_raw_samples_are_converted_to_volts_and_seconds():
    """A count of 128 is the reference, so it must come out at the origin."""
    preamble = {
        "x_increment": 1e-9,
        "x_origin": -2e-9,
        "x_reference": 0,
        "y_increment": 1e-3,
        "y_origin": 0.5,
        "y_reference": 128,
        "points": 3,
    }
    times, volts = InfiniiVision.scale_waveform([128, 138, 118], preamble)
    assert times == pytest.approx([-2e-9, -1e-9, 0.0])
    assert volts == pytest.approx([0.5, 0.51, 0.49])


def test_ascii_samples_are_already_in_volts():
    preamble = {
        "x_increment": 1e-9,
        "x_origin": 0.0,
        "x_reference": 0,
        "y_increment": 1e-3,
        "y_origin": 0.5,
        "y_reference": 128,
    }
    _, volts = InfiniiVision.scale_waveform([0.1, 0.2], preamble, already_scaled=True)
    assert volts == pytest.approx([0.1, 0.2])


def test_read_waveform_configures_then_reads():
    scope, transport = build(
        {
            ":WAVeform:PREamble?": PREAMBLE,
            ":WAVeform:DATA?": [128, 129, 130, 131, 132],
        }
    )
    times, volts = scope.read_waveform(1, waveform_format="word")
    assert ":WAVeform:SOURce CHANnel1" in transport.writes
    assert ":WAVeform:FORMat WORD" in transport.writes
    assert ":WAVeform:BYTeorder MSBFirst" in transport.writes
    assert len(times) == len(volts) == 5
    assert volts == pytest.approx([0.0, 0.001, 0.002, 0.003, 0.004])
    assert times == pytest.approx([0.0, 1e-9, 2e-9, 3e-9, 4e-9])


def test_read_waveform_in_ascii_does_not_rescale():
    scope, transport = build(
        {":WAVeform:PREamble?": PREAMBLE, ":WAVeform:DATA?": "0.1,0.2,0.3"}
    )
    _, volts = scope.read_waveform(1, waveform_format="ascii")
    assert ":WAVeform:FORMat ASCii" in transport.writes
    assert volts == pytest.approx([0.1, 0.2, 0.3])


def test_read_waveform_can_limit_the_point_count():
    scope, transport = build(
        {":WAVeform:PREamble?": PREAMBLE, ":WAVeform:DATA?": [128] * 5}
    )
    scope.read_waveform(1, points=1000)
    assert ":WAVeform:POINts 1000" in transport.writes


def test_bad_waveform_format_is_rejected(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="waveform format"):
        instrument.read_waveform(1, waveform_format="binary-ish")


def test_read_waveforms_defaults_to_the_displayed_channels():
    replies = {
        ":WAVeform:PREamble?": PREAMBLE,
        ":WAVeform:DATA?": [128] * 5,
        ":CHANnel1:DISPlay?": "1",
        ":CHANnel2:DISPlay?": "0",
        ":CHANnel3:DISPlay?": "1",
        ":CHANnel4:DISPlay?": "0",
    }
    scope, _ = build(replies)
    times, traces = scope.read_waveforms()
    assert sorted(traces) == [1, 3]
    assert len(times) == 5


def test_reading_with_nothing_displayed_says_so():
    replies = {f":CHANnel{number}:DISPlay?": "0" for number in range(1, 5)}
    scope, _ = build(replies)
    with pytest.raises(RangeError, match="No channels are displayed"):
        scope.read_waveforms()


# Measurements


def test_measurement_returns_a_value():
    scope, transport = build({":MEASure:VPP? CHANnel1": "1.234"})
    assert scope.measure("peak to peak") == pytest.approx(1.234)
    assert transport.queries == [":MEASure:VPP? CHANnel1"]


def test_a_measurement_the_scope_could_not_make_is_none():
    """The scope reports failure as a huge number rather than raising, and
    9.9e37 volts is not a reading anyone wants to plot."""
    scope, _ = build({":MEASure:FREQuency? CHANnel1": str(MEASUREMENT_FAILED)})
    assert scope.measure("frequency") is None


def test_unknown_measurement_lists_the_options(scope):
    instrument, _ = scope
    with pytest.raises(RangeError, match="measurement"):
        instrument.measure("vibes")


def test_measure_all_returns_every_quantity():
    scope, _ = build(default="1.0")
    values = scope.measure_all(1, quantities=["frequency", "peak to peak"])
    assert sorted(values) == ["frequency", "peak to peak"]
