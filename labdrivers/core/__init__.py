"""Shared foundation for the labdrivers instrument drivers.

Drivers get their connection handling, reply parsing, argument validation and
error types from here, and add only what is specific to their instrument.
"""

import logging

from .errors import (
    ConnectionFailure,
    InstrumentError,
    InstrumentTimeoutError,
    LabdriversError,
    RangeError,
    UnknownSetting,
)
from .instrument import Instrument, ScpiInstrument, Settings
from .transport import (
    RecordingTransport,
    SocketTransport,
    Transport,
    VisaTransport,
    open_transport,
)
from .sweep import round_trip, sweep_values
from .validators import (
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
    nearest_allowed,
)

__all__ = [
    "ConnectionFailure",
    "Instrument",
    "InstrumentError",
    "InstrumentTimeoutError",
    "LabdriversError",
    "RangeError",
    "UnknownSetting",
    "RecordingTransport",
    "ScpiInstrument",
    "Settings",
    "SocketTransport",
    "Transport",
    "VisaTransport",
    "check_boolean",
    "check_choice",
    "check_integer_range",
    "check_range",
    "enable_logging",
    "nearest_allowed",
    "open_transport",
    "round_trip",
    "sweep_values",
]

# Whether anything is printed is the calling program's decision, not the
# library's, so labdrivers emits log records and attaches no handler of its
# own. enable_logging() below opts in.
logging.getLogger(__name__).addHandler(logging.NullHandler())


def enable_logging(level=logging.DEBUG, stream=None):
    """Print every command sent to and received from an instrument.

    A convenience for debugging a measurement from a notebook or a console.
    Without it, labdrivers emits log records but installs no handler.

    :param level: Logging level to show (default: DEBUG, which is every
                  command and reply).
    :param stream: Where to write, defaulting to standard error.
    :return: The handler that was installed, so it can be removed again.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    package_logger = logging.getLogger("labdrivers")
    package_logger.addHandler(handler)
    package_logger.setLevel(level)
    return handler
