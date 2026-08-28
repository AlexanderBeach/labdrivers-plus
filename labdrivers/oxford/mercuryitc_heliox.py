"""Driver for the Mercury iTC as fitted to an Oxford HelioxVT insert.

A Heliox is a single-shot helium-3 refrigerator. Its iTC controls a different
set of stages from a plain VTI system, namely the sorb, the He-3 pot and the
1 K plate. Running it also involves a condense-and-recirculate cycle that has
no equivalent on a standard cryostat.

The sensor map differs from a plain VTI system, and the sorb has a temperature
limit that must not be exceeded, so the two are separate drivers.

Commands are transcribed from the *HelioxVT Manual* (issue 5) and the
*Mercury iTC Operator's Manual* (issue 18).
"""

import time

from ..core import check_range
from ..core.errors import InstrumentTimeoutError
from .mercuryitc import MercuryItc

# Board identifiers for a HelioxVT fit. Override with sensors= if yours differs.
HELIOX_SENSORS = {
    "sorb": "DB6.T1",
    "he3pot": "DB7.T1",
    "he3pot_low": "DB7.T1",
    "onek": "MB1.T1",
}

# The sorb heater is used to drive helium off the charcoal during condensing.
# Going far above this risks the sorb heater, so the driver refuses to.
MAXIMUM_SORB_TEMPERATURE = 50.0

# Typical condensing and base-temperature setpoints, in kelvin.
DEFAULT_CONDENSE_TEMPERATURE = 30.0
DEFAULT_SORB_BASE_TEMPERATURE = 4.0


class MercuryItcHeliox(MercuryItc):
    """Interface to the Mercury iTC in an Oxford HelioxVT insert.

        heliox = MercuryItcHeliox(ip_address="192.168.0.11")
        heliox.condense()
        heliox.wait_for_temperature("he3pot", 0.3)

    :param maximum_sorb_temperature: Highest the sorb may be driven to, in
                                     kelvin.
    """

    maximum_sorb_temperature = None

    def __init__(
        self,
        *args,
        maximum_sorb_temperature=MAXIMUM_SORB_TEMPERATURE,
        sensors=None,
        **kwargs,
    ):
        merged = dict(HELIOX_SENSORS)
        if sensors:
            merged.update(
                {str(name).lower(): str(uid) for name, uid in sensors.items()}
            )
        super().__init__(*args, sensors=merged, **kwargs)
        self.maximum_sorb_temperature = float(maximum_sorb_temperature)

    # Stage temperatures

    @property
    def sorb_temperature(self):
        """Returns the sorb temperature, in kelvin."""
        return self.temperature("sorb")

    @property
    def he3_pot_temperature(self):
        """Returns the helium-3 pot temperature, in kelvin. This is the sample stage."""
        return self.temperature("he3pot")

    @property
    def one_kelvin_plate_temperature(self):
        """Returns the 1 K plate temperature, in kelvin."""
        return self.temperature("onek")

    # Sorb control

    def set_sorb_temperature(self, value):
        """Set the sorb setpoint, refusing anything that could damage it."""
        check_range(
            value,
            0,
            self.maximum_sorb_temperature,
            "sorb temperature setpoint",
            " K",
        )
        return self.setpoint("sorb", value)

    # Condensing cycle
    #
    # A single-shot He-3 cycle is: heat the sorb so it releases the helium,
    # which condenses in the pot. Then cool the sorb so it pumps on the pot,
    # taking it to base temperature. The charge lasts until the pot runs dry,
    # at which point the cycle is repeated.

    def condense(
        self, sorb_temperature=DEFAULT_CONDENSE_TEMPERATURE, wait=True, timeout=3600.0
    ):
        """Drive the sorb warm so the helium-3 condenses into the pot.

        :param sorb_temperature: Temperature to hold the sorb at, in kelvin.
        :param wait: Block until the sorb reaches that temperature.
        """
        self.set_sorb_temperature(sorb_temperature)
        self.heater_enabled("sorb", True)
        if wait:
            self.wait_for_temperature("sorb", sorb_temperature, timeout=timeout)
        return self.sorb_temperature

    def recirculate(
        self, sorb_temperature=DEFAULT_SORB_BASE_TEMPERATURE, wait=True, timeout=3600.0
    ):
        """Cool the sorb so it pumps on the pot and takes it to base.

        :param sorb_temperature: Temperature to hold the sorb at, in kelvin.
        :param wait: Block until the sorb reaches that temperature.
        """
        self.set_sorb_temperature(sorb_temperature)
        if wait:
            self.wait_for_temperature("sorb", sorb_temperature, timeout=timeout)
        return self.sorb_temperature

    def run_cycle(
        self,
        condense_temperature=DEFAULT_CONDENSE_TEMPERATURE,
        base_temperature=DEFAULT_SORB_BASE_TEMPERATURE,
        soak=600.0,
        timeout=3600.0,
    ):
        """Run a full condense-and-recirculate cycle.

        :param condense_temperature: Sorb temperature during condensing.
        :param base_temperature: Sorb temperature during recirculation.
        :param soak: Seconds to hold the sorb warm before cooling it, which is
                     what determines how much helium is condensed and so how
                     long the charge lasts.
        :return: The helium-3 pot temperature at the end.
        """
        self.condense(condense_temperature, wait=True, timeout=timeout)
        time.sleep(float(soak))
        self.recirculate(base_temperature, wait=True, timeout=timeout)
        return self.he3_pot_temperature

    def wait_for_base(self, target=0.35, timeout=7200.0, interval=10.0):
        """Block until the helium-3 pot reaches base temperature.

        :param target: Temperature to wait for, in kelvin.
        :raises InstrumentTimeoutError: If it never gets there, which usually
                                        means the charge did not condense.
        """
        deadline = time.monotonic() + float(timeout)
        while self.he3_pot_temperature > float(target):
            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"The helium-3 pot did not reach {target} K within {timeout} s. "
                    f"It last read {self.he3_pot_temperature} K. The charge "
                    "may not have condensed."
                )
            time.sleep(interval)
        return self.he3_pot_temperature

    def __repr__(self):
        return f"MercuryItcHeliox({self._transport!r})"
