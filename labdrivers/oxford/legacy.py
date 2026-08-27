"""Shared protocol for the older Oxford Instruments GPIB controllers.

The IPS 120-10 and ITC 503 predate the Mercury line protocol. They take
single-letter commands with a numeric argument, terminated by a carriage
return, and echo the command letter back::

    R7          ->  R+00.0000     read parameter 7
    C3          ->  C             set remote and unlocked

A reply that does not begin with the letter that was sent means the controller
did not understand the command, so every exchange checks the echo before
trusting what came back.
"""

from ..core import Instrument, check_choice
from ..core.errors import InstrumentError

TERMINATOR = "\r"

# Remote/local control states, shared by both instruments.
CONTROL_STATES = {
    "local locked": 0,
    "remote locked": 1,
    "local unlocked": 2,
    "remote unlocked": 3,
}


class OxfordLegacyInstrument(Instrument):
    """Base for the older Oxford GPIB controllers."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("read_termination", TERMINATOR)
        kwargs.setdefault("write_termination", TERMINATOR)
        super().__init__(*args, **kwargs)

    def command(self, letter, argument=""):
        """Send a single-letter command and confirm the controller echoed it.

        :param letter: The command letter, e.g. 'C' or 'R'.
        :param argument: The numeric argument, if the command takes one.
        :return: The reply with the echoed letter stripped off.
        :raises InstrumentError: If the reply does not echo the command letter.
        """
        sent = f"{letter}{argument}"
        reply = self.query(sent).strip()

        if reply.startswith("?"):
            raise InstrumentError(
                f"The controller rejected '{sent}' as an illegal command. "
                f"It replied {reply!r}."
            )
        if not reply.startswith(letter):
            raise InstrumentError(
                f"Expected the controller to echo '{letter}' in reply to "
                f"'{sent}', but it replied {reply!r}. The instrument may be in "
                "local mode, or another command's reply may still be queued."
            )
        return reply[len(letter) :]

    def read_parameter(self, number):
        """Read one of the controller's numbered parameters (``R<n>``).

        :return: The value as a float.
        """
        reply = self.command("R", int(number))
        try:
            return float(reply)
        except ValueError:
            raise InstrumentError(
                f"Expected a number from parameter {number}, got {reply!r}."
            )

    def set_control(self, state="remote unlocked"):
        """Set the local/remote control state (``C<n>``).

        Every other command needs the controller in a remote state first.
        """
        code = check_choice(state, CONTROL_STATES, "control state")
        self.command("C", code)

    def version(self):
        """Return the controller's firmware version string (``V``)."""
        return self.query("V").strip()
