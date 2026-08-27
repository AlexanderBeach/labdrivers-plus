"""Exception types raised by labdrivers.

Every exception here derives from :class:`RuntimeError`, so a measurement
script can catch that one type and still be told which of them happened.
"""


class LabdriversError(RuntimeError):
    """Base class for every error raised by labdrivers."""


class ConnectionFailure(LabdriversError):
    """Raised when an instrument cannot be reached, opened or configured."""


class InstrumentError(LabdriversError):
    """Raised when an instrument reports a fault through its own error queue."""

    def __init__(self, message, code=None, instrument=None):
        super().__init__(message)
        self.code = code
        self.instrument = instrument


class RangeError(LabdriversError, ValueError):
    """Raised when a setting is outside the range the instrument accepts.

    Also derives from :class:`ValueError`, since that is what a caller
    validating user input would naturally expect to catch.
    """


class TimeoutError_(LabdriversError):
    """Raised when an instrument does not respond, or a wait never settles."""


# Exported under a friendlier name. The trailing underscore above only avoids
# shadowing the builtin inside this module.
InstrumentTimeoutError = TimeoutError_
