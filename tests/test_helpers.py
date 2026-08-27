"""Tests for the sweep helpers and the per-instrument convenience methods."""

import pytest

from labdrivers.core import RangeError, RecordingTransport, round_trip, sweep_values
from labdrivers.keithley.keithley2182 import Keithley2182
from labdrivers.keithley.keithley2400 import Keithley2400
from labdrivers.keithley.keithley6221 import Keithley6221
from labdrivers.lakeshore.ls332 import Ls332
from labdrivers.oxford.mercuryitc import MercuryItc
from labdrivers.oxford.triton200 import Triton200
from labdrivers.srs.sr830 import Sr830

IDENTITY_2400 = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Make every sleep in the package return immediately."""
    for module in (
        "labdrivers.keithley.keithley2400",
        "labdrivers.keithley.keithley2182",
        "labdrivers.keithley.keithley6221",
        "labdrivers.srs.sr830",
        "labdrivers.funky_rotator.rotator",
        "labdrivers.ni.nidaq",
    ):
        monkeypatch.setattr(f"{module}.time.sleep", lambda seconds: None)


# Sweep values


def test_linear_sweep_includes_both_ends():
    assert sweep_values(0, 1, points=5) == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_sweep_by_step_matches_sweep_by_points():
    assert sweep_values(0, 1, step=0.25) == pytest.approx(sweep_values(0, 1, points=5))


def test_a_descending_sweep_takes_its_direction_from_the_ends():
    """The sign of the step is ignored, so start and stop decide the direction."""
    assert sweep_values(1, 0, step=0.5) == pytest.approx([1.0, 0.5, 0.0])
    assert sweep_values(1, 0, step=-0.5) == pytest.approx([1.0, 0.5, 0.0])


def test_a_step_that_does_not_divide_evenly_still_reaches_the_stop():
    """A sweep must never quietly end short of where it was asked to go."""
    values = sweep_values(0, 1, step=0.3)
    assert values[-1] == pytest.approx(1.0)
    assert values[:4] == pytest.approx([0.0, 0.3, 0.6, 0.9])


def test_logarithmic_sweep_is_evenly_spaced_in_ratio():
    values = sweep_values(1, 1000, points=4, spacing="logarithmic")
    assert values == pytest.approx([1.0, 10.0, 100.0, 1000.0])


def test_logarithmic_sweep_rejects_a_sign_change():
    with pytest.raises(RangeError, match="same sign"):
        sweep_values(-1, 1, points=5, spacing="logarithmic")


def test_logarithmic_sweep_rejects_zero():
    with pytest.raises(RangeError, match="nonzero"):
        sweep_values(0, 10, points=5, spacing="logarithmic")


def test_sweep_needs_exactly_one_of_points_or_step():
    with pytest.raises(RangeError, match="either points= or step="):
        sweep_values(0, 1)
    with pytest.raises(RangeError, match="either points= or step="):
        sweep_values(0, 1, points=5, step=0.1)


def test_zero_step_is_rejected():
    with pytest.raises(RangeError, match="cannot be zero"):
        sweep_values(0, 1, step=0)


def test_a_single_point_is_not_a_sweep():
    with pytest.raises(RangeError, match="at least 2 points"):
        sweep_values(0, 1, points=1)


def test_round_trip_does_not_repeat_the_turning_point():
    assert round_trip([0, 1, 2]) == [0, 1, 2, 1, 0]


# Keithley 2400


def build_2400(responses=None):
    replies = {"*IDN?": IDENTITY_2400, ":SOUR:FUNC:MODE?": "VOLT"}
    replies.update(responses or {})
    transport = RecordingTransport(replies)
    instrument = Keithley2400(transport=transport)
    transport.clear()
    return instrument, transport


def test_configure_source_sets_compliance_on_the_other_quantity():
    """A voltage source is limited in current, and the other way round."""
    instrument, transport = build_2400()
    instrument.configure_source("voltage", level=0.5, compliance=1e-3)
    assert ":SENS:CURR:PROT:LEV 0.001" in transport.writes
    assert ":SOUR:VOLT:LEV 0.5" in transport.writes


def test_configure_source_as_a_current_source():
    instrument, transport = build_2400({":SOUR:FUNC:MODE?": "CURR"})
    instrument.configure_source("current", level=1e-6, compliance=10)
    assert ":SENS:VOLT:PROT:LEV 10" in transport.writes


def test_sweep_source_visits_every_level_and_yields_readings():
    instrument, transport = build_2400({":READ?": "0.5,1e-6"})
    results = list(instrument.sweep_source(0, 1, points=5))
    levels = [level for level, _, _ in results]
    assert levels == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
    assert all((voltage, current) == (0.5, 1e-6) for _, voltage, current in results)
    assert transport.writes.count(":READ?") == 0, ":READ? is a query, not a write"


def test_sweep_source_can_return_to_its_start():
    instrument, _ = build_2400({":READ?": "0,0"})
    levels = [
        level
        for level, _, _ in instrument.sweep_source(0, 1, points=3, return_to_start=True)
    ]
    assert levels == pytest.approx([0.0, 0.5, 1.0, 0.5, 0.0])


def test_sweep_source_is_lazy():
    """Nothing is sent until the caller starts iterating."""
    instrument, transport = build_2400({":READ?": "0,0"})
    sweep = instrument.sweep_source(0, 1, points=5)
    assert transport.commands == []
    next(sweep)
    assert transport.commands


def test_safe_shutdown_ramps_before_switching_off():
    instrument, transport = build_2400({":OUTP:STAT?": "1", ":SOUR:VOLT:LEV?": "1.0"})
    instrument.safe_shutdown(steps=4, delay=0)
    levels = [c for c in transport.writes if c.startswith(":SOUR:VOLT:LEV ")]
    assert levels, "the source must be walked down"
    assert float(levels[-1].split()[-1]) == pytest.approx(0.0)
    assert transport.writes[-1] == ":OUTP:STAT 0"


def test_safe_shutdown_on_an_already_off_output_does_not_ramp():
    instrument, transport = build_2400({":OUTP:STAT?": "0"})
    instrument.safe_shutdown()
    assert transport.writes == [":OUTP:STAT 0"]


# Keithley 6221


def test_6221_safe_shutdown_stops_the_waveform_first():
    transport = RecordingTransport({"OUTP:STAT?": "1", "SOUR:CURR?": "1e-3"})
    instrument = Keithley6221(transport=transport)
    transport.clear()
    instrument.safe_shutdown(steps=4, delay=0)
    assert transport.writes[0] == "SOUR:WAVE:ABOR"
    assert transport.writes[-1] == "OUTP:STAT 0"


# Keithley 2182


def test_read_average_returns_the_mean_and_its_uncertainty():
    readings = iter(["1.0", "2.0", "3.0", "4.0"])
    transport = RecordingTransport(responses=lambda command: next(readings))
    meter = Keithley2182(transport=transport)
    average, error = meter.read_average(count=4)
    assert average == pytest.approx(2.5)
    assert error == pytest.approx(0.6454972, rel=1e-4)


def test_read_average_of_one_reading_has_no_scatter():
    meter = Keithley2182(transport=RecordingTransport({":READ?": "1.5"}))
    assert meter.read_average(count=1) == (1.5, 0.0)


# SR830


def test_settling_time_scales_with_the_filter_order():
    """Each 6 dB/octave is another pole, and each pole settles in its own time."""
    single = Sr830(
        transport=RecordingTransport({"OFLT?": "10", "OFSL?": "0"}), interface=None
    )
    quadruple = Sr830(
        transport=RecordingTransport({"OFLT?": "10", "OFSL?": "3"}), interface=None
    )
    assert single.settling_time(5) == pytest.approx(5.0)
    assert quadruple.settling_time(5) == pytest.approx(20.0)


def test_measure_settles_then_snapshots():
    transport = RecordingTransport({"OFLT?": "0", "OFSL?": "0", "SNAP? 1,2": "1.0,2.0"})
    lockin = Sr830(transport=transport, interface=None)
    assert lockin.measure() == (1.0, 2.0)
    assert transport.queries[-1] == "SNAP? 1,2"


def test_measure_average_reports_the_scatter():
    values = iter(["1.0,10.0", "2.0,20.0", "3.0,30.0"])

    def respond(command):
        if command.startswith("SNAP"):
            return next(values)
        return "0"

    lockin = Sr830(transport=RecordingTransport(responses=respond), interface=None)
    mean_x, mean_y, error_x, error_y = lockin.measure_average(count=3, interval=0)
    assert (mean_x, mean_y) == pytest.approx((2.0, 20.0))
    assert error_x == pytest.approx(0.57735, rel=1e-4)
    assert error_y == pytest.approx(5.7735, rel=1e-4)


def test_ramp_amplitude_walks_to_the_target():
    transport = RecordingTransport({"SLVL?": "0.004"})
    lockin = Sr830(transport=transport, interface=None)
    lockin.ramp_amplitude(1.0, steps=5, delay=0)
    levels = [float(c.split()[-1]) for c in transport.writes if c.startswith("SLVL ")]
    assert len(levels) == 5
    assert levels[-1] == pytest.approx(1.0)
    assert levels == sorted(levels)


def test_ramp_amplitude_respects_the_output_range():
    lockin = Sr830(transport=RecordingTransport({"SLVL?": "1.0"}), interface=None)
    with pytest.raises(RangeError, match="sine output amplitude"):
        lockin.ramp_amplitude(10.0)


# Temperature sweeps


def test_itc_sweep_sets_each_setpoint_in_turn(monkeypatch):
    itc = MercuryItc(transport=RecordingTransport())
    applied = []
    monkeypatch.setattr(
        MercuryItc, "setpoint", lambda self, sensor, value=None: applied.append(value)
    )
    monkeypatch.setattr(
        MercuryItc,
        "wait_for_temperature",
        lambda self, sensor, target=None, **kwargs: target,
    )
    reached = list(itc.sweep_temperature("vti", 2, 10, points=5))
    assert applied == pytest.approx([2.0, 4.0, 6.0, 8.0, 10.0])
    assert reached == pytest.approx(applied)


def test_triton_sweep_sets_each_setpoint_in_turn(monkeypatch):
    fridge = Triton200(transport=RecordingTransport())
    applied = []
    monkeypatch.setattr(
        Triton200,
        "temperature_setpoint",
        property(lambda self: 0.0, lambda self, value: applied.append(value)),
    )
    monkeypatch.setattr(
        Triton200, "wait_for_temperature", lambda self, target=None, **kwargs: target
    )
    reached = list(fridge.sweep_temperature(0.02, 0.10, points=5))
    assert applied == pytest.approx([0.02, 0.04, 0.06, 0.08, 0.10])
    assert reached == pytest.approx(applied)


def test_lakeshore_ramp_to_sets_the_rate_then_the_setpoint():
    transport = RecordingTransport({"RAMP? 1": "1,5.0"})
    controller = Ls332(transport=transport)
    controller.ramp_to(4.2, rate=2.0, wait=False)
    assert "RAMP 1,1,2.0" in transport.writes
    assert "SETP 1,4.2" in transport.writes


def test_lakeshore_ramp_to_without_a_rate_leaves_it_alone():
    transport = RecordingTransport()
    controller = Ls332(transport=transport)
    controller.ramp_to(4.2, wait=False)
    assert transport.writes == ["SETP 1,4.2"]
