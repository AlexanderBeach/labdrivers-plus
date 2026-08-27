"""Tests for the rotation probe driver and its WJ_API binding.

The vendor DLL is replaced with a fake that records every call it receives, so
these tests assert the exact controller calls a driver makes. That is the same
role the recording-transport tests play for the SCPI instruments.
"""

import ctypes

import pytest

from labdrivers.funky_rotator import rotator as rotator_module
from labdrivers.funky_rotator import wj_api as wj_api_module
from labdrivers.funky_rotator.rotator import (
    MAXIMUM_ANGLE,
    MINIMUM_ANGLE,
    PULSES_PER_REVOLUTION,
    VENDOR_PULSES_PER_REVOLUTION,
    Rotator,
)

# Positions are quantized to whole stepper pulses, so an arbitrary angle
# cannot be hit exactly. The manual specifies resolution 'better than
# 0.01 deg', and one pulse is about 0.0072 deg.
ONE_PULSE_DEGREES = 360.0 / PULSES_PER_REVOLUTION

# Every function the binding expects to find in the DLL.
EXPORTS = {
    "WJ_Open",
    "WJ_Close",
    "WJ_Get_Axis_Acc",
    "WJ_Get_Axis_Dec",
    "WJ_Get_Axis_Vel",
    "WJ_Get_Axis_Subdivision",
    "WJ_Get_Axis_Status",
    "WJ_Get_Axes_Status",
    "WJ_Get_Axis_Pulses",
    "WJ_Get_Axes_Pulses",
    "WJ_Get_Axes_Num",
    "WJ_Move_Axis_Pulses",
    "WJ_Move_Axes_Pulses",
    "WJ_Move_Axis_Vel",
    "WJ_Move_Axes_Vel",
    "WJ_Move_Axis_Emergency_Stop",
    "WJ_Move_Axis_Slow_Stop",
    "WJ_Move_Axis_Home",
    "WJ_Set_Axis_Acc",
    "WJ_Set_Axis_Dec",
    "WJ_Set_Axis_Vel",
    "WJ_Set_Axis_Subdivision",
    "WJ_Set_Axis_Slow_Stop",
    "WJ_Set_Led_Twinkle",
    "WJ_Set_Axis_Pulses_Zero",
    "WJ_Set_Default",
    "WJ_Set_Move_Axis_Vel_Acc",
    "WJ_Set_Axis_Home_Pulses",
    "WJ_IO_Output",
    "WJ_IO_Input",
}


class FakeFunction:
    """Stands in for a ctypes function pointer bound out of the DLL."""

    def __init__(self, dll, name):
        self._dll = dll
        self._name = name
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._dll.dispatch(self._name, args)


class FakeDll:
    """A minimal simulation of WJ_API.dll that records every call."""

    def __init__(self, axis_count=4, missing=()):
        self.calls = []
        self.axis_count = axis_count
        self.missing = set(missing)
        self.pulses = 0
        self.status = 0
        self.velocity = 1
        self.acceleration = 0
        self.deceleration = 0
        self.subdivision = 0

    def __getattr__(self, name):
        if name not in EXPORTS or name in self.missing:
            raise AttributeError(name)
        return FakeFunction(self, name)

    @staticmethod
    def _write(argument, value):
        """Write an output value back through a ctypes.byref() argument."""
        argument._obj.value = value

    def dispatch(self, name, args):
        self.calls.append((name, args))

        if name == "WJ_Get_Axes_Num":
            self._write(args[-1], self.axis_count)
        elif name == "WJ_Get_Axis_Pulses":
            self._write(args[-1], self.pulses)
        elif name == "WJ_Get_Axis_Status":
            self._write(args[-1], self.status)
        elif name == "WJ_Get_Axis_Vel":
            self._write(args[-1], self.velocity)
        elif name == "WJ_Get_Axis_Acc":
            self._write(args[-1], self.acceleration)
        elif name == "WJ_Get_Axis_Dec":
            self._write(args[-1], self.deceleration)
        elif name == "WJ_Get_Axis_Subdivision":
            self._write(args[-1], self.subdivision)
        elif name in ("WJ_Get_Axes_Pulses", "WJ_Get_Axes_Status"):
            for index in range(self.axis_count):
                args[0][index] = self.pulses if "Pulses" in name else self.status
        elif name == "WJ_Move_Axis_Pulses":
            # The real controller moves, but the fake jumps straight to the target.
            self.pulses += args[1]
        elif name == "WJ_Set_Axis_Vel":
            self.velocity = args[1]
        elif name == "WJ_Set_Axis_Pulses_Zero":
            self.pulses = 0
        return 0

    def calls_to(self, name):
        return [args for called, args in self.calls if called == name]


@pytest.fixture
def dll(monkeypatch):
    """Install a FakeDll in place of the real vendor library."""
    fake = FakeDll()
    monkeypatch.setattr(ctypes, "WinDLL", lambda path: fake, raising=False)
    monkeypatch.setattr(wj_api_module.os.path, "isfile", lambda path: True)
    return fake


@pytest.fixture
def probe(dll):
    return Rotator()


# Binding


def test_binding_covers_every_documented_function(dll):
    """Every function declared by WJ_API.h must be reachable from Python.

    Drives all 30 wrappers and checks that the set of DLL functions actually
    dispatched is exactly the set the vendor header declares. That catches both a
    missing wrapper and a wrapper wired to the wrong DLL function.
    """
    api = wj_api_module.WJApi()
    axes = [0, 0, 0, 0]
    dll.calls.clear()

    api.open(0)
    api.close()
    api.get_axis_acceleration(1)
    api.get_axis_deceleration(1)
    api.get_axis_velocity(1)
    api.get_axis_subdivision(1)
    api.get_axis_status(1)
    api.get_axes_status()
    api.get_axis_pulses(1)
    api.get_axes_pulses()
    api.get_axes_number()
    api.move_axis_pulses(1, 10)
    api.move_axes_pulses(axes)
    api.move_axis_velocity(1, 1)
    api.move_axes_velocity(axes)
    api.emergency_stop(1)
    api.slow_stop(1)
    api.move_axis_home(1, 0)
    api.set_axis_acceleration(1, 1)
    api.set_axis_deceleration(1, 1)
    api.set_axis_velocity(1, 1)
    api.set_axis_subdivision(1, 1)
    api.set_axis_slow_stop(1, 1)
    api.set_led_twinkle()
    api.set_axis_pulses_zero(1)
    api.set_default()
    api.set_move_axis_velocity_acceleration(1, 1)
    api.set_axis_home_pulses(1, 0)
    api.io_output(1, 1)
    api.io_input(1)

    assert {name for name, _ in dll.calls} == EXPORTS
    assert len(EXPORTS) == 30


def test_missing_exports_are_reported_clearly(monkeypatch):
    fake = FakeDll(missing={"WJ_Set_Default", "WJ_IO_Input"})
    monkeypatch.setattr(ctypes, "WinDLL", lambda path: fake, raising=False)
    monkeypatch.setattr(wj_api_module.os.path, "isfile", lambda path: True)
    with pytest.raises(RuntimeError, match="missing 2 of the 30 expected"):
        wj_api_module.WJApi()


def test_missing_dll_names_the_path():
    with pytest.raises(RuntimeError, match="nowhere.dll"):
        wj_api_module.WJApi(dll_path="nowhere.dll")


def test_axis_buffers_follow_the_controller_axis_count(monkeypatch):
    """An eight-axis board must get eight-element buffers."""
    fake = FakeDll(axis_count=8)
    monkeypatch.setattr(ctypes, "WinDLL", lambda path: fake, raising=False)
    monkeypatch.setattr(wj_api_module.os.path, "isfile", lambda path: True)
    api = wj_api_module.WJApi()
    assert api.axis_count == 8
    assert len(api.get_axes_pulses()) == 8


def test_strict_mode_raises_on_nonzero_return(dll, monkeypatch):
    api = wj_api_module.WJApi(strict=True)
    monkeypatch.setattr(dll, "dispatch", lambda name, args: -1)
    with pytest.raises(wj_api_module.WJApiError):
        api.set_led_twinkle()


# Connection


def test_opens_after_closing_a_stale_handle(dll):
    Rotator()
    names = [name for name, _ in dll.calls]
    assert names.index("WJ_Close") < names.index("WJ_Open")


def test_context_manager_closes(dll):
    with Rotator():
        pass
    assert dll.calls_to("WJ_Close")


# Position and motion


def test_angle_converts_pulses_to_degrees(probe, dll):
    dll.pulses = 25002  # half a revolution at 50004 pulses/rev
    assert probe.angle == pytest.approx(180.0)


def test_move_to_is_absolute(probe, dll):
    """move_to must command the difference between target and current position."""
    dll.pulses = probe._degrees_to_pulses(30.0)
    probe.move_to(90.0)
    assert dll.calls_to("WJ_Move_Axis_Pulses")[-1][1] == probe._degrees_to_pulses(60.0)
    assert probe.angle == pytest.approx(90.0)


def test_move_by_is_relative(probe, dll):
    """move_by must command exactly the requested change, from any position."""
    dll.pulses = probe._degrees_to_pulses(30.0)
    probe.move_by(45.0)
    assert dll.calls_to("WJ_Move_Axis_Pulses")[-1][1] == probe._degrees_to_pulses(45.0)
    assert probe.angle == pytest.approx(75.0, abs=ONE_PULSE_DEGREES)


def test_repeated_absolute_moves_do_not_drift(probe, dll):
    """Each move_to must re-read position rather than trust a cached buffer."""
    for target in (10.0, 200.0, 45.0, 360.0, 0.0):
        probe.move_to(target)
        assert probe.angle == pytest.approx(target, abs=ONE_PULSE_DEGREES)


@pytest.mark.parametrize("angle", [MINIMUM_ANGLE, 0.0, 180.0, MAXIMUM_ANGLE])
def test_angles_within_travel_are_accepted(probe, angle):
    probe.move_to(angle)


@pytest.mark.parametrize("angle", [MINIMUM_ANGLE - 0.1, MAXIMUM_ANGLE + 0.1, 400.0])
def test_angles_outside_travel_are_rejected(probe, dll, angle):
    before = len(dll.calls_to("WJ_Move_Axis_Pulses"))
    with pytest.raises(RuntimeError, match="must be between"):
        probe.move_to(angle)
    assert len(dll.calls_to("WJ_Move_Axis_Pulses")) == before, "must not move"


def test_relative_move_past_the_limit_is_rejected(probe, dll):
    dll.pulses = probe._degrees_to_pulses(350.0)
    with pytest.raises(RuntimeError, match="must be between"):
        probe.move_by(50.0)


def test_wait_times_out_if_the_probe_never_stops(probe, dll):
    dll.status = 1  # permanently "moving"
    with pytest.raises(RuntimeError, match="still moving"):
        probe.wait_while_moving(poll_interval=0.001, timeout=0.05)


def test_stop_and_emergency_stop_use_different_calls(probe, dll):
    probe.stop()
    probe.emergency_stop()
    assert dll.calls_to("WJ_Move_Axis_Slow_Stop")
    assert dll.calls_to("WJ_Move_Axis_Emergency_Stop")


def test_define_zero_resets_the_angle(probe, dll):
    dll.pulses = probe._degrees_to_pulses(123.0)
    probe.define_zero()
    assert probe.angle == pytest.approx(0.0)


# Settings


def test_speed_round_trips(probe):
    probe.speed = 3
    assert probe.speed == 3


@pytest.mark.parametrize("bad_speed", [0, -1])
def test_speed_below_one_is_rejected(probe, bad_speed):
    with pytest.raises(RuntimeError, match="1 or greater"):
        probe.speed = bad_speed


def test_pulses_per_revolution_is_overridable(dll):
    """The 50004-vs-50000 calibration must be settable without editing code."""
    probe = Rotator(pulses_per_revolution=VENDOR_PULSES_PER_REVOLUTION)
    dll.pulses = VENDOR_PULSES_PER_REVOLUTION
    assert probe.angle == pytest.approx(360.0)


def test_non_positive_pulses_per_revolution_is_rejected(dll):
    with pytest.raises(RuntimeError, match="positive number"):
        Rotator(pulses_per_revolution=0)


def test_bad_axis_is_rejected(dll):
    with pytest.raises(RuntimeError, match="axis must be an integer"):
        Rotator(axis=9)


def test_vendor_limits_match_the_manual():
    assert (MINIMUM_ANGLE, MAXIMUM_ANGLE) == (-5.0, 365.0)
    # The driver is calibrated against 50004, but the vendor quotes 50000.
    assert rotator_module.PULSES_PER_REVOLUTION == 50004
    assert rotator_module.VENDOR_PULSES_PER_REVOLUTION == 50000
