"""Shared protocol for the Oxford Instruments Mercury family.

The Mercury iPS and iTC speak a line protocol of their own rather than SCPI.
A read looks like::

    READ:DEV:GRPZ:PSU:SIG:FLD
    STAT:DEV:GRPZ:PSU:SIG:FLD:0.5000T

and a write looks like::

    SET:DEV:GRPZ:PSU:SIG:FSET:1.0
    STAT:SET:DEV:GRPZ:PSU:SIG:FSET:1.0:VALID

That trailing VALID or INVALID is the instrument saying whether it accepted the
command, so every write checks it. Without that check a rejected setpoint is
indistinguishable from an accepted one.

This module holds the parts every Mercury driver needs, and the
instrument-specific nouns live in the drivers themselves.
"""

import re

from ..core import Instrument
from ..core.errors import InstrumentError

# Terminator the Mercury expects on every command and puts on every reply.
TERMINATOR = "\r\n"


class MercuryInstrument(Instrument):
    """Base for instruments speaking the Mercury line protocol.

    :param resource_name: VISA resource name, for a serial or USB connection.
    :param ip_address: Address for a raw socket connection.
    :param port: TCP port, normally 7020.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("port", 7020)
        # The Mercury needs its own line ending on both directions.
        kwargs.setdefault("write_termination", TERMINATOR)
        kwargs.setdefault("read_termination", "\n")
        super().__init__(*args, **kwargs)

    def read_noun(self, noun):
        """Send ``READ:<noun>`` and return the instrument's reply.

        :param noun: The device path, e.g. 'DEV:GRPZ:PSU:SIG:FLD'.
        """
        return self.query(f"READ:{noun}")

    def is_responding(self):
        """Returns True if the controller answers.

        Asked rather than assumed. These speak a line protocol of their own
        instead of SCPI, so the check they would otherwise inherit only reports
        whether a socket object exists, and a controller switched off at the
        wall goes on looking healthy for as long as the server is left running.
        """
        try:
            return bool(self.query("READ:SYS:CAT"))
        except Exception:
            return False

    def read_value(self, noun, unit=""):
        """Read a numeric signal and return it as a float.

        :param noun: The device path.
        :param unit: The unit the Mercury appends, stripped before parsing.
                     Left empty, any recognized unit suffix is stripped.
        """
        return self.parse_value(self.read_noun(noun), noun, unit)

    def set_noun(self, noun, value=None):
        """Send ``SET:<noun>`` (optionally with a value) and check it was taken.

        :param noun: The device path.
        :param value: The value to set, appended after a colon.
        :raises InstrumentError: If the Mercury replies INVALID, or does not
                                 reply at all.
        """
        command = f"SET:{noun}" if value is None else f"SET:{noun}:{value}"
        reply = self.query(command)
        self.check_valid(reply, command)
        return reply

    @staticmethod
    def check_valid(reply, command):
        """Raise unless the Mercury confirmed a SET with VALID.

        :raises InstrumentError: If the reply is empty or ends in INVALID.
        """
        if not reply:
            raise InstrumentError(
                f"The Mercury did not reply to '{command}'. Check the address, "
                "and that no other program is holding the connection."
            )
        if reply.strip().upper().endswith("INVALID"):
            raise InstrumentError(
                f"The Mercury rejected '{command}' as invalid. Check the device "
                "identifier exists on this instrument and that the value is in "
                f"range. Full reply: {reply.strip()}"
            )
        return True

    @staticmethod
    def parse_value(reply, noun, unit=""):
        """Pull the number out of a Mercury reply.

        The reply echoes the noun, then the value with a unit suffix. Stripping
        by known unit is not enough on its own, because 'T/m' contains 'T',
        so the number is matched directly instead.

        :raises InstrumentError: If no number can be found in the reply.
        """
        if not reply:
            raise InstrumentError(f"The Mercury did not reply to a read of '{noun}'.")

        text = reply.strip()
        prefix = f"STAT:{noun}:"
        if text.upper().startswith(prefix.upper()):
            text = text[len(prefix) :]
        else:
            # Some firmware answers without echoing the whole noun. Fall back
            # to whatever follows the last colon.
            text = text.rsplit(":", 1)[-1]

        if unit and text.endswith(unit):
            text = text[: -len(unit)]

        match = re.match(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", text.strip())
        if not match:
            raise InstrumentError(
                f"Could not read a number out of the Mercury's reply to "
                f"'{noun}': {reply.strip()!r}"
            )
        return float(match.group(0))

    @staticmethod
    def parse_word(reply, noun):
        """Pull the trailing keyword out of a Mercury reply, e.g. 'HOLD'."""
        if not reply:
            raise InstrumentError(f"The Mercury did not reply to a read of '{noun}'.")
        return reply.strip().rsplit(":", 1)[-1].strip().upper()
