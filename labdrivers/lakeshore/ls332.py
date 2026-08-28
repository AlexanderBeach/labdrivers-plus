"""Driver for the Lake Shore Cryotronics Model 332 temperature controller.

Two sensor inputs (A and B) and two control loops. Loop 1 drives the main
heater with a selectable power range, and loop 2 is a lower-power analog
output.

Commands are transcribed from the *Model 332 Temperature Controller User's
Manual*, Chapter 6 (Remote Operation).
"""

import time

from ..core import (
    Instrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import InstrumentError, InstrumentTimeoutError
from ..core.sweep import sweep_values

INPUTS = ("A", "B")
LOOPS = (1, 2)

# Heater power ranges for loop 1. Off, then low, medium and high.
HEATER_RANGES = {"off": 0, "low": 1, "medium": 2, "high": 3}

# What the control loop reads its setpoint in.
SETPOINT_UNITS = {"kelvin": 1, "celsius": 2, "sensor": 3}

CONTROL_MODES = {
    "manual pid": 1,
    "zone": 2,
    "open loop": 3,
    "autotune pid": 4,
    "autotune pi": 5,
    "autotune p": 6,
}

# Sensor curve types accepted by INTYPE.
SENSOR_TYPES = {
    "silicon diode": 0,
    "gaas diode": 1,
    "platinum 250": 2,
    "platinum 500": 3,
    "platinum 5000": 4,
    "cernox": 5,
}


# Front-panel brightness, as the percentages the manual names and the codes the
# instrument takes for them.
BRIGHTNESS_CODES = {25: 0, 50: 1, 75: 2, 100: 3}
BRIGHTNESS_PERCENT = {code: percent for percent, code in BRIGHTNESS_CODES.items()}


class Ls332(Instrument):
    """Interface to a Lake Shore 332 temperature controller.

    controller = Ls332(gpib_address=12)
    print(controller.temperature("A"))
    controller.setpoint(1, 4.2)
    controller.heater_range = "medium"
    """

    IDENTIFIER = "MODEL332"

    def _check_input(self, channel):
        return check_choice(channel, INPUTS, "sensor input")

    def _check_loop(self, loop):
        return check_integer_range(loop, LOOPS[0], LOOPS[-1], "control loop")

    def identify(self):
        """Returns the instrument's identification string (``*IDN?``)."""
        return self.query("*IDN?")

    def is_responding(self):
        """Returns True if the instrument answers ``*IDN?``.

        Asked rather than assumed. An open GPIB session outlives the instrument
        being switched off underneath it, so a check that only reports whether
        a handle exists calls a dead instrument healthy for as long as the
        server is left running.
        """
        try:
            return bool(self.identify())
        except Exception:
            return False

    def reset(self):
        """Put the instrument back to its power-on defaults (``*RST``)."""
        self.write("*RST")

    def clear_status(self):
        """Clear the status registers (``*CLS``)."""
        self.write("*CLS")

    # Readings

    def temperature(self, channel="A") -> float:
        """Read one sensor input, in kelvin."""
        return self.query_float(f"KRDG? {self._check_input(channel)}")

    def temperature_celsius(self, channel="A") -> float:
        """Read one sensor input, in degrees Celsius."""
        return self.query_float(f"CRDG? {self._check_input(channel)}")

    def sensor_units(self, channel="A"):
        """Read one sensor input in its raw units (volts or ohms)."""
        return self.query_float(f"SRDG? {self._check_input(channel)}")

    def temperatures(self):
        """Read both inputs, as a dict keyed by input letter."""
        return {letter: self.temperature(letter) for letter in INPUTS}

    def reading_status(self, channel="A"):
        """Returns whether a reading is valid, and why not if it is not.

        :return: A tuple of (ok, description). The 332 reports invalid
                 readings as a bit field rather than as an out-of-range number,
                 so a reading of 0 K can mean the sensor is unplugged.
        """
        code = self.query_integer(f"RDGST? {self._check_input(channel)}")
        problems = []
        for bit, description in (
            (1, "invalid reading"),
            (16, "temperature under range"),
            (32, "temperature over range"),
            (64, "sensor units zero"),
            (128, "sensor units over range"),
        ):
            if code & bit:
                problems.append(description)
        return (not problems, ", ".join(problems) or "ok")

    # Setpoint and control

    def setpoint(self, loop=1, value=None):
        """Get or set a loop's setpoint, in the loop's setpoint units."""
        number = self._check_loop(loop)
        if value is None:
            return self.query_float(f"SETP? {number}")
        check_range(value, 0, 1000, f"loop {number} setpoint", " K")
        self.write(f"SETP {number},{value}")
        return value

    @property
    def heater_range(self):
        """Returns the heater power range for loop 1.

        One of 'off', 'low', 'medium' or 'high'.
        """
        code = self.query_integer("RANGE?")
        for name, value in HEATER_RANGES.items():
            if value == code:
                return name
        return str(code)

    @heater_range.setter
    def heater_range(self, value):
        code = check_choice(value, HEATER_RANGES, "heater range")
        self.write(f"RANGE {code}")

    @property
    def heater_output(self):
        """Returns the loop 1 heater output, as a percentage of the selected range."""
        return self.query_float("HTR?")

    def heater_status(self):
        """Whether the heater has tripped, and why.

        :return: A tuple of (ok, description).
        """
        code = self.query_integer("HTRST?")
        reasons = {
            0: "ok",
            1: "power supply over voltage",
            2: "power supply under voltage",
            3: "output digital-to-analog converter error",
            4: "current limit digital-to-analog converter error",
            5: "open heater load",
            6: "heater load less than 10 ohms",
        }
        return (code == 0, reasons.get(code, f"unknown fault {code}"))

    def manual_output(self, loop=1, value=None):
        """Get or set a loop's manual output, as a percentage."""
        number = self._check_loop(loop)
        if value is None:
            return self.query_float(f"MOUT? {number}")
        check_range(value, 0, 100, f"loop {number} manual output", " percent")
        self.write(f"MOUT {number},{value}")
        return value

    def control_mode(self, loop=1, value=None):
        """Get or set a loop's control mode."""
        number = self._check_loop(loop)
        if value is None:
            code = self.query_integer(f"CMODE? {number}")
            for name, mode in CONTROL_MODES.items():
                if mode == code:
                    return name
            return str(code)
        mode = check_choice(value, CONTROL_MODES, "control mode")
        self.write(f"CMODE {number},{mode}")
        return value

    def configure_loop(
        self,
        loop=1,
        channel="A",
        units="kelvin",
        powerup_enable=False,
        heater_on_error=True,
    ):
        """Point a control loop at a sensor and choose its setpoint units.

        The last two values go out as the fourth and fifth fields of CSET.
        Lake Shore assign those two fields differently across the 331, 332 and
        340, and on a controller where the fourth is the loop's own enable, a
        call made with the defaults switches the loop off rather than setting
        it up. Check the front panel still shows the loop enabled the first
        time this is used on a given controller.

        :param loop: Which control loop, 1 or 2.
        :param channel: Which input the loop reads to control from.
        :param units: Units the setpoint is given in.
        :param powerup_enable: Whether the loop re-enables itself after a
                               power cycle.
        :param heater_on_error: Whether the heater stays on when the loop
                                reports an error.
        """
        number = self._check_loop(loop)
        letter = self._check_input(channel)
        unit = check_choice(units, SETPOINT_UNITS, "setpoint units")
        powerup = check_boolean(powerup_enable, "power-up enable")
        on_error = check_boolean(heater_on_error, "heater on error")
        self.write(f"CSET {number},{letter},{unit},{int(powerup)},{int(on_error)}")

    def pid(self, loop=1, proportional=None, integral=None, derivative=None):
        """Get or set a loop's PID terms.

        With no terms given, returns them as a tuple.
        """
        number = self._check_loop(loop)
        if proportional is None and integral is None and derivative is None:
            return tuple(self.query_floats(f"PID? {number}"))
        present = self.query_floats(f"PID? {number}")
        terms = [
            present[0] if proportional is None else proportional,
            present[1] if integral is None else integral,
            present[2] if derivative is None else derivative,
        ]
        check_range(terms[0], 0.1, 1000, "proportional gain")
        check_range(terms[1], 0, 1000, "integral", " seconds")
        check_range(terms[2], 0, 200, "derivative", " percent")
        self.write(f"PID {number},{terms[0]},{terms[1]},{terms[2]}")
        return tuple(terms)

    def ramp(self, loop=1, enabled=None, rate=None):
        """Get or set a loop's setpoint ramp.

        :param rate: Ramp rate in kelvin per minute, 0.1 to 100.
        """
        number = self._check_loop(loop)
        if enabled is None and rate is None:
            reply = self.query_floats(f"RAMP? {number}")
            return bool(reply[0]), reply[1]
        state = check_boolean(True if enabled is None else enabled, "setpoint ramping")
        if rate is None:
            rate = self.query_floats(f"RAMP? {number}")[1]
        check_range(rate, 0.1, 100, "ramp rate", " K/min")
        self.write(f"RAMP {number},{int(state)},{rate}")
        return state, rate

    def ramping(self, loop=1):
        """Whether a loop's setpoint is currently ramping."""
        return self.query_boolean(f"RAMPST? {self._check_loop(loop)}")

    def autotune(self, loop=1, mode="autotune pid"):
        """Start the controller's PID autotune on a loop."""
        self.control_mode(loop, mode)

    # Sensor inputs

    def input_type(self, channel="A", sensor=None, compensation=None):
        """Get or set the sensor type on one input."""
        letter = self._check_input(channel)
        if sensor is None:
            code = self.query_floats(f"INTYPE? {letter}")[0]
            for name, value in SENSOR_TYPES.items():
                if value == int(code):
                    return name
            return str(int(code))
        value = check_choice(sensor, SENSOR_TYPES, "sensor type")
        compensate = check_boolean(
            False if compensation is None else compensation, "compensation"
        )
        self.write(f"INTYPE {letter},{value},{int(compensate)}")
        return sensor

    def curve_number(self, channel="A", value=None):
        """Get or set which calibration curve an input uses."""
        letter = self._check_input(channel)
        if value is None:
            return self.query_integer(f"INCRV? {letter}")
        number = check_integer_range(value, 0, 41, "curve number")
        self.write(f"INCRV {letter},{number}")
        return number

    # Front panel

    @property
    def display_brightness(self):
        """Returns the front-panel brightness, as a percentage."""
        return BRIGHTNESS_PERCENT[self.query_integer("BRIGT?")]

    @display_brightness.setter
    def display_brightness(self, value):
        code = check_choice(value, BRIGHTNESS_CODES, "display brightness")
        self.write(f"BRIGT {code}")

    def safe_shutdown(self):
        """Turn the heater off and leave the controller reading.

        The state to leave a temperature controller in when walking away from
        it. The setpoint is left alone, since it is a number rather than a
        thing delivering power, and the range is what actually drives current
        into the heater.
        """
        self.heater_range = "off"

    def lock_front_panel(self, locked=True, code=123):
        """Lock or unlock the front panel keypad.

        :param code: The three-digit code needed to unlock it again.
        """
        state = check_boolean(locked, "front panel lock")
        number = check_integer_range(code, 0, 999, "lock code")
        self.write(f"LOCK {int(state)},{number:03d}")

    def go_to_local(self):
        """Hand the instrument back to front-panel control."""
        self.write("MODE 0")

    def go_to_remote(self):
        """Put the instrument under remote control."""
        self.write("MODE 1")

    def beep(self, enabled=True):
        """Turn the audible alarm beeper on or off."""
        state = check_boolean(enabled, "beeper")
        self.write(f"BEEP {int(state)}")

    # Waiting

    def wait_for_temperature(
        self,
        target=None,
        channel="A",
        tolerance=0.05,
        hold=30.0,
        timeout=7200.0,
        interval=5.0,
    ):
        """Block until a sensor settles at a temperature.

        :param target: Temperature in kelvin. Defaults to loop 1's setpoint.
        :param tolerance: How close counts as settled, as a fraction of target.
        :param hold: Seconds it must stay within tolerance.
        """
        if self.heater_range == "off":
            # Asked before the wait rather than discovered at the end of it.
            # Nothing else in the loop can tell the difference between a heater
            # that is off and a cryostat that is simply slow, so without this
            # the answer arrives when the timeout does.
            raise InstrumentError(
                "The heater range is off, so the loop cannot reach a setpoint. "
                "Set heater_range to 'low', 'medium' or 'high' first."
            )
        if target is None:
            target = self.setpoint(1)
        target = float(target)
        check_range(tolerance, 0, 1, "tolerance", " (fraction of target)")
        window = abs(target) * float(tolerance)

        deadline = time.monotonic() + float(timeout)
        settled_since = None
        while True:
            reading = self.temperature(channel)
            if abs(reading - target) <= window:
                if settled_since is None:
                    settled_since = time.monotonic()
                elif time.monotonic() - settled_since >= float(hold):
                    return reading
            else:
                settled_since = None

            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"Input {channel} did not settle within {window:.3f} K of "
                    f"{target} K after {timeout} s. It last read {reading} K."
                )
            time.sleep(interval)

    def ramp_to(self, temperature, rate=None, loop=1, wait=True, timeout=7200.0):
        """Ramp the setpoint to a temperature rather than stepping to it.

        Stepping the setpoint asks the loop for as much heater power as it can
        give, which overshoots. Ramping walks it there at a controlled rate.

        :param temperature: Target temperature, in kelvin.
        :param rate: Ramp rate in kelvin per minute. Left alone if not given.
        :param wait: Block until the temperature settles.
        :return: The temperature reached, or the target if not waiting.
        """
        if rate is not None:
            self.ramp(loop, enabled=True, rate=rate)
        self.setpoint(loop, temperature)
        if not wait:
            return float(temperature)
        return self.wait_for_temperature(temperature, timeout=timeout)

    def sweep_temperature(
        self,
        start,
        stop,
        points=None,
        step=None,
        rate=None,
        channel="A",
        tolerance=0.05,
        hold=30.0,
        timeout=7200.0,
    ):
        """Step through a series of temperatures, yielding once each is stable.

            for temperature in controller.sweep_temperature(2, 10, points=9):
                resistance = measure()

        :param start: First temperature, in kelvin.
        :param stop: Last temperature, in kelvin.
        :param points: Number of temperatures, including both ends.
        :param step: Spacing between temperatures, as an alternative to points.
        :param rate: Ramp rate in kelvin per minute, applied once at the start.
        :param channel: Which input to watch while waiting.
        :param tolerance: How close counts as settled, as a fraction of the
                          target rather than an absolute number of kelvin. The
                          default of 0.05 is a window of 0.075 K at 1.5 K and
                          15 K at 300 K, so a sweep crossing a wide span wants
                          a smaller number than one at the bottom of it.
        :param hold: Seconds the temperature must stay inside that window
                     before the point counts as reached.
        :param timeout: Seconds to wait at any one temperature before giving up.
        :yield: The temperature actually reached, in kelvin.
        """
        if rate is not None:
            self.ramp(1, enabled=True, rate=rate)
        for target in sweep_values(start, stop, points=points, step=step):
            self.setpoint(1, target)
            yield self.wait_for_temperature(
                target,
                channel=channel,
                tolerance=tolerance,
                hold=hold,
                timeout=timeout,
            )

    def __repr__(self):
        return f"Ls332({self._transport!r})"
