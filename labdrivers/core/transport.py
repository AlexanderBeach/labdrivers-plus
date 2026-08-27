"""Ways of talking to an instrument.

A transport moves strings to and from a device and knows nothing about what
they mean. Drivers own the command language, and transports own the wire.

Three transports are provided. VISA covers GPIB, USB, serial and TCPIP, a raw
TCP socket covers the Oxford cryogenics controllers, and a recording double
stands in for an instrument when there is no hardware to talk to.
:func:`open_transport` picks between them using the arguments a driver was
constructed with.

Timeouts are in **seconds** everywhere in labdrivers. VISA counts them in
milliseconds internally, but that conversion happens here, once, rather than
in each driver.
"""

import logging
import socket

from .errors import ConnectionFailure, InstrumentTimeoutError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_SOCKET_PORT = 7020
DEFAULT_BYTES_TO_READ = 2048

# One resource manager is shared by every VISA transport, created on first use.
# It must not be built at import time: constructing it loads the NI VISA driver,
# so a module-level call makes merely importing labdrivers touch hardware and
# fail on any machine without VISA installed.
_resource_manager = None


def get_resource_manager():
    """Return the shared pyvisa ResourceManager, creating it on first use."""
    global _resource_manager
    if _resource_manager is None:
        try:
            import pyvisa
        except ImportError:
            raise ConnectionFailure(
                "pyvisa is not installed, so VISA instruments cannot be reached. "
                "Install it with 'pip install pyvisa'."
            )
        try:
            _resource_manager = pyvisa.ResourceManager()
        except OSError as err:
            raise ConnectionFailure(
                "Could not load the VISA library. Is the National Instruments "
                f"VISA driver installed? Original error: {err}"
            )
    return _resource_manager


class Transport:
    """Base class for every way of reaching an instrument."""

    def write(self, command):
        """Send a command that expects no reply."""
        raise NotImplementedError

    def read(self):
        """Read one reply."""
        raise NotImplementedError

    def query(self, command):
        """Send a command and return its reply, stripped of whitespace."""
        self.write(command)
        return self.read()

    def query_binary(self, command, datatype="B", is_big_endian=False):
        """Send a command and read back an IEEE 488.2 binary block.

        Oscilloscopes and network analyzers return traces this way: a header
        giving the byte count, then the raw samples. Transferring a long trace
        as text is many times slower and no more accurate.

        :param datatype: struct code for one sample, e.g. 'B' for unsigned
                         byte, 'h' for signed 16-bit, 'f' for float.
        :param is_big_endian: Byte order the instrument sends.
        :return: A list of numbers.
        """
        raise NotImplementedError

    def close(self):
        """Release the connection."""

    @property
    def is_open(self):
        """Returns True while the connection can be used."""
        return True

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()
        return False


class VisaTransport(Transport):
    """Talks to an instrument through pyvisa.

        Covers GPIB, USB, serial and TCPIP alike, and the resource name decides which
    of them is used.

        :param resource_name: VISA resource string, e.g. 'GPIB0::24::INSTR'.
        :param timeout: Seconds to wait for a reply (default: 10).
        :param read_termination: Character ending a reply, if the instrument needs
                                 one set explicitly (the Oxford ITC 503 wants '\\r').
        :param write_termination: Character appended to each command.
        :param baud_rate: Serial line speed, for RS-232 resources.
    """

    def __init__(
        self,
        resource_name,
        timeout=DEFAULT_TIMEOUT,
        read_termination=None,
        write_termination=None,
        baud_rate=None,
        **resource_options,
    ):
        self.resource_name = str(resource_name)
        self.timeout = float(timeout)

        try:
            self._resource = get_resource_manager().open_resource(
                self.resource_name, **resource_options
            )
        except ConnectionFailure:
            raise
        except Exception as err:
            raise ConnectionFailure(
                f"Could not open VISA resource '{self.resource_name}'. Check that "
                "the instrument is powered on, connected, and set to the address "
                f"you expect. Original error: {err}"
            )

        # pyvisa counts the timeout in milliseconds.
        self._resource.timeout = self.timeout * 1000.0
        if read_termination is not None:
            self._resource.read_termination = read_termination
        if write_termination is not None:
            self._resource.write_termination = write_termination
        if baud_rate is not None:
            self._resource.baud_rate = int(baud_rate)

    def write(self, command):
        logger.debug("%s <- %s", self.resource_name, command)
        self._resource.write(command)

    def read(self):
        try:
            reply = self._resource.read()
        except Exception as err:
            if "timeout" in type(err).__name__.lower() or "VI_ERROR_TMO" in str(err):
                raise InstrumentTimeoutError(
                    f"'{self.resource_name}' did not reply within {self.timeout} s. "
                    "The instrument may be busy, or the command may not produce a "
                    "response."
                )
            raise
        logger.debug("%s -> %s", self.resource_name, reply.strip())
        return reply.strip()

    def query(self, command):
        logger.debug("%s <- %s", self.resource_name, command)
        try:
            reply = self._resource.query(command)
        except Exception as err:
            if "timeout" in type(err).__name__.lower() or "VI_ERROR_TMO" in str(err):
                raise InstrumentTimeoutError(
                    f"'{self.resource_name}' did not reply to '{command}' within "
                    f"{self.timeout} s."
                )
            raise
        logger.debug("%s -> %s", self.resource_name, reply.strip())
        return reply.strip()

    def query_binary(self, command, datatype="B", is_big_endian=False):
        logger.debug("%s <- %s (binary)", self.resource_name, command)
        return self._resource.query_binary_values(
            command,
            datatype=datatype,
            is_big_endian=is_big_endian,
            container=list,
        )

    def close(self):
        if self._resource is not None:
            self._resource.close()
            self._resource = None

    @property
    def is_open(self):
        return self._resource is not None

    def __repr__(self):
        return f"VisaTransport({self.resource_name!r})"


class SocketTransport(Transport):
    """Talks to an instrument over a raw TCP socket.

    Used by the Oxford Triton 200 and Mercury controllers, which speak a
    line-based protocol on a bare socket rather than through VISA.

    The socket is opened on first use and kept open, reconnecting if the far end
    drops it. Replies are read until the terminator arrives rather than taking
    whatever a single recv() happens to return, which is what truncated long
    replies before.

    :param ip_address: Address of the instrument.
    :param port: TCP port. The Mercury uses 7020 and the Triton uses 33576.
    :param timeout: Seconds to wait for a reply (default: 10).
    :param terminator: Line ending the instrument uses (default: '\\r\\n').
    """

    def __init__(
        self,
        ip_address,
        port=DEFAULT_SOCKET_PORT,
        timeout=DEFAULT_TIMEOUT,
        bytes_to_read=DEFAULT_BYTES_TO_READ,
        terminator="\r\n",
        encoding="ascii",
    ):
        self.address = (str(ip_address), int(port))
        self.timeout = float(timeout)
        self.bytes_to_read = int(bytes_to_read)
        self.terminator = terminator
        self.encoding = encoding
        self._socket = None

    def _ensure_open(self):
        if self._socket is None:
            try:
                self._socket = socket.create_connection(self.address, self.timeout)
            except OSError as err:
                raise ConnectionFailure(
                    f"Could not connect to {self.address[0]}:{self.address[1]}. "
                    "Check the instrument is on the network and that the port is "
                    "not already held by another program. On Windows, "
                    "'netstat -ona | findstr <port>' shows what is holding it. "
                    f"Original error: {err}"
                )
            self._socket.settimeout(self.timeout)
        return self._socket

    def write(self, command):
        if not command.endswith(self.terminator):
            command += self.terminator
        logger.debug("%s <- %s", self.address[0], command.strip())
        connection = self._ensure_open()
        try:
            connection.sendall(command.encode(self.encoding))
        except OSError:
            # The far end may have dropped an idle connection, so try once more.
            self.close()
            self._ensure_open().sendall(command.encode(self.encoding))

    def read(self):
        connection = self._ensure_open()
        chunks = []
        while True:
            try:
                chunk = connection.recv(self.bytes_to_read)
            except socket.timeout:
                raise InstrumentTimeoutError(
                    f"{self.address[0]}:{self.address[1]} did not reply within "
                    f"{self.timeout} s."
                )
            if not chunk:
                break
            chunks.append(chunk)
            if chunks and b"".join(chunks).endswith(self.terminator.encode()):
                break
        reply = b"".join(chunks).decode(self.encoding).strip()
        logger.debug("%s -> %s", self.address[0], reply)
        return reply

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    @property
    def is_open(self):
        return self._socket is not None

    def __repr__(self):
        return f"SocketTransport({self.address[0]!r}, {self.address[1]})"


class RecordingTransport(Transport):
    """A fake instrument that records every command it is sent.

    It makes the exact bytes a driver puts on the wire visible, which is the
    only way to catch a command mistranscribed from a manual without plugging
    in the instrument.

    :param responses: What to reply to queries. Either a mapping of command to
                      reply, or a callable taking the command and returning the
                      reply. Commands are matched after stripping whitespace.
    :param default: Reply used for any query not covered by ``responses``. If
                    left as None, an unexpected query raises, so tests fail
                    loudly rather than silently reading an empty string.
    """

    def __init__(self, responses=None, default=None):
        self.commands = []
        self.responses = responses or {}
        self.default = default
        self.closed = False
        # Whether each recorded command was a write or a query. Recorded rather
        # than inferred, because a trailing '?' does not identify a query on
        # every instrument, and the SR830 writes its queries as 'OUTP? 1'.
        self._kinds = []

    def write(self, command):
        self.commands.append(command.strip())
        self._kinds.append("write")

    def read(self):
        return ""

    def query(self, command):
        command = command.strip()
        self.commands.append(command)
        self._kinds.append("query")

        if callable(self.responses):
            return str(self.responses(command))
        if command in self.responses:
            return str(self.responses[command])
        if self.default is not None:
            return str(self.default)
        raise ConnectionFailure(
            f"The test transport has no reply configured for '{command}'. Add it "
            "to the responses mapping, or set a default."
        )

    def query_binary(self, command, datatype="B", is_big_endian=False):
        """Return a canned binary reply, which must be a list of numbers."""
        command = command.strip()
        self.commands.append(command)
        self._kinds.append("query")

        reply = (
            self.responses(command)
            if callable(self.responses)
            else (self.responses.get(command, self.default))
        )
        if reply is None:
            raise ConnectionFailure(
                f"The test transport has no binary reply configured for "
                f"'{command}'. Add a list of numbers to the responses mapping."
            )
        return list(reply)

    def close(self):
        self.closed = True

    @property
    def is_open(self):
        return not self.closed

    @property
    def last_command(self):
        """Returns the most recent command sent, or None."""
        return self.commands[-1] if self.commands else None

    @property
    def writes(self):
        """Returns the only the commands that set something, ignoring queries.

        Useful for asserting the exact sequence a configuration step emits,
        without the interleaved queries a driver makes to find out what state
        the instrument is in.
        """
        return [
            command
            for command, kind in zip(self.commands, self._kinds)
            if kind == "write"
        ]

    @property
    def queries(self):
        """Returns the only the commands that asked the instrument something."""
        return [
            command
            for command, kind in zip(self.commands, self._kinds)
            if kind == "query"
        ]

    def clear(self):
        """Forget every recorded command."""
        self.commands = []
        self._kinds = []

    def __repr__(self):
        return f"RecordingTransport({len(self.commands)} commands)"


def open_transport(
    resource_name=None,
    gpib_address=None,
    ip_address=None,
    port=None,
    transport=None,
    **kwargs,
):
    """Build whichever transport the given arguments describe.

    Lets every driver accept the same connection arguments, so choosing between
    GPIB, serial, TCPIP and a raw socket is a constructor argument rather than a
    different module.

    :param resource_name: A full VISA resource string.
    :param gpib_address: A GPIB primary address, as shorthand for a resource
                         string of 'GPIB0::<address>::INSTR'.
    :param ip_address: An address for a raw socket connection.
    :param port: TCP port to go with ``ip_address``.
    :param transport: An already-built transport, returned unchanged. This is
                      how tests inject a RecordingTransport.
    :raises ConnectionFailure: If nothing identifying an instrument was given.
    """
    if transport is not None:
        return transport

    if resource_name is not None:
        return VisaTransport(resource_name, **kwargs)

    if gpib_address is not None:
        board = kwargs.pop("gpib_board", 0)
        return VisaTransport(f"GPIB{board}::{int(gpib_address)}::INSTR", **kwargs)

    if ip_address is not None:
        socket_arguments = {
            key: value
            for key, value in kwargs.items()
            if key in ("timeout", "bytes_to_read", "terminator", "encoding")
        }
        if port is not None:
            socket_arguments["port"] = port
        return SocketTransport(ip_address, **socket_arguments)

    raise ConnectionFailure(
        "No instrument was specified. Pass one of gpib_address=, resource_name=, "
        "or ip_address= (with port=) to say how to reach it."
    )
