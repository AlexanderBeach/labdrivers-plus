"""Driver for the Oxford Instruments Mercury iTC temperature controller.

The iTC addresses each sensor, heater and valve by a board identifier such as
``MB1.T1`` or ``DB8.T1``. Which identifier corresponds to which physical
location is a property of how a particular cryostat was wired, not of the
instrument, so the map is a constructor argument. The defaults match a
Teslatron-style system with the VTI on the motherboard sensor and the probe on
daughter board 8. Pass ``sensors`` for any other cryostat.

Commands are transcribed from the *Mercury iTC Operator's Manual* (issue 18).
"""

import time

from ..core import check_boolean, check_range
from ..core.errors import InstrumentTimeoutError, RangeError
from ..core.sweep import sweep_values
from .mercury import MercuryInstrument

# Board identifiers for a typical Teslatron-style fit. Override with sensors=.
DEFAULT_SENSORS = {"vti": "MB1.T1", "probe": "DB8.T1"}

# Board identifier of the needle valve / pressure loop.
DEFAULT_PRESSURE_BOARD = "DB5.P1"


class MercuryItc(MercuryInstrument):
    """Interface to a Mercury iTC temperature controller.

        itc = MercuryItc(ip_address="192.168.0.11")
        itc.setpoint("vti", 4.2)
        itc.wait_for_temperature("vti", 4.2)

    :param sensors: Map of friendly name to board identifier, e.g.
                    {'vti': 'MB1.T1', 'probe': 'DB8.T1'}.
    :param pressure_board: Board identifier of the needle valve loop.
    """

    def __init__(
        self, *args, sensors=None, pressure_board=DEFAULT_PRESSURE_BOARD, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.sensors = dict(DEFAULT_SENSORS)
        if sensors:
            self.sensors.update(
                {str(name).lower(): str(uid) for name, uid in sensors.items()}
            )
        self.pressure_board = str(pressure_board)

    def _sensor(self, name):
        """Resolve a friendly sensor name to its board identifier.

        A raw board identifier is passed through unchanged, so a cryostat with
        an unusual fit can still be driven without configuring the map.
        """
        key = str(name).lower().strip()
        if key in self.sensors:
            return self.sensors[key]
        if "." in str(name):
            return str(name)
        raise RangeError(
            f"'{name}' is not a known sensor. This controller has "
            f"{', '.join(sorted(self.sensors))}, or pass a board identifier "
            "such as 'MB1.T1' directly."
        )

    def _temperature_noun(self, name, tail):
        return f"DEV:{self._sensor(name)}:TEMP:{tail}"

    # Temperature

    def temperature(self, sensor):
        """Read one sensor, in kelvin."""
        return self.read_value(self._temperature_noun(sensor, "SIG:TEMP"), "K")

    def temperatures(self):
        """Read every configured sensor, as a dict of name to kelvin."""
        return {name: self.temperature(name) for name in self.sensors}

    def setpoint(self, sensor, value=None):
        """Get or set the temperature setpoint of one loop, in kelvin.

        :param value: Setpoint to apply. Omitted, the present setpoint is read.
        """
        if value is None:
            return self.read_value(self._temperature_noun(sensor, "LOOP:TSET"), "K")
        check_range(value, 0, 2000, f"{sensor} temperature setpoint", " K")
        self.set_noun(self._temperature_noun(sensor, "LOOP:TSET"), value)
        return value

    def ramp_rate(self, sensor, value=None):
        """Get or set the setpoint ramp rate of one loop, in kelvin per minute."""
        if value is None:
            return self.read_value(self._temperature_noun(sensor, "LOOP:RSET"), "K/m")
        check_range(value, 0, 1000, f"{sensor} ramp rate", " K/min")
        self.set_noun(self._temperature_noun(sensor, "LOOP:RSET"), value)
        return value

    def ramp_enabled(self, sensor, value=None):
        """Get or set whether the setpoint ramps rather than stepping."""
        if value is None:
            reply = self.read_noun(self._temperature_noun(sensor, "LOOP:RENA"))
            return self.parse_word(reply, "RENA") == "ON"
        state = check_boolean(value, "setpoint ramping")
        self.set_noun(
            self._temperature_noun(sensor, "LOOP:RENA"), "ON" if state else "OFF"
        )
        return state

    # Heater

    def heater_percent(self, sensor, value=None):
        """Get or set the heater output of one loop, as a percentage."""
        if value is None:
            return self.read_value(self._temperature_noun(sensor, "LOOP:HSET"), "%")
        check_range(value, 0, 100, f"{sensor} heater output", " percent")
        self.set_noun(self._temperature_noun(sensor, "LOOP:HSET"), value)
        return value

    def automatic_pid(self, sensor, value=None):
        """Get or set whether the loop picks its own PID terms from a table."""
        if value is None:
            reply = self.read_noun(self._temperature_noun(sensor, "LOOP:PIDT"))
            return self.parse_word(reply, "PIDT") == "ON"
        state = check_boolean(value, "automatic PID")
        self.set_noun(
            self._temperature_noun(sensor, "LOOP:PIDT"), "ON" if state else "OFF"
        )
        return state

    def pid(self, sensor, proportional=None, integral=None, derivative=None):
        """Get or set the PID terms of one loop.

        With no arguments, returns the three terms as a tuple.
        """
        if proportional is None and integral is None and derivative is None:
            return (
                self.read_value(self._temperature_noun(sensor, "LOOP:P")),
                self.read_value(self._temperature_noun(sensor, "LOOP:I")),
                self.read_value(self._temperature_noun(sensor, "LOOP:D")),
            )
        for term, letter, name in (
            (proportional, "P", "proportional"),
            (integral, "I", "integral"),
            (derivative, "D", "derivative"),
        ):
            if term is not None:
                check_range(term, 0, 1e6, f"{name} term")
                self.set_noun(self._temperature_noun(sensor, f"LOOP:{letter}"), term)
        return None

    def heater_enabled(self, sensor, value=None):
        """Get or set whether the loop's heater is enabled at all."""
        if value is None:
            reply = self.read_noun(self._temperature_noun(sensor, "LOOP:ENAB"))
            return self.parse_word(reply, "ENAB") == "ON"
        state = check_boolean(value, "heater")
        self.set_noun(
            self._temperature_noun(sensor, "LOOP:ENAB"), "ON" if state else "OFF"
        )
        return state

    # Needle valve and flow

    @property
    def pressure(self):
        """Returns the pressure at the needle valve loop, in millibar."""
        return self.read_value(f"DEV:{self.pressure_board}:PRES:SIG:PRES", "mB")

    @property
    def pressure_setpoint(self):
        """Returns the pressure the needle valve loop is regulating to, in millibar."""
        return self.read_value(f"DEV:{self.pressure_board}:PRES:LOOP:TSET", "mB")

    @pressure_setpoint.setter
    def pressure_setpoint(self, value):
        check_range(value, 0, 1000, "pressure setpoint", " mbar")
        self.set_noun(f"DEV:{self.pressure_board}:PRES:LOOP:TSET", value)

    @property
    def flow_percent(self):
        """Returns the needle valve opening, as a percentage."""
        return self.read_value(f"DEV:{self.pressure_board}:PRES:LOOP:FSET", "%")

    @flow_percent.setter
    def flow_percent(self, value):
        check_range(value, 0, 100, "needle valve opening", " percent")
        self.set_noun(f"DEV:{self.pressure_board}:PRES:LOOP:FSET", value)

    @property
    def automatic_flow(self):
        """Returns whether the needle valve regulates itself."""
        reply = self.read_noun(f"DEV:{self.pressure_board}:PRES:LOOP:FAUT")
        return self.parse_word(reply, "FAUT") == "ON"

    @automatic_flow.setter
    def automatic_flow(self, value):
        state = check_boolean(value, "automatic flow")
        self.set_noun(
            f"DEV:{self.pressure_board}:PRES:LOOP:FAUT", "ON" if state else "OFF"
        )

    # Waiting

    def wait_for_temperature(
        self,
        sensor,
        target=None,
        tolerance=0.05,
        hold=30.0,
        timeout=7200.0,
        interval=5.0,
    ):
        """Block until a sensor settles at its setpoint.

        Requires the temperature to stay within tolerance for ``hold`` seconds
        rather than merely touching it once, since a cryostat commonly
        overshoots through the setpoint on the way.

        :param sensor: Which sensor to watch.
        :param target: Temperature to wait for, in kelvin. Defaults to the
                       loop's present setpoint.
        :param tolerance: How close counts as settled, as a fraction of the
                          target.
        :param hold: Seconds the temperature must stay within tolerance.
        :param timeout: Seconds to wait before giving up.
        """
        if target is None:
            target = self.setpoint(sensor)
        target = float(target)
        check_range(tolerance, 0, 1, "tolerance", " (fraction of target)")
        window = abs(target) * float(tolerance)

        deadline = time.monotonic() + float(timeout)
        settled_since = None
        while True:
            reading = self.temperature(sensor)
            if abs(reading - target) <= window:
                if settled_since is None:
                    settled_since = time.monotonic()
                elif time.monotonic() - settled_since >= float(hold):
                    return reading
            else:
                settled_since = None

            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"The {sensor} sensor did not settle within {window:.3f} K of "
                    f"{target} K after {timeout} s. It last read {reading} K."
                )
            time.sleep(interval)

    def sweep_temperature(
        self,
        sensor,
        start,
        stop,
        points=None,
        step=None,
        tolerance=0.05,
        hold=30.0,
        timeout=7200.0,
    ):
        """Step through a series of temperatures, yielding once each is stable.

            for temperature in itc.sweep_temperature("vti", 2, 10, points=9):
                resistance = measure()

        :param sensor: Which sensor to control and watch.
        :param start: First temperature, in kelvin.
        :param stop: Last temperature, in kelvin.
        :param points: Number of temperatures, including both ends.
        :param step: Spacing between temperatures, as an alternative to points.
        :param tolerance: How close counts as settled, as a fraction of target.
        :param hold: Seconds the temperature must stay within tolerance.
        :yield: The temperature actually reached, in kelvin.
        """
        for target in sweep_values(start, stop, points=points, step=step):
            self.setpoint(sensor, target)
            yield self.wait_for_temperature(
                sensor, target, tolerance=tolerance, hold=hold, timeout=timeout
            )

    def __repr__(self):
        return f"MercuryItc({self._transport!r}, sensors={sorted(self.sensors)})"
