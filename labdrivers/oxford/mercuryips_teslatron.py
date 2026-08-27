"""Driver for the Mercury iPS as fitted to an Oxford TeslatronPT.

A TeslatronPT has one superconducting solenoid on the z axis rather than a
vector magnet, a higher field limit, and a persistent-mode switch heater that
has to be sequenced correctly. It also exposes the pulse tube stage
temperatures, which a bare iPS does not.

Driving a Teslatron with a vector magnet's limits would let a script ask for a
field the solenoid cannot reach, and the persistent-mode sequence has no
meaning on a magnet without a switch heater, so the two are separate drivers.

Commands are transcribed from the *TeslatronPT System Manual* (issue 01) and
the *Mercury iPS Operator's Manual* (issue 20).
"""

import time

from ..core.errors import InstrumentTimeoutError
from .mercuryips import MercuryIps

# A TeslatronPT solenoid is normally specified to 8, 12 or 14 T. 12 T is the
# common fit. Pass field_limits= for a different one.
DEFAULT_TESLATRON_FIELD_LIMIT = 12.0

# Seconds to allow the switch heater to warm or cool before moving on. The
# magnet can be damaged by ramping while the switch is still superconducting,
# so this wait is not optional.
SWITCH_HEATER_SETTLE_TIME = 60.0


class MercuryIpsTeslatron(MercuryIps):
    """Interface to the Mercury iPS in an Oxford TeslatronPT.

        supply = MercuryIpsTeslatron(ip_address="192.168.0.10")
        supply.z.ramp_to_field(5.0)
        supply.enter_persistent_mode()

    :param field_limit: Solenoid field limit in tesla (default: 12).
    :param switch_heater_wait: Seconds to allow the switch heater to change
                               state before ramping.
    """

    def __init__(
        self,
        *args,
        field_limit=DEFAULT_TESLATRON_FIELD_LIMIT,
        switch_heater_wait=SWITCH_HEATER_SETTLE_TIME,
        **kwargs,
    ):
        kwargs.setdefault("axes", ("GRPZ",))
        kwargs.setdefault("field_limits", {"GRPZ": field_limit})
        super().__init__(*args, **kwargs)
        self.switch_heater_wait = float(switch_heater_wait)

    @property
    def magnet(self):
        """Returns the solenoid. A Teslatron has only the one."""
        return self.z

    # Pulse tube temperatures

    @property
    def pt1_temperature(self):
        """Returns the first pulse tube stage temperature, in kelvin."""
        return self.read_value("DEV:DB5.T1:TEMP:SIG:TEMP", "K")

    @property
    def pt2_temperature(self):
        """Returns the second pulse tube stage temperature, in kelvin."""
        return self.read_value("DEV:DB6.T1:TEMP:SIG:TEMP", "K")

    # Persistent mode
    #
    # The sequence matters. Entering persistent mode means: reach the field,
    # hold, open the switch (heater off), wait for it to become
    # superconducting, then ramp the leads to zero. Leaving means the reverse.
    # Getting the order wrong either quenches the magnet or dumps the current.

    def enter_persistent_mode(self, timeout=3600.0):
        """Put the magnet into persistent mode at its present field.

        Holds the field, turns the switch heater off, waits for the switch to
        go superconducting, then ramps the leads down to zero.

        :param timeout: Seconds to allow for each ramp.
        """
        magnet = self.magnet
        magnet.hold()
        magnet.wait_for_field(timeout=timeout)

        magnet.switch_heater = False
        time.sleep(self.switch_heater_wait)

        magnet.ramp_to_zero()
        magnet.wait_for_field(timeout=timeout)
        return magnet.persistent_field

    def leave_persistent_mode(self, timeout=3600.0):
        """Bring the magnet out of persistent mode.

        Ramps the leads back to the field the magnet is holding, closes the
        switch, and waits for it to become normal.

        :param timeout: Seconds to allow for the ramp.
        """
        magnet = self.magnet
        held = magnet.persistent_field

        magnet.field_setpoint = held
        magnet.ramp_to_setpoint()
        magnet.wait_for_field(timeout=timeout)

        magnet.switch_heater = True
        time.sleep(self.switch_heater_wait)
        return held

    def in_persistent_mode(self):
        """Whether the magnet is persistent: switch open, leads at zero."""
        return not self.magnet.switch_heater

    def wait_for_switch_heater(self, state, timeout=300.0, interval=5.0):
        """Block until the switch heater reports the requested state.

        :param state: True to wait for on, False for off.
        :raises InstrumentTimeoutError: If it never gets there.
        """
        wanted = bool(state)
        deadline = time.monotonic() + float(timeout)
        while self.magnet.switch_heater != wanted:
            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"The switch heater did not turn {'on' if wanted else 'off'} "
                    f"within {timeout} s."
                )
            time.sleep(interval)
        return True

    def __repr__(self):
        return f"MercuryIpsTeslatron({self._transport!r})"
