"""Driver for the Oxford Instruments Triton 200 cryogen-free dilution refrigerator.

The Triton speaks the same line protocol as the Mercury family, on port 33576
rather than 7020.

The heater range a mixing chamber wants depends on the temperature it is being
held at, and the right ladder depends on the particular fridge and its heater
resistance. Pass ``heater_ranges`` and ``heater_thresholds`` to match the
fridge in front of you rather than editing the module.

Commands are transcribed from the *Triton Manual* (4.0.1) and the
*Handbook: TRITON200 Cryofree Dilution Refrigerator*.
"""

import time

from ..core import check_boolean, check_choice, check_integer_range, check_range
from ..core.errors import InstrumentTimeoutError, RangeError
from ..core.sweep import sweep_values
from .mercury import MercuryInstrument

DEFAULT_PORT = 33576

# Heater currents in milliamps, smallest first.
DEFAULT_HEATER_RANGES = ("0.316", "1", "3.16", "10", "31.6", "100")

# Setpoints in kelvin at which the next heater range up is needed. One fewer
# than the number of ranges.
DEFAULT_HEATER_THRESHOLDS = (0.030, 0.050, 0.170, 0.240, 1.0)

# Temperature channels a Triton 200 exposes.
VALID_CHANNELS = tuple(range(1, 16))

VALVE_STATES = {"open": "OPEN", "close": "CLOSE", "closed": "CLOSE"}


class Triton200(MercuryInstrument):
    """Interface to a Triton 200 dilution refrigerator.

        fridge = Triton200(ip_address="192.168.0.12")
        fridge.temperature_setpoint = 0.02
        print(fridge.temperature(5))

    :param temperature_channel: Which channel the control loop reads.
    :param heater_ranges: Heater currents in milliamps, smallest first.
    :param heater_thresholds: Setpoints in kelvin at which to step up a range.
                              Must be one shorter than heater_ranges.
    """

    def __init__(
        self,
        *args,
        temperature_channel=5,
        heater_channel=1,
        turbo_channel=1,
        heater_ranges=DEFAULT_HEATER_RANGES,
        heater_thresholds=DEFAULT_HEATER_THRESHOLDS,
        **kwargs,
    ):
        kwargs.setdefault("port", DEFAULT_PORT)
        super().__init__(*args, **kwargs)

        if len(heater_thresholds) != len(heater_ranges) - 1:
            raise RangeError(
                f"There must be exactly one fewer heater threshold than heater "
                f"range, but there are {len(heater_thresholds)} thresholds "
                f"for {len(heater_ranges)} ranges."
            )
        if list(heater_thresholds) != sorted(heater_thresholds):
            raise RangeError(
                "The heater thresholds must be in increasing order, but got "
                f"{heater_thresholds}."
            )

        self.heater_ranges = tuple(heater_ranges)
        self.heater_thresholds = tuple(heater_thresholds)
        self._temperature_channel = self._check_channel(temperature_channel)
        self.heater_channel = str(heater_channel)
        self.turbo_channel = str(turbo_channel)

    @staticmethod
    def _check_channel(channel):
        return check_integer_range(
            channel, VALID_CHANNELS[0], VALID_CHANNELS[-1], "temperature channel"
        )

    # Temperature

    def temperature(self, channel=None):
        """Read one temperature channel, in kelvin.

        :param channel: Which channel. Defaults to the control channel.
        """
        number = self._check_channel(
            self._temperature_channel if channel is None else channel
        )
        return self.read_value(f"DEV:T{number}:TEMP:SIG:TEMP", "K")

    def resistance(self, channel=None):
        """Read one channel's raw sensor resistance, in ohms."""
        number = self._check_channel(
            self._temperature_channel if channel is None else channel
        )
        return self.read_value(f"DEV:T{number}:TEMP:SIG:RES", "Ohm")

    @property
    def temperature_channel(self):
        """Returns the channel the control loop reads."""
        return self._temperature_channel

    @temperature_channel.setter
    def temperature_channel(self, value):
        self._temperature_channel = self._check_channel(value)

    @property
    def control_channel(self):
        """Returns the channel the heater loop is actually associated with."""
        reply = self.read_noun(f"DEV:H{self.heater_channel}:HTR:LOOP:SENS")
        word = self.parse_word(reply, "SENS")
        return int(word.lstrip("T")) if word.lstrip("T").isdigit() else word

    def enable_channel(self, channel, enabled=True):
        """Turn one temperature channel's measurement on or off.

        Channels left enabled are scanned in turn, so switching off the ones
        not in use makes the others update more often.
        """
        number = self._check_channel(channel)
        state = check_boolean(enabled, "channel measurement")
        self.set_noun(f"DEV:T{number}:TEMP:MEAS:ENAB", "ON" if state else "OFF")

    @property
    def temperature_setpoint(self):
        """Returns the temperature the control loop is holding, in kelvin."""
        return self.read_value(f"DEV:T{self._temperature_channel}:TEMP:LOOP:TSET", "K")

    @temperature_setpoint.setter
    def temperature_setpoint(self, value):
        check_range(value, 0, 300, "temperature setpoint", " K")
        self.set_noun(f"DEV:T{self._temperature_channel}:TEMP:LOOP:TSET", value)
        self.update_heater_range(value)

    # Heater

    def heater_range_for(self, setpoint):
        """Which heater range suits a given setpoint.

        :return: The heater current, in milliamps, as the string the Triton
                 expects.
        """
        index = sum(float(setpoint) > threshold for threshold in self.heater_thresholds)
        return self.heater_ranges[index]

    def update_heater_range(self, setpoint=None):
        """Point the heater at the control channel and set a suitable range.

        :param setpoint: Setpoint to choose the range for. Defaults to the
                         present setpoint.
        """
        if setpoint is None:
            setpoint = self.temperature_setpoint
        self.set_noun(
            f"DEV:T{self._temperature_channel}:TEMP:LOOP:HTR",
            f"H{self.heater_channel}",
        )
        current = self.heater_range_for(setpoint)
        self.set_noun(f"DEV:T{self._temperature_channel}:TEMP:LOOP:RANGE", current)
        return current

    @property
    def heater_power(self):
        """Returns the power the mixing chamber heater is delivering, in watts."""
        return self.read_value(f"DEV:H{self.heater_channel}:HTR:SIG:POWR", "W")

    # Control loop

    @property
    def closed_loop(self):
        """Returns whether the temperature control loop is closed."""
        reply = self.read_noun(f"DEV:T{self._temperature_channel}:TEMP:LOOP:MODE")
        return self.parse_word(reply, "MODE") == "ON"

    @closed_loop.setter
    def closed_loop(self, value):
        state = check_boolean(value, "closed loop control")
        if state:
            self.update_heater_range()
        self.set_noun(
            f"DEV:T{self._temperature_channel}:TEMP:LOOP:MODE",
            "ON" if state else "OFF",
        )

    def close_loop(self):
        """Start closed-loop temperature control."""
        self.closed_loop = True

    def open_loop(self):
        """Stop closed-loop control and turn the heater off."""
        self.closed_loop = False

    @property
    def ramp_rate(self):
        """Returns the rate the setpoint ramps at, in kelvin per minute."""
        return self.read_value(
            f"DEV:T{self._temperature_channel}:TEMP:LOOP:RAMP:RATE", "K/m"
        )

    @ramp_rate.setter
    def ramp_rate(self, value):
        check_range(value, 0, 1000, "ramp rate", " K/min")
        self.set_noun(f"DEV:T{self._temperature_channel}:TEMP:LOOP:RAMP:RATE", value)

    @property
    def ramp_enabled(self):
        """Returns whether the setpoint ramps rather than stepping."""
        reply = self.read_noun(f"DEV:T{self._temperature_channel}:TEMP:LOOP:RAMP:ENAB")
        return self.parse_word(reply, "ENAB") == "ON"

    @ramp_enabled.setter
    def ramp_enabled(self, value):
        state = check_boolean(value, "setpoint ramping")
        self.set_noun(
            f"DEV:T{self._temperature_channel}:TEMP:LOOP:RAMP:ENAB",
            "ON" if state else "OFF",
        )

    # Valves and pumps

    def set_valve(self, number, state):
        """Open or close one of the gas-handling valves.

        :param number: Valve number.
        :param state: 'open' or 'close'.
        """
        valve = check_integer_range(number, 1, 20, "valve number")
        code = check_choice(state, VALVE_STATES, "valve state")
        self.set_noun(f"DEV:V{valve}:VALV:SIG:STATE", code)

    def valve_state(self, number):
        """Returns whether one valve is open."""
        valve = check_integer_range(number, 1, 20, "valve number")
        reply = self.read_noun(f"DEV:V{valve}:VALV:SIG:STATE")
        return self.parse_word(reply, "STATE") == "OPEN"

    @property
    def turbo(self):
        """Returns whether the turbo pump is running."""
        reply = self.read_noun(f"DEV:TURB{self.turbo_channel}:PUMP:SIG:STATE")
        return self.parse_word(reply, "STATE") == "ON"

    @turbo.setter
    def turbo(self, value):
        state = check_boolean(value, "turbo pump")
        self.set_noun(
            f"DEV:TURB{self.turbo_channel}:PUMP:SIG:STATE", "ON" if state else "OFF"
        )

    @property
    def turbo_speed(self):
        """Returns the turbo pump speed, in hertz."""
        return self.read_value(f"DEV:TURB{self.turbo_channel}:PUMP:SIG:SPD", "Hz")

    # Waiting

    def wait_for_temperature(
        self,
        target=None,
        tolerance=0.05,
        hold=60.0,
        timeout=14400.0,
        interval=10.0,
        channel=None,
    ):
        """Block until the fridge settles at a temperature.

        Requires the reading to stay within tolerance for ``hold`` seconds,
        since a dilution fridge routinely passes through its setpoint before
        settling.

        :param target: Temperature in kelvin. Defaults to the present setpoint.
        :param tolerance: How close counts as settled, as a fraction of target.
        :param hold: Seconds it must stay within tolerance.
        :param timeout: Seconds to wait before giving up.
        """
        if target is None:
            target = self.temperature_setpoint
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
                    f"The fridge did not settle within {window:.4f} K of {target} K "
                    f"after {timeout} s. It last read {reading} K."
                )
            time.sleep(interval)

    def sweep_temperature(
        self,
        start,
        stop,
        points=None,
        step=None,
        tolerance=0.05,
        hold=60.0,
        timeout=14400.0,
    ):
        """Step through a series of temperatures, yielding once each is stable.

        The heater range is chosen for each setpoint on the way, which matters
        across the wide span a dilution fridge covers.

        :param start: First temperature, in kelvin.
        :param stop: Last temperature, in kelvin.
        :param points: Number of temperatures, including both ends.
        :param step: Spacing between temperatures, as an alternative to points.
        :yield: The temperature actually reached, in kelvin.
        """
        for target in sweep_values(start, stop, points=points, step=step):
            self.temperature_setpoint = target
            yield self.wait_for_temperature(
                target, tolerance=tolerance, hold=hold, timeout=timeout
            )

    def __repr__(self):
        return f"Triton200({self._transport!r})"
