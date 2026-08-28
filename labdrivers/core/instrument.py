"""The base every instrument driver is built on.

:class:`Instrument` owns the connection and the business of turning replies into
Python values. :class:`ScpiInstrument` adds the IEEE 488.2 common commands that
every SCPI instrument shares, so no driver has to redefine ``*IDN?`` or ``*RST``.

Anything specific to one instrument stays in that instrument's own driver.
That covers its command language, the ranges it accepts, and the way it
formats a reading.
"""

import difflib
import logging
import time

from .errors import InstrumentError, InstrumentTimeoutError, UnknownSetting
from .transport import open_transport


logger = logging.getLogger(__name__)


class Settings:
    """Refuses an attribute that is not one of this object's settings.

    Python is happy to put a new attribute on any object, so a mistyped setting
    name assigns to the object and sends nothing. The measurement then runs at
    whatever the instrument was already on, with no error anywhere. Everything
    else in this package says what went wrong, and this would be the one way to
    be quietly wrong.

    An instrument is not the only thing with settings on it. A magnet axis, a
    rotation stage and a Quantum Design system all take them as well, and a name
    misspelled on one of those goes the same way, so they carry this too.

    The rule is that the class is the whole list of what an object has. A
    setting is a property, and anything else a constructor fills in is declared
    beside it, so writing a driver means saying once what it has rather than
    discovering later what it turned out to have.
    """

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        existing = getattr(type(self), name, None)
        if hasattr(type(self), name) and not callable(existing):
            object.__setattr__(self, name, value)
            return

        settings = sorted(
            attribute
            for attribute in dir(type(self))
            if not attribute.startswith("_")
            and isinstance(getattr(type(self), attribute, None), property)
        )
        if callable(existing):
            # A name that exists and is a method. Assigning to it replaces the
            # method and sends nothing, and the measurement then runs at
            # whatever the instrument was already on. It reads naturally
            # because the same word is a setting on a different instrument.
            raise UnknownSetting(
                f"'{name}' on the {type(self).__name__} is something it does, "
                f"not something it has. Call {name}(...) instead of assigning "
                f"to it."
            )
        close = difflib.get_close_matches(name, settings, n=1, cutoff=0.6)
        suggestion = f" Did you mean '{close[0]}'?" if close else ""
        raise UnknownSetting(
            f"The {type(self).__name__} has no setting called '{name}'.{suggestion}"
        )


class Instrument(Settings):
    """Base class for an instrument reached over some transport.

    Every driver accepts the same connection arguments, so the choice between
    GPIB, serial, TCPIP and a raw socket is made at construction:

        Sr830(gpib_address=8)
        Sr830(resource_name="ASRL3::INSTR", baud_rate=9600)
        Sr830(transport=RecordingTransport())      # in tests

    :param resource_name: A full VISA resource string.
    :param gpib_address: A GPIB primary address.
    :param ip_address: Address for a raw socket connection.
    :param port: TCP port to go with ``ip_address``.
    :param transport: An already-built transport, used as-is.
    """

    #: Substring expected in the *IDN? reply, checked by :meth:`verify_identity`.
    IDENTIFIER = None

    def __init__(
        self,
        resource_name=None,
        gpib_address=None,
        ip_address=None,
        port=None,
        transport=None,
        **kwargs,
    ):
        self._transport = open_transport(
            resource_name=resource_name,
            gpib_address=gpib_address,
            ip_address=ip_address,
            port=port,
            transport=transport,
            **kwargs,
        )

    @property
    def transport(self):
        """Returns the transport this instrument talks over."""
        return self._transport

    # Sending and receiving

    def write(self, command):
        """Send a command that expects no reply."""
        self._transport.write(command)

    def read(self):
        """Read one reply, as a stripped string."""
        return self._transport.read()

    def query(self, command):
        """Send a command and return its reply, as a stripped string."""
        return self._transport.query(command)

    def query_binary(self, command, datatype="B", is_big_endian=False):
        """Send a command and read back an IEEE 488.2 binary block.

        :param datatype: struct code for one sample, e.g. 'B' for unsigned
                         byte, 'h' for signed 16-bit.
        :return: A list of numbers.
        """
        return self._transport.query_binary(
            command, datatype=datatype, is_big_endian=is_big_endian
        )

    def query_float(self, command):
        """Send a command and return its reply as a float.

        :raises InstrumentError: If the reply is not a number.
        """
        reply = self.query(command)
        try:
            return float(reply)
        except ValueError:
            raise InstrumentError(
                f"Expected a number in reply to '{command}', got {reply!r}.",
                instrument=self,
            )

    def query_integer(self, command):
        """Send a command and return its reply as an int.

        Tolerates instruments that answer an integer query in float form, such
        as '1.000000E+00' for 1.

        :raises InstrumentError: If the reply is not a whole number.
        """
        reply = self.query(command)
        try:
            return int(float(reply))
        except ValueError:
            raise InstrumentError(
                f"Expected a whole number in reply to '{command}', got {reply!r}.",
                instrument=self,
            )

    def query_floats(self, command):
        """Send a command and return a comma-separated reply as a list of floats.

        :raises InstrumentError: If any field is not a number.
        """
        reply = self.query(command)
        try:
            return [float(field) for field in reply.split(",") if field.strip()]
        except ValueError:
            raise InstrumentError(
                f"Expected comma-separated numbers in reply to '{command}', got "
                f"{reply!r}.",
                instrument=self,
            )

    def query_boolean(self, command):
        """Send a command and return its reply as True or False.

        Understands the several ways instruments answer a yes/no query: '1'/'0',
        'ON'/'OFF', and the float forms some return instead.

        :raises InstrumentError: If the reply is not recognizable as either.
        """
        reply = self.query(command).strip().upper()
        if reply in ("1", "ON", "TRUE"):
            return True
        if reply in ("0", "OFF", "FALSE"):
            return False
        try:
            return bool(int(float(reply)))
        except ValueError:
            raise InstrumentError(
                f"Expected an on/off reply to '{command}', got {reply!r}.",
                instrument=self,
            )

    # Waiting

    def wait_until(self, condition, timeout=60.0, interval=0.5, description=None):
        """Poll ``condition`` until it returns True.

        :param condition: A callable taking no arguments and returning a bool.
        :param timeout: Seconds to wait before giving up.
        :param interval: Seconds between checks.
        :param description: What is being waited for, used in the error message.
        :raises InstrumentTimeoutError: If the condition never becomes true.
        """
        deadline = time.monotonic() + float(timeout)
        while not condition():
            if time.monotonic() > deadline:
                raise InstrumentTimeoutError(
                    f"Timed out after {timeout} s waiting for "
                    f"{description or 'a condition'} on {self!r}."
                )
            time.sleep(interval)

    def is_responding(self):
        """Returns True if the instrument still answers.

        Useful when a measurement has been running unattended and you want to
        know the thing is still on the other end of the cable, which an open
        handle does not tell you: a GPIB session survives the instrument being
        switched off underneath it.

        This base cannot ask a question without knowing the command language,
        so it reports whether the connection is open. A driver that knows a
        harmless question should override this and ask it.
        """
        return bool(self._transport.is_open)

    # Connection lifetime

    def close(self):
        """Release the connection to the instrument."""
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()
        return False

    def __repr__(self):
        return f"{type(self).__name__}({self._transport!r})"


class ScpiInstrument(Instrument):
    """An instrument speaking the IEEE 488.2 common command set.

    Provides the starred commands every SCPI instrument implements, so no driver
    reimplements them. Instrument-specific commands belong in the driver.
    """

    #: Query that returns the oldest entry in the instrument's error queue.
    ERROR_QUERY = "SYST:ERR?"

    def identify(self):
        """Returns the instrument's identification string (``*IDN?``)."""
        return self.query("*IDN?")

    def is_responding(self):
        """Returns True if the instrument answers ``*IDN?``.

        Asked rather than assumed, so an instrument switched off underneath an
        open session is reported as gone instead of as connected. Any failure
        counts as not responding, since this is asked exactly when something is
        suspected to be wrong.
        """
        try:
            return bool(self.identify())
        except Exception:
            return False

    def verify_identity(self):
        """Check the instrument is the model this driver expects.

        :return: The identification string.
        :raises InstrumentError: If IDENTIFIER is set and does not appear in it.
        """
        identity = self.identify()
        if self.IDENTIFIER and self.IDENTIFIER.lower() not in identity.lower():
            raise InstrumentError(
                f"Expected a {self.IDENTIFIER} but the instrument identifies "
                f"itself as '{identity}'. Check the address is right.",
                instrument=self,
            )
        return identity

    def reset(self):
        """Put the instrument back to its power-on defaults (``*RST``)."""
        self.write("*RST")

    def clear_status(self):
        """Clear the status registers and the error queue (``*CLS``)."""
        self.write("*CLS")

    def self_test(self):
        """Run the instrument's self test (``*TST?``).

        :return: 0 if the instrument passed.
        """
        return self.query_integer("*TST?")

    @property
    def status_byte(self):
        """Returns the status byte register (``*STB?``)."""
        return self.query_integer("*STB?")

    # Error queue

    def next_error(self):
        """Read the oldest entry from the instrument's error queue.

        :return: A tuple of (code, message). Code 0 means the queue is empty.
        """
        reply = self.query(self.ERROR_QUERY)
        code, _, message = reply.partition(",")
        try:
            code = int(float(code))
        except ValueError:
            return 0, reply.strip()
        return code, message.strip().strip('"')

    def errors(self):
        """Drain the instrument's error queue.

        :return: A list of (code, message) tuples, oldest first.
        """
        found = []
        while True:
            code, message = self.next_error()
            if code == 0:
                return found
            found.append((code, message))
            if len(found) > 100:
                return found

    def check_errors(self):
        """Raise if the instrument has anything in its error queue.

        Call after a configuration step to find out immediately that a command
        was rejected, rather than discovering it in the data.

        :raises InstrumentError: If the queue is not empty.
        """
        found = self.errors()
        if found:
            reported = ", ".join(f"{code}: {message}" for code, message in found)
            raise InstrumentError(
                f"{type(self).__name__} reported {reported}.",
                code=found[0][0],
                instrument=self,
            )
