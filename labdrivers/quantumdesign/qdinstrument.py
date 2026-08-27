"""Driver for Quantum Design cryostats through the QDInstrument .NET assembly.

Covers the PPMS, DynaCool, VersaLab, SVSM and MPMS 3 by way of Quantum Design's
``QDInstrument.dll``, which talks to MultiVu. Reaching it from Python needs
pythonnet, and MultiVu must be running on the machine that owns the instrument.

The assembly is located when an instrument is constructed, either from an
explicit path or from alongside this module, so importing labdrivers on a
machine without pythonnet or the DLL costs nothing.
"""

import logging
import os
import time

from ..core import check_choice, check_range
from ..core.errors import ConnectionFailure, InstrumentTimeoutError

logger = logging.getLogger(__name__)

# The DLL ships alongside this module.
DEFAULT_DLL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "QDInstrument.dll"
)

# Instrument types as QDInstrumentBase.QDInstrumentType numbers them.
INSTRUMENT_TYPES = {
    "ppms": 0,
    "versalab": 1,
    "dynacool": 2,
    "svsm": 3,
    "mpms3": 4,
}

# Temperature approach modes.
TEMPERATURE_APPROACHES = {"fast settle": 0, "no overshoot": 1}

# Field approach modes and end states.
FIELD_APPROACHES = {"linear": 0, "no overshoot": 1, "oscillate": 2}
FIELD_END_STATES = {"persistent": 0, "driven": 1}

# Position approach modes.
POSITION_APPROACHES = {"move to position": 0, "move to index": 1, "redefine": 2}


class QdInstrument:
    """Base for Quantum Design instruments reached through MultiVu.

    :param instrument_type: Which instrument, e.g. 'dynacool'.
    :param ip_address: Address of the machine running MultiVu. Use '127.0.0.1'
                       when running on that machine itself.
    :param remote: Whether to talk to MultiVu over the network rather than
                   in-process.
    :param dll_path: Location of QDInstrument.dll (default: alongside this
                     module).
    """

    def __init__(
        self, instrument_type, ip_address="127.0.0.1", remote=True, dll_path=None
    ):
        code = check_choice(instrument_type, INSTRUMENT_TYPES, "instrument type")
        self.instrument_type = str(instrument_type).lower()
        self.ip_address = str(ip_address)
        self.dll_path = DEFAULT_DLL_PATH if dll_path is None else str(dll_path)

        factory = self._load_assembly()
        self._instrument = factory.GetQDInstrument(code, bool(remote), self.ip_address)

    def _load_assembly(self):
        """Load QDInstrument.dll and return the instrument factory."""
        try:
            import clr
        except ImportError:
            raise ConnectionFailure(
                "pythonnet is not installed, so the Quantum Design .NET "
                "assembly cannot be loaded. Install it with "
                "'pip install pythonnet'."
            )

        if not os.path.isfile(self.dll_path):
            raise ConnectionFailure(
                f"Could not find QDInstrument.dll at '{self.dll_path}'. It ships "
                "with MultiVu. Pass its location as dll_path, or copy it next "
                "to this module."
            )

        try:
            clr.AddReference(self.dll_path)
            from QuantumDesign.QDInstrument import QDInstrumentFactory
        except Exception as err:
            raise ConnectionFailure(
                f"Could not load '{self.dll_path}'. If the file came from a "
                "download, Windows may have blocked it: right-click it, choose "
                "Properties, and click Unblock. The assembly is also 32-bit, so "
                "a 32-bit Python may be required. Original error: "
                f"{err}"
            )
        return QDInstrumentFactory

    # Temperature

    @property
    def temperature(self):
        """Returns the present sample temperature, in kelvin."""
        return self._instrument.GetTemperature(0.0, 0)[1]

    @property
    def temperature_status(self):
        """Returns the status code describing what the temperature control is doing."""
        return self._instrument.GetTemperature(0.0, 0)[2]

    def set_temperature(self, temperature, rate=10.0, approach="fast settle"):
        """Set the temperature setpoint.

        :param temperature: Target temperature, in kelvin.
        :param rate: Ramp rate, in kelvin per minute.
        :param approach: 'fast settle' or 'no overshoot'.
        """
        check_range(temperature, 0, 1000, "temperature setpoint", " K")
        check_range(rate, 0, 100, "temperature ramp rate", " K/min")
        code = check_choice(approach, TEMPERATURE_APPROACHES, "temperature approach")
        return self._instrument.SetTemperature(float(temperature), float(rate), code)

    def wait_for_temperature(self, delay=5.0, timeout=600.0):
        """Block until the temperature is stable.

        :param delay: Seconds between checks.
        :param timeout: Seconds to wait before giving up.
        :raises InstrumentTimeoutError: If it never stabilises.
        """
        return self._wait(
            lambda: self.temperature_status == 1,
            delay,
            timeout,
            "the temperature to stabilise",
        )

    # Field

    @property
    def field(self):
        """Returns the present magnetic field, in oersted."""
        return self._instrument.GetField(0.0, 0)[1]

    @property
    def field_status(self):
        """Returns the status code describing what the magnet is doing."""
        return self._instrument.GetField(0.0, 0)[2]

    def set_field(self, field, rate=200.0, approach="linear", end_state="driven"):
        """Set the magnetic field.

        :param field: Target field, in oersted.
        :param rate: Ramp rate, in oersted per second.
        :param approach: 'linear', 'no overshoot' or 'oscillate'.
        :param end_state: 'driven' or 'persistent'.
        """
        check_range(field, -1e5, 1e5, "field setpoint", " Oe")
        check_range(rate, 0, 1e4, "field ramp rate", " Oe/s")
        approach_code = check_choice(approach, FIELD_APPROACHES, "field approach")
        end_code = check_choice(end_state, FIELD_END_STATES, "field end state")
        return self._instrument.SetField(
            float(field), float(rate), approach_code, end_code
        )

    def wait_for_field(self, delay=5.0, timeout=600.0):
        """Block until the field is stable."""
        return self._wait(
            lambda: self.field_status == 1, delay, timeout, "the field to stabilise"
        )

    # Position

    @property
    def position(self):
        """Returns the present sample position, in degrees or millimetres."""
        return self._instrument.GetPosition("Horizontal Rotator", 0.0, 0)[1]

    @property
    def position_status(self):
        """Returns the status code describing what the sample stage is doing."""
        return self._instrument.GetPosition("Horizontal Rotator", 0.0, 0)[2]

    def set_position(self, position, rate=10.0, approach="move to position"):
        """Move the sample stage.

        :param position: Target position.
        :param rate: Speed of the move.
        :param approach: 'move to position', 'move to index' or 'redefine'.
        """
        code = check_choice(approach, POSITION_APPROACHES, "position approach")
        return self._instrument.SetPosition(
            "Horizontal Rotator", float(position), float(rate), code
        )

    def wait_for_position(self, delay=5.0, timeout=600.0):
        """Block until the sample stage stops moving."""
        return self._wait(
            lambda: self.position_status == 1,
            delay,
            timeout,
            "the sample stage to stop",
        )

    # Raw commands

    def send_ppms_command(self, command, argument1=0.0, argument2=0.0):
        """Send an arbitrary Model 6000 command. PPMS only.

        This is the escape hatch for anything the .NET interface does not
        expose, including the rotator.
        """
        if self.instrument_type != "ppms":
            raise ConnectionFailure(
                "Raw Model 6000 commands are only available on a PPMS. This is "
                f"a {self.instrument_type}."
            )
        return self._instrument.SendPPMSCommand(
            command, "", "", int(argument1), int(argument2)
        )

    def _wait(self, condition, delay, timeout, description):
        check_range(delay, 0, 3600, "check interval", " s")
        check_range(timeout, 0, 86400, "timeout", " s")
        deadline = time.monotonic() + float(timeout)
        while not condition():
            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"Timed out after {timeout} s waiting for {description} on "
                    f"the {self.instrument_type}."
                )
            time.sleep(float(delay))
        return True

    def __repr__(self):
        return f"{type(self).__name__}({self.ip_address!r})"


class Ppms(QdInstrument):
    """A Quantum Design PPMS."""

    def __init__(self, ip_address="127.0.0.1", **kwargs):
        super().__init__("ppms", ip_address, **kwargs)


class VersaLab(QdInstrument):
    """A Quantum Design VersaLab."""

    def __init__(self, ip_address="127.0.0.1", **kwargs):
        super().__init__("versalab", ip_address, **kwargs)


class Dynacool(QdInstrument):
    """A Quantum Design PPMS DynaCool."""

    def __init__(self, ip_address="127.0.0.1", **kwargs):
        super().__init__("dynacool", ip_address, **kwargs)


class Svsm(QdInstrument):
    """A Quantum Design SVSM."""

    def __init__(self, ip_address="127.0.0.1", **kwargs):
        super().__init__("svsm", ip_address, **kwargs)


class Mpms(QdInstrument):
    """A Quantum Design MPMS 3."""

    def __init__(self, ip_address="127.0.0.1", **kwargs):
        super().__init__("mpms3", ip_address, **kwargs)
