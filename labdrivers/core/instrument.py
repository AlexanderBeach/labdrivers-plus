"""The base every instrument driver is built on.

:class:`Instrument` owns the connection and the business of turning replies into
Python values. :class:`ScpiInstrument` adds the IEEE 488.2 common commands that
every SCPI instrument shares, so no driver has to redefine ``*IDN?`` or ``*RST``.

Anything specific to one instrument stays in that instrument's own driver.
That covers its command language, the ranges it accepts, and the way it
formats a reading.
"""

import logging
import time

from .errors import InstrumentError, InstrumentTimeoutError
from .transport import open_transport

logger = logging.getLogger(__name__)


class Instrument:
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
        """Return the instrument's identification string (``*IDN?``)."""
        return self.query("*IDN?")

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
        """Return the instrument to its power-on defaults (``*RST``)."""
        self.write("*RST")

    def clear_status(self):
        """Clear the status registers and the error queue (``*CLS``)."""
        self.write("*CLS")

    def self_test(self):
        """Run the instrument's self test (``*TST?``).

        :return: 0 if the instrument passed.
        """
        return self.query_integer("*TST?")

    def wait_to_continue(self):
        """Make the instrument finish pending commands before continuing
        (``*WAI``)."""
        self.write("*WAI")

    def set_operation_complete(self):
        """Ask the instrument to flag completion in its status byte (``*OPC``)."""
        self.write("*OPC")

    def operation_complete(self):
        """Block until pending operations finish, then return True (``*OPC?``)."""
        return self.query("*OPC?").strip() in ("1", "1.0")

    @property
    def event_status_register(self):
        """Returns the standard event status register (``*ESR?``).

        Reading the register clears it.
        """
        return self.query_integer("*ESR?")

    @property
    def event_status_enable(self):
        """Returns the standard event status enable mask (``*ESE``)."""
        return self.query_integer("*ESE?")

    @event_status_enable.setter
    def event_status_enable(self, mask):
        self.write(f"*ESE {int(mask)}")

    @property
    def service_request_enable(self):
        """Returns the service request enable mask (``*SRE``)."""
        return self.query_integer("*SRE?")

    @service_request_enable.setter
    def service_request_enable(self, mask):
        self.write(f"*SRE {int(mask)}")

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
