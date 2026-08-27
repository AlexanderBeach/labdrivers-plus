"""Driver for the Oxford Instruments Mercury iPS magnet power supply.

The iPS drives one magnet per axis. Each axis is a :class:`Magnet` with its own
setpoints, ramp rates and switch heater, and :class:`MercuryIps` groups the
axes a system actually has.

Field limits belong to the magnet, not to the power supply, so they are given
per axis at construction. The defaults suit a three-axis vector magnet with a
6 T solenoid and 1 T split coils. Pass ``field_limits`` for any other system.

Commands are transcribed from the *Mercury iPS Operator's Manual* (issue 20).
"""

import math
import time

from ..core import check_boolean, check_choice, check_range
from ..core.errors import RangeError
from ..core.sweep import round_trip, sweep_values
from .mercury import MercuryInstrument

AXES = ("GRPX", "GRPY", "GRPZ")

# Field limits in tesla per axis. Override with field_limits= for a different
# magnet. These are only a sensible starting point.
DEFAULT_FIELD_LIMITS = {"GRPX": 1.0, "GRPY": 1.0, "GRPZ": 6.0}

# Largest current a Mercury iPS output will deliver, in amps.
DEFAULT_CURRENT_LIMIT = 130.0

ACTIONS = {
    "hold": "HOLD",
    "ramp to setpoint": "RTOS",
    "ramp to zero": "RTOZ",
    "clamp": "CLMP",
}


class Magnet:
    """One axis of a Mercury iPS.

    :param instrument: The MercuryIps this axis belongs to.
    :param axis: 'GRPX', 'GRPY' or 'GRPZ'.
    :param field_limit: Largest field this magnet may be asked for, in tesla.
    :param current_limit: Largest current this magnet may be asked for, in amps.
    """

    def __init__(
        self, instrument, axis, field_limit=None, current_limit=DEFAULT_CURRENT_LIMIT
    ):
        axis = str(axis).upper().strip()
        if axis not in AXES:
            raise RangeError(
                f"The magnet axis can be {', '.join(AXES)}, but got {axis!r}."
            )
        self._instrument = instrument
        self.axis = axis
        self.field_limit = float(
            DEFAULT_FIELD_LIMITS[axis] if field_limit is None else field_limit
        )
        self.current_limit = float(current_limit)

    def _noun(self, tail):
        return f"DEV:{self.axis}:PSU:{tail}"

    # Field

    @property
    def field(self):
        """Returns the magnetic field at the magnet, in tesla."""
        return self._instrument.read_value(self._noun("SIG:FLD"), "T")

    @property
    def persistent_field(self):
        """Returns the field held by the magnet in persistent mode, in tesla."""
        return self._instrument.read_value(self._noun("SIG:PFLD"), "T")

    @property
    def field_setpoint(self):
        """Returns the field the magnet is being ramped towards, in tesla."""
        return self._instrument.read_value(self._noun("SIG:FSET"), "T")

    @field_setpoint.setter
    def field_setpoint(self, value):
        check_range(
            value,
            -self.field_limit,
            self.field_limit,
            f"{self.axis} field setpoint",
            " T",
        )
        self._instrument.set_noun(self._noun("SIG:FSET"), value)

    @property
    def field_ramp_rate(self):
        """Returns the rate the field ramps at, in tesla per minute."""
        return self._instrument.read_value(self._noun("SIG:RFST"), "T/m")

    @field_ramp_rate.setter
    def field_ramp_rate(self, value):
        check_range(value, 0, self.field_limit, "field ramp rate", " T/min")
        self._instrument.set_noun(self._noun("SIG:RFST"), value)

    # Current

    @property
    def current(self):
        """Returns the current through the magnet, in amps."""
        return self._instrument.read_value(self._noun("SIG:CURR"), "A")

    @property
    def current_setpoint(self):
        """Returns the current the magnet is being ramped towards, in amps."""
        return self._instrument.read_value(self._noun("SIG:CSET"), "A")

    @current_setpoint.setter
    def current_setpoint(self, value):
        check_range(
            value,
            -self.current_limit,
            self.current_limit,
            f"{self.axis} current setpoint",
            " A",
        )
        self._instrument.set_noun(self._noun("SIG:CSET"), value)

    @property
    def current_ramp_rate(self):
        """Returns the rate the current ramps at, in amps per minute."""
        return self._instrument.read_value(self._noun("SIG:RCST"), "A/m")

    @current_ramp_rate.setter
    def current_ramp_rate(self, value):
        check_range(value, 0, self.current_limit, "current ramp rate", " A/min")
        self._instrument.set_noun(self._noun("SIG:RCST"), value)

    @property
    def voltage(self):
        """Returns the voltage across the magnet leads, in volts."""
        return self._instrument.read_value(self._noun("SIG:VOLT"), "V")

    # Switch heater

    @property
    def switch_heater(self):
        """Returns whether the persistent-mode switch heater is on."""
        reply = self._instrument.read_noun(self._noun("SIG:SWHT"))
        return self._instrument.parse_word(reply, "SWHT") == "ON"

    @switch_heater.setter
    def switch_heater(self, value):
        state = check_boolean(value, "switch heater")
        self._instrument.set_noun(self._noun("SIG:SWHT"), "ON" if state else "OFF")

    # Actions and state

    @property
    def action(self):
        """Returns what the supply is doing: 'hold', 'ramp to setpoint', 'ramp to
        zero' or 'clamp'."""
        reply = self._instrument.read_noun(self._noun("ACTN"))
        code = self._instrument.parse_word(reply, "ACTN")
        for name, action in ACTIONS.items():
            if code == action:
                return name
        return code.lower()

    @action.setter
    def action(self, value):
        code = check_choice(value, ACTIONS, "magnet action")
        self._instrument.set_noun(self._noun("ACTN"), code)

    def hold(self):
        """Stop any ramp and hold the present field."""
        self.action = "hold"

    def ramp_to_setpoint(self):
        """Ramp towards the field setpoint."""
        self.action = "ramp to setpoint"

    def ramp_to_zero(self):
        """Ramp the field down to zero."""
        self.action = "ramp to zero"

    def clamp(self):
        """Clamp the output."""
        self.action = "clamp"

    def ramping(self):
        """Whether the magnet is ramping, in either direction."""
        return self.action in ("ramp to setpoint", "ramp to zero")

    def holding(self):
        """Whether the magnet is holding its field."""
        return self.action == "hold"

    def clamped(self):
        """Whether the magnet output is clamped."""
        return self.action == "clamp"

    def wait_for_field(self, timeout=3600.0, interval=5.0):
        """Block until the magnet stops ramping.

        :param timeout: Seconds to wait before giving up.
        :param interval: Seconds between checks.
        """
        self._instrument.wait_until(
            lambda: not self.ramping(),
            timeout=timeout,
            interval=interval,
            description=f"the {self.axis} magnet to finish ramping",
        )

    def ramp_to_field(self, field, wait=True, timeout=3600.0):
        """Set the field setpoint and ramp to it.

        :param field: Target field in tesla.
        :param wait: Block until the ramp finishes.
        """
        self.field_setpoint = field
        self.ramp_to_setpoint()
        if wait:
            self.wait_for_field(timeout=timeout)

    def sweep_field(
        self, start, stop, points=None, step=None, settle=0.0, return_to_start=False
    ):
        """Ramp through a series of fields, yielding once the magnet arrives.

        The loop body runs with the magnet holding at each field, which is what
        a field sweep in a transport measurement wants.

            for field in supply.z.sweep_field(-1, 1, points=41):
                x, y = lockin.measure()

        :param start: First field, in tesla.
        :param stop: Last field, in tesla.
        :param points: Number of fields, including both ends.
        :param step: Spacing between fields, as an alternative to points.
        :param settle: Extra seconds to wait after each ramp finishes.
        :param return_to_start: Sweep back down again, for hysteresis.
        :yield: The field actually reached, in tesla.
        """
        fields = sweep_values(start, stop, points=points, step=step)
        if return_to_start:
            fields = round_trip(fields)

        for field in fields:
            self.ramp_to_field(field, wait=True)
            if settle:
                time.sleep(settle)
            yield self.field

    def __repr__(self):
        return f"Magnet({self.axis!r}, field_limit={self.field_limit})"


class MercuryIps(MercuryInstrument):
    """Interface to a Mercury iPS magnet power supply.

        supply = MercuryIps(ip_address="192.168.0.10")
        supply.z.ramp_to_field(1.0)
        print(supply.z.field)

    :param axes: Which axes this system has. Defaults to all three.
    :param field_limits: Field limit in tesla per axis, e.g. {'GRPZ': 9.0}.
                         Any axis not named keeps its default.
    """

    def __init__(self, *args, axes=AXES, field_limits=None, **kwargs):
        super().__init__(*args, **kwargs)
        limits = dict(DEFAULT_FIELD_LIMITS)
        limits.update(
            {str(axis).upper(): value for axis, value in (field_limits or {}).items()}
        )
        self.magnets = {
            str(axis).upper(): Magnet(
                self, axis, field_limit=limits.get(str(axis).upper())
            )
            for axis in axes
        }

    def _magnet(self, axis):
        try:
            return self.magnets[str(axis).upper()]
        except KeyError:
            raise RangeError(
                f"This Mercury iPS has no {axis} axis. It has "
                f"{', '.join(sorted(self.magnets))}."
            )

    @property
    def x(self):
        """Returns the x-axis magnet."""
        return self._magnet("GRPX")

    @property
    def y(self):
        """Returns the y-axis magnet."""
        return self._magnet("GRPY")

    @property
    def z(self):
        """Returns the z-axis magnet."""
        return self._magnet("GRPZ")

    @property
    def magnet_temperature(self):
        """Returns the temperature of the magnet, in kelvin."""
        return self.read_value("DEV:MB1.T1:TEMP:SIG:TEMP", "K")

    def hold_all(self):
        """Hold every axis."""
        for magnet in self.magnets.values():
            magnet.hold()

    def ramp_all_to_zero(self, wait=True, timeout=3600.0):
        """Ramp every axis down to zero field."""
        for magnet in self.magnets.values():
            magnet.ramp_to_zero()
        if wait:
            for magnet in self.magnets.values():
                magnet.wait_for_field(timeout=timeout)

    def vector_field(self):
        """Present field on every axis, as a dict keyed by axis name."""
        return {axis: magnet.field for axis, magnet in self.magnets.items()}

    def circle_sweep(self, radius, points, plane="xy"):
        """Generate the field vectors for a circular sweep in one plane.

        Returns the points rather than driving them, so a measurement loop can
        decide what to do at each one.

        :param radius: Field magnitude in tesla.
        :param points: How many points around the circle.
        :param plane: Which two axes to rotate in: 'xy', 'xz' or 'yz'.
        :return: A list of dicts mapping axis name to field.
        """
        plane = str(plane).lower().strip()
        pairs = {"xy": ("GRPX", "GRPY"), "xz": ("GRPX", "GRPZ"), "yz": ("GRPY", "GRPZ")}
        if plane not in pairs:
            raise RangeError(
                f"The sweep plane can be {', '.join(sorted(pairs))}, but got "
                f"{plane!r}."
            )
        first, second = pairs[plane]
        for axis in (first, second):
            magnet = self._magnet(axis)
            check_range(
                radius,
                -magnet.field_limit,
                magnet.field_limit,
                f"{axis} field",
                " T",
            )
        if int(points) < 1:
            raise RangeError(
                f"A circle sweep needs at least 1 point, but got {points}."
            )

        vectors = []
        for step in range(int(points)):
            angle = 2 * math.pi * step / int(points)
            vectors.append(
                {first: radius * math.cos(angle), second: radius * math.sin(angle)}
            )
        return vectors

    def safe_shutdown(self, timeout=3600.0):
        """Ramp every axis to zero and leave the supply holding.

        The state to leave a magnet in when walking away from it.
        """
        self.ramp_all_to_zero(wait=True, timeout=timeout)
        self.hold_all()

    def __repr__(self):
        return f"MercuryIps({self._transport!r}, axes={sorted(self.magnets)})"
