"""Tests for the function and arbitrary waveform generator drivers.

Commands are asserted against the 33500 Series Operating and Service Guide and
the DG1000Z Series Programming Guide.
"""

import pytest

from labdrivers.core import RangeError, RecordingTransport
from labdrivers.keysight.keysight33500 import MODELS as KEYSIGHT_MODELS
from labdrivers.keysight.keysight33500 import Keysight33500
from labdrivers.rigol.rigoldg1000z import MODELS as RIGOL_MODELS
from labdrivers.rigol.rigoldg1000z import RigolDG1000Z

KEYSIGHT_1CH = "Agilent Technologies,33511B,MY52301234,3.03"
KEYSIGHT_2CH = "Keysight Technologies,33622A,MY52301234,5.01"
RIGOL_IDENTITY = "Rigol Technologies,DG1062Z,DG1ZA00000001,00.02.01"

# 50 ohms is the reply for a terminated output, and 9.9E37 is how both
# instruments report a high-impedance one.
INTO_50 = "50"
INTO_HIGH_Z = "9.9E37"


def build_keysight(responses=None, identity=KEYSIGHT_2CH, **kwargs):
    replies = {
        "*IDN?": identity,
        "OUTP1:LOAD?": INTO_50,
        "OUTP2:LOAD?": INTO_50,
        "SOUR1:VOLT:UNIT?": "VPP",
        "SOUR2:VOLT:UNIT?": "VPP",
    }
    replies.update(responses or {})
    transport = RecordingTransport(replies)
    instrument = Keysight33500(transport=transport, **kwargs)
    transport.clear()
    return instrument, transport


def build_rigol(responses=None, **kwargs):
    replies = {
        "*IDN?": RIGOL_IDENTITY,
        ":OUTP1:IMP?": INTO_50,
        ":OUTP2:IMP?": INTO_50,
        ":SOUR1:VOLT:UNIT?": "VPP",
        ":SOUR2:VOLT:UNIT?": "VPP",
    }
    replies.update(responses or {})
    transport = RecordingTransport(replies)
    instrument = RigolDG1000Z(transport=transport, **kwargs)
    transport.clear()
    return instrument, transport


@pytest.fixture
def keysight():
    return build_keysight()


@pytest.fixture
def rigol():
    return build_rigol()


# Model detection


def test_keysight_model_sets_the_frequency_limit():
    instrument, _ = build_keysight(identity=KEYSIGHT_2CH)
    assert instrument.model == "33622A"
    assert instrument.maximum_frequency == 120e6
    assert instrument.channel_count == 2


def test_keysight_single_channel_model_rejects_channel_two():
    instrument, _ = build_keysight(identity=KEYSIGHT_1CH)
    assert instrument.channel_count == 1
    with pytest.raises(RangeError, match="output channel"):
        instrument.channel = 2


@pytest.mark.parametrize(
    "model,limit", [(m, f) for m, (_, f) in KEYSIGHT_MODELS.items()]
)
def test_every_keysight_model_has_a_frequency_limit(model, limit):
    instrument, _ = build_keysight(model=model)
    assert instrument.maximum_frequency == limit


def test_frequency_above_the_model_limit_is_rejected():
    instrument, _ = build_keysight(identity=KEYSIGHT_1CH)  # 20 MHz part
    instrument.frequency = 20e6
    with pytest.raises(RangeError, match="frequency"):
        instrument.frequency = 50e6


def test_rigol_model_detection():
    instrument, _ = build_rigol()
    assert instrument.model == "DG1062Z"
    assert instrument.maximum_frequency == 60e6


@pytest.mark.parametrize("model,limit", list(RIGOL_MODELS.items()))
def test_every_rigol_model_has_a_frequency_limit(model, limit):
    instrument, _ = build_rigol(model=model)
    assert instrument.maximum_frequency == limit


def test_rigol_accepts_an_explicit_frequency_limit():
    """A variant not in the table can still be driven."""
    instrument, _ = build_rigol(max_frequency=25e6)
    assert instrument.maximum_frequency == 25e6
    with pytest.raises(RangeError, match="frequency"):
        instrument.frequency = 30e6


# Waveform and levels


def test_keysight_waveform_commands(keysight):
    instrument, transport = keysight
    instrument.waveform = "square"
    assert transport.last_command == "SOUR1:FUNC SQU"
    instrument.waveform = "pulse"
    assert transport.last_command == "SOUR1:FUNC PULS"


def test_rigol_waveform_commands(rigol):
    instrument, transport = rigol
    instrument.waveform = "square"
    assert transport.last_command == ":SOUR1:FUNC SQUare"


def test_unknown_waveform_lists_the_options(keysight):
    instrument, _ = keysight
    with pytest.raises(RangeError, match="'sine'"):
        instrument.waveform = "sawtooth-ish"


def test_channel_selects_the_source_path(keysight):
    instrument, transport = keysight
    instrument.channel = 2
    instrument.frequency = 1e3
    assert transport.last_command == "SOUR2:FREQ 1000.0"


def test_rigol_channel_selects_the_source_path(rigol):
    instrument, transport = rigol
    instrument.channel = 2
    instrument.frequency = 1e3
    assert transport.last_command == ":SOUR2:FREQ 1000.0"


def test_amplitude_limit_follows_the_output_load():
    """Into an open circuit the generator delivers twice what it does into
    50 ohms, so 15 Vpp is impossible on a terminated output and fine on a
    high-impedance one."""
    terminated, _ = build_keysight({"OUTP1:LOAD?": INTO_50})
    with pytest.raises(RangeError, match="amplitude"):
        terminated.amplitude = 15.0

    high_z, transport = build_keysight({"OUTP1:LOAD?": INTO_HIGH_Z})
    high_z.amplitude = 15.0
    assert transport.last_command == "SOUR1:VOLT 15.0"


def test_rigol_amplitude_limit_follows_the_output_load():
    terminated, _ = build_rigol({":OUTP1:IMP?": INTO_50})
    with pytest.raises(RangeError, match="amplitude"):
        terminated.amplitude = 15.0

    high_z, transport = build_rigol({":OUTP1:IMP?": INTO_HIGH_Z})
    high_z.amplitude = 15.0
    assert transport.last_command == ":SOUR1:VOLT 15.0"


def test_offset_limit_follows_the_output_load():
    terminated, _ = build_keysight({"OUTP1:LOAD?": INTO_50})
    with pytest.raises(RangeError, match="offset"):
        terminated.offset = 8.0

    high_z, transport = build_keysight({"OUTP1:LOAD?": INTO_HIGH_Z})
    high_z.offset = 8.0
    assert transport.last_command == "SOUR1:VOLT:OFFS 8.0"


def test_high_impedance_load_reads_back_as_infinity():
    instrument, _ = build_keysight({"OUTP1:LOAD?": INTO_HIGH_Z})
    assert instrument.load == float("inf")
    assert instrument.load_is_high_impedance() is True


def test_load_accepts_infinity_by_name(keysight):
    instrument, transport = keysight
    instrument.load = "infinity"
    assert transport.last_command == "OUTP1:LOAD INF"
    instrument.load = 50
    assert transport.last_command == "OUTP1:LOAD 50"


def test_rigol_load_uses_the_impedance_command(rigol):
    instrument, transport = rigol
    instrument.load = "high z"
    assert transport.last_command == ":OUTP1:IMP INF"


def test_nonsense_load_is_rejected(keysight):
    instrument, _ = keysight
    with pytest.raises(RangeError, match="output load"):
        instrument.load = "somewhat resistive"


def test_duty_cycle_and_symmetry(keysight):
    instrument, transport = keysight
    instrument.duty_cycle = 25
    assert transport.last_command == "SOUR1:FUNC:SQU:DCYC 25"
    instrument.ramp_symmetry = 100
    assert transport.last_command == "SOUR1:FUNC:RAMP:SYMM 100"
    with pytest.raises(RangeError, match="duty cycle"):
        instrument.duty_cycle = 120


def test_voltage_unit(keysight):
    instrument, transport = keysight
    instrument.voltage_unit = "vrms"
    assert transport.last_command == "SOUR1:VOLT:UNIT VRMS"


# Apply


def test_keysight_apply_sends_one_command(keysight):
    instrument, transport = keysight
    instrument.apply("sine", frequency=1e3, amplitude=0.5, offset=0.0)
    assert transport.writes == ["SOUR1:APPL:SIN 1000.0,0.5,0.0"]


def test_apply_uses_defaults_for_omitted_arguments(keysight):
    instrument, transport = keysight
    instrument.apply("square", frequency=1e3)
    assert transport.writes == ["SOUR1:APPL:SQU 1000.0,DEF,DEF"]


def test_rigol_apply_trims_trailing_defaults(rigol):
    """Sending a trailing DEF adds nothing, so it is left off."""
    instrument, transport = rigol
    instrument.apply("sine", frequency=1e3)
    assert transport.writes == [":SOUR1:APPL:SINusoid 1000.0"]


def test_rigol_apply_with_no_arguments(rigol):
    instrument, transport = rigol
    instrument.apply("noise")
    assert transport.writes == [":SOUR1:APPL:NOISe"]


def test_apply_checks_the_frequency_against_the_model(keysight):
    instrument, _ = build_keysight(identity=KEYSIGHT_1CH)
    with pytest.raises(RangeError, match="frequency"):
        instrument.apply("sine", frequency=50e6)


# Output


def test_output_commands(keysight):
    instrument, transport = keysight
    instrument.output = True
    assert transport.last_command == "OUTP1 1"
    instrument.output = "off"
    assert transport.last_command == "OUTP1 0"


def test_rigol_output_uses_words(rigol):
    instrument, transport = rigol
    instrument.output = True
    assert transport.last_command == ":OUTP1 ON"


def test_polarity(keysight):
    instrument, transport = keysight
    instrument.polarity = "inverted"
    assert transport.last_command == "OUTP1:POL INV"


# Burst and sweep


def test_configure_burst(keysight):
    instrument, transport = keysight
    instrument.configure_burst(cycles=10, mode="triggered", phase=0, period=0.1)
    assert transport.writes == [
        "SOUR1:BURS:MODE TRIG",
        "SOUR1:BURS:NCYC 10",
        "SOUR1:BURS:PHAS 0",
        "SOUR1:BURS:INT:PER 0.1",
        "TRIG1:SOUR IMM",
        "SOUR1:BURS:STAT ON",
    ]


def test_burst_accepts_infinite_cycles(keysight):
    instrument, transport = keysight
    instrument.configure_burst(cycles="infinite")
    assert "SOUR1:BURS:NCYC INF" in transport.writes


def test_configure_sweep(keysight):
    instrument, transport = keysight
    instrument.configure_sweep(1e3, 1e5, duration=2.0, spacing="logarithmic")
    assert transport.writes == [
        "SOUR1:FREQ:STAR 1000.0",
        "SOUR1:FREQ:STOP 100000.0",
        "SOUR1:SWE:SPAC LOG",
        "SOUR1:SWE:TIME 2.0",
        "SOUR1:SWE:HTIM 0.0",
        "SOUR1:SWE:RTIM 0.0",
        "SOUR1:SWE:STAT ON",
    ]


def test_sweep_endpoints_are_checked_against_the_model():
    instrument, _ = build_keysight(identity=KEYSIGHT_1CH)
    with pytest.raises(RangeError, match="sweep stop"):
        instrument.configure_sweep(1e3, 50e6)


def test_rigol_sweep_supports_step_spacing(rigol):
    instrument, transport = rigol
    instrument.configure_sweep(1e3, 1e4, spacing="step")
    assert ":SOUR1:SWE:SPAC STEp" in transport.writes


# Modulation


def test_amplitude_modulation(keysight):
    instrument, transport = keysight
    instrument.configure_amplitude_modulation(depth=50, frequency=1e3, shape="sine")
    assert transport.writes == [
        "SOUR1:AM:SOUR INT",
        "SOUR1:AM:INT:FUNC SIN",
        "SOUR1:AM:INT:FREQ 1000.0",
        "SOUR1:AM:DEPT 50",
        "SOUR1:AM:STAT ON",
    ]


def test_frequency_modulation_deviation_is_bounded(keysight):
    instrument, _ = build_keysight(identity=KEYSIGHT_1CH)
    with pytest.raises(RangeError, match="frequency deviation"):
        instrument.configure_frequency_modulation(deviation=50e6)


def test_disable_modulation_turns_off_every_kind(keysight):
    instrument, transport = keysight
    instrument.disable_modulation()
    assert transport.writes == [
        "SOUR1:AM:STAT OFF",
        "SOUR1:FM:STAT OFF",
        "SOUR1:PM:STAT OFF",
        "SOUR1:PWM:STAT OFF",
    ]


# Stepped sweep and shutdown


def test_sweep_frequency_steps_and_yields(keysight):
    instrument, transport = keysight
    reached = list(instrument.sweep_frequency(1e3, 5e3, points=5))
    assert reached == pytest.approx([1e3, 2e3, 3e3, 4e3, 5e3])
    assert transport.writes[0] == "SOUR1:FREQ 1000.0"


def test_sweep_frequency_is_logarithmic_when_asked(keysight):
    instrument, _ = keysight
    reached = list(
        instrument.sweep_frequency(1e2, 1e5, points=4, spacing="logarithmic")
    )
    assert reached == pytest.approx([1e2, 1e3, 1e4, 1e5])


def test_safe_shutdown_turns_everything_off(keysight):
    instrument, transport = keysight
    instrument.safe_shutdown()
    assert transport.writes[0] == "OUTP1 0"
    assert "SOUR1:BURS:STAT 0" in transport.writes
    assert "SOUR1:SWE:STAT 0" in transport.writes


def test_amplitude_is_not_bounded_in_volts_when_the_unit_is_not_volts():
    # The limits are peak-to-peak volts. Applied while the generator is set to
    # dBm they would refuse every ordinary level, because -10 dBm is a
    # perfectly normal output and reads as a negative voltage.
    generator, transport = build_keysight({"SOUR1:VOLT:UNIT?": "DBM"})
    generator.amplitude = -10
    assert transport.last_command == "SOUR1:VOLT -10"


def test_the_rigol_amplitude_is_not_bounded_in_volts_either():
    generator, transport = build_rigol({":SOUR1:VOLT:UNIT?": "DBM"})
    generator.amplitude = -10
    assert transport.last_command == ":SOUR1:VOLT -10"
