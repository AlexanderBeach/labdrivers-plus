"""Driver for the Oxford Instruments IPS 120-10 magnet power supply.

An older GPIB supply using Oxford's single-letter protocol. Field limits belong
to the magnet rather than the supply, so they are a constructor argument.

Commands are transcribed from the *IPS 120-10 Operator's Handbook*.
"""

import time

from ..core import check_boolean, check_choice, check_range
from ..core.errors import InstrumentTimeoutError
from .legacy import OxfordLegacyInstrument

# Numbered parameters the R command reads.
PARAMETERS = {
    "demand_current": 0,
    "supply_voltage": 1,
    "measured_current": 2,
    "demand_current_setpoint": 5,
    "current_sweep_rate": 6,
    "demand_field": 7,
    "field_setpoint": 8,
    "field_sweep_rate": 9,
    "software_voltage_limit": 15,
    "persistent_current": 16,
    "trip_current": 17,
    "persistent_field": 18,
    "trip_field": 19,
    "switch_heater_current": 20,
    "safe_current_negative": 21,
    "safe_current_positive": 22,
    "lead_resistance": 23,
    "magnet_inductance": 24,
}

ACTIVITIES = {"hold": 0, "to setpoint": 1, "to zero": 2, "clamp": 4}
HEATER_STATES = {"off": 0, "on": 1, "off forced": 2}
DEFAULT_FIELD_LIMIT = 8.0


class Ips120(OxfordLegacyInstrument):
    """Interface to an IPS 120-10 magnet power supply.

        supply = Ips120(gpib_address=25, field_limit=8.0)
        supply.set_control("remote unlocked")
        supply.ramp_to_field(1.0)

    :param field_limit: Largest field this magnet may be asked for, in tesla.
    """

    def __init__(self, *args, field_limit=DEFAULT_FIELD_LIMIT, **kwargs):
        super().__init__(*args, **kwargs)
        self.field_limit = float(field_limit)

    # Readings

    @property
    def field(self):
        """Returns the field the supply is presently producing, in tesla."""
        return self.read_parameter(PARAMETERS["demand_field"])

    @property
    def persistent_field(self):
        """Returns the field held by the magnet in persistent mode, in tesla."""
        return self.read_parameter(PARAMETERS["persistent_field"])

    @property
    def current(self):
        """Returns the measured magnet current, in amps."""
        return self.read_parameter(PARAMETERS["measured_current"])

    @property
    def voltage(self):
        """Returns the supply output voltage, in volts."""
        return self.read_parameter(PARAMETERS["supply_voltage"])

    def parameter(self, name):
        """Read any documented parameter by name.

        :param name: One of the keys of PARAMETERS.
        """
        number = check_choice(name, PARAMETERS, "parameter")
        return self.read_parameter(number)

    # Setpoints

    @property
    def field_setpoint(self):
        """Returns the field the supply is ramping towards, in tesla."""
        return self.read_parameter(PARAMETERS["field_setpoint"])

    @field_setpoint.setter
    def field_setpoint(self, value):
        check_range(value, -self.field_limit, self.field_limit, "field setpoint", " T")
        self.command("J", f"{float(value):.4f}")

    @property
    def field_sweep_rate(self):
        """Returns the rate the field sweeps at, in tesla per minute."""
        return self.read_parameter(PARAMETERS["field_sweep_rate"])

    @field_sweep_rate.setter
    def field_sweep_rate(self, value):
        check_range(value, 0, self.field_limit, "field sweep rate", " T/min")
        self.command("T", f"{float(value):.4f}")

    # Activity and heater

    @property
    def activity(self):
        """Returns what the supply is doing.

        One of 'hold', 'to setpoint', 'to zero' or 'clamp'.
        """
        status = self.status()
        code = int(status[4]) if len(status) > 4 else 0
        for name, value in ACTIVITIES.items():
            if value == code:
                return name
        return str(code)

    @activity.setter
    def activity(self, value):
        code = check_choice(value, ACTIVITIES, "activity")
        self.command("A", code)

    @property
    def switch_heater(self):
        """Returns whether the persistent-mode switch heater is on."""
        status = self.status()
        return len(status) > 8 and status[8] == "1"

    @switch_heater.setter
    def switch_heater(self, value):
        if isinstance(value, str) and value.strip().lower() == "off forced":
            self.command("H", HEATER_STATES["off forced"])
            return
        state = check_boolean(value, "switch heater")
        self.command("H", HEATER_STATES["on" if state else "off"])

    def status(self):
        """Return the raw status string (``X``).

        The reply is a fixed-width field of digits describing system status,
        activity, heater state and sweep mode.
        """
        return self.query("X").strip()

    def set_display(self, quantity="tesla"):
        """Choose whether the front panel shows amps or tesla (``F<n>``)."""
        code = check_choice(quantity, {"amps": 8, "tesla": 9}, "display quantity")
        self.command("F", code)

    # Ramping

    def hold(self):
        """Stop any sweep and hold the present field."""
        self.activity = "hold"

    def ramp_to_setpoint(self):
        """Sweep towards the field setpoint."""
        self.activity = "to setpoint"

    def ramp_to_zero(self):
        """Sweep the field down to zero."""
        self.activity = "to zero"

    def clamp(self):
        """Clamp the output."""
        self.activity = "clamp"

    def wait_for_field(self, target=None, tolerance=0.01, timeout=3600.0, interval=5.0):
        """Block until the field reaches its setpoint.

        :param target: Field to wait for, in tesla. Defaults to the setpoint.
        :param tolerance: How close counts as arrived, in tesla.
        :raises InstrumentTimeoutError: If it never gets there.
        """
        if target is None:
            target = self.field_setpoint
        target = float(target)
        check_range(tolerance, 0, self.field_limit, "tolerance", " T")

        deadline = time.monotonic() + float(timeout)
        while abs(self.field - target) > float(tolerance):
            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"The magnet did not reach {target} T within {timeout} s. "
                    f"It last read {self.field} T."
                )
            time.sleep(interval)
        return self.field

    def ramp_to_field(self, field, wait=True, timeout=3600.0):
        """Set the setpoint and sweep to it.

        :param field: Target field in tesla.
        :param wait: Block until the sweep finishes.
        """
        self.field_setpoint = field
        self.ramp_to_setpoint()
        if wait:
            self.wait_for_field(field, timeout=timeout)

    def __repr__(self):
        return f"Ips120({self._transport!r})"
