"""Driver for the Oxford Instruments ITC 503 temperature controller.

An older GPIB controller using Oxford's single-letter protocol, with three
sensor channels, a heater output and a gas-flow (needle valve) output.

Commands are transcribed from the *ITC 503 Operator's Handbook*.
"""

import time

from ..core import check_boolean, check_choice, check_integer_range, check_range
from ..core.errors import InstrumentTimeoutError
from .legacy import OxfordLegacyInstrument

# Numbered parameters the R command reads.
PARAMETERS = {
    "setpoint": 0,
    "sensor1": 1,
    "sensor2": 2,
    "sensor3": 3,
    "temperature_error": 4,
    "heater_percent": 5,
    "heater_volts": 6,
    "gas_flow": 7,
    "proportional": 8,
    "integral": 9,
    "derivative": 10,
}

# Bit pattern for the A command: heater and gas each auto or manual.
AUTO_MODES = {
    "heater manual gas manual": 0,
    "heater auto gas manual": 1,
    "heater manual gas auto": 2,
    "heater auto gas auto": 3,
}


class Itc503(OxfordLegacyInstrument):
    """Interface to an ITC 503 temperature controller.

        controller = Itc503(gpib_address=24)
        controller.set_control("remote unlocked")
        controller.heater_sensor = 1
        controller.auto_mode = "heater auto gas auto"
        controller.setpoint = 4.2

    :param default_sensor: Which sensor temperature() reads when not told.
    """

    def __init__(self, *args, default_sensor=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_sensor = check_integer_range(default_sensor, 1, 3, "sensor")

    # Readings

    def temperature(self, sensor=None):
        """Read one sensor, in kelvin.

        :param sensor: Which sensor, 1 to 3. Defaults to default_sensor.
        """
        number = check_integer_range(
            self.default_sensor if sensor is None else sensor, 1, 3, "sensor"
        )
        return self.read_parameter(PARAMETERS[f"sensor{number}"])

    def temperatures(self):
        """Read all three sensors, as a dict keyed by sensor number."""
        return {number: self.temperature(number) for number in (1, 2, 3)}

    def parameter(self, name):
        """Read any documented parameter by name."""
        number = check_choice(name, PARAMETERS, "parameter")
        return self.read_parameter(number)

    @property
    def temperature_error(self):
        """Returns the difference between the setpoint and the control sensor.

        In kelvin.
        """
        return self.read_parameter(PARAMETERS["temperature_error"])

    # Setpoint and control

    @property
    def setpoint(self):
        """Returns the temperature the controller is holding, in kelvin."""
        return self.read_parameter(PARAMETERS["setpoint"])

    @setpoint.setter
    def setpoint(self, value):
        check_range(value, 0, 2000, "temperature setpoint", " K")
        self.command("T", f"{float(value):.4f}")

    @property
    def heater_sensor(self):
        """Returns which sensor the heater loop controls from, 1 to 3."""
        return self._heater_sensor

    @heater_sensor.setter
    def heater_sensor(self, value):
        number = check_integer_range(value, 1, 3, "heater sensor")
        self.command("H", number)
        self._heater_sensor = number

    _heater_sensor = 1

    @property
    def auto_mode(self):
        """Returns whether the heater and gas flow are automatic or manual."""
        return self._auto_mode

    @auto_mode.setter
    def auto_mode(self, value):
        code = check_choice(value, AUTO_MODES, "auto mode")
        self.command("A", code)
        self._auto_mode = value

    _auto_mode = "heater manual gas manual"

    # Outputs

    @property
    def heater_percent(self):
        """Returns the heater output, as a percentage of full scale."""
        return self.read_parameter(PARAMETERS["heater_percent"])

    @heater_percent.setter
    def heater_percent(self, value):
        check_range(value, 0, 99.9, "heater output", " percent")
        self.command("O", f"{float(value):.1f}")

    @property
    def gas_flow(self):
        """Returns the gas flow (needle valve) output, as a percentage."""
        return self.read_parameter(PARAMETERS["gas_flow"])

    @gas_flow.setter
    def gas_flow(self, value):
        check_range(value, 0, 99.9, "gas flow", " percent")
        self.command("G", f"{float(value):.1f}")

    @property
    def maximum_heater_voltage(self):
        """Returns the heater voltage limit, in volts."""
        return self.read_parameter(PARAMETERS["heater_volts"])

    @maximum_heater_voltage.setter
    def maximum_heater_voltage(self, value):
        check_range(value, 0, 40, "maximum heater voltage", " V")
        self.command("M", f"{float(value):.1f}")

    # PID

    def pid(self, proportional=None, integral=None, derivative=None):
        """Get or set the PID terms.

        With no arguments, returns the three terms as a tuple.

        :param proportional: Proportional band, in kelvin.
        :param integral: Integral action time, in minutes.
        :param derivative: Derivative action time, in minutes.
        """
        if proportional is None and integral is None and derivative is None:
            return (
                self.read_parameter(PARAMETERS["proportional"]),
                self.read_parameter(PARAMETERS["integral"]),
                self.read_parameter(PARAMETERS["derivative"]),
            )
        if proportional is not None:
            check_range(proportional, 0, 1000, "proportional band", " K")
            self.command("P", f"{float(proportional):.4f}")
        if integral is not None:
            check_range(integral, 0, 140, "integral action time", " minutes")
            self.command("I", f"{float(integral):.4f}")
        if derivative is not None:
            check_range(derivative, 0, 273, "derivative action time", " minutes")
            self.command("D", f"{float(derivative):.4f}")
        return None

    @property
    def automatic_pid(self):
        """Returns whether PID terms are taken from the built-in table."""
        return self._automatic_pid

    @automatic_pid.setter
    def automatic_pid(self, value):
        state = check_boolean(value, "automatic PID")
        self.command("L", int(state))
        self._automatic_pid = state

    _automatic_pid = False

    # Sweeps

    def set_sweep_step(self, step, temperature, sweep_minutes, hold_minutes):
        """Program one step of the controller's internal sweep table.

        :param step: Which step, 1 to 16.
        :param temperature: Temperature to reach at the end of the step.
        :param sweep_minutes: Minutes to take getting there, 0 to 1339.9.
        :param hold_minutes: Minutes to hold once there, 0 to 1339.9.
        """
        number = check_integer_range(step, 1, 16, "sweep step")
        check_range(temperature, 0, 2000, "sweep step temperature", " K")
        check_range(sweep_minutes, 0, 1339.9, "sweep time", " minutes")
        check_range(hold_minutes, 0, 1339.9, "hold time", " minutes")

        self.command("x", number)
        self.command("y", 1)
        self.command("s", f"{float(temperature):.4f}")
        self.command("y", 2)
        self.command("s", f"{float(sweep_minutes):.4f}")
        self.command("y", 3)
        self.command("s", f"{float(hold_minutes):.4f}")

    def start_sweep(self):
        """Start the programmed sweep."""
        self.command("S", 1)

    def stop_sweep(self):
        """Stop the sweep and hold the present setpoint."""
        self.command("S", 0)

    # Waiting

    def wait_for_temperature(
        self,
        target=None,
        tolerance=0.05,
        hold=30.0,
        timeout=7200.0,
        interval=5.0,
        sensor=None,
    ):
        """Block until a sensor settles at the setpoint.

        :param target: Temperature in kelvin. Defaults to the present setpoint.
        :param tolerance: How close counts as settled, as a fraction of target.
        :param hold: Seconds it must stay within tolerance.
        """
        if target is None:
            target = self.setpoint
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
                    f"The ITC 503 did not settle within {window:.3f} K of {target} K "
                    f"after {timeout} s. It last read {reading} K."
                )
            time.sleep(interval)

    def __repr__(self):
        return f"Itc503({self._transport!r})"
