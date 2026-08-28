"""Reaching instruments that a labdrivers server is holding open.

This is what a measurement notebook imports. It hands back the ordinary driver
object, not a stand-in for one, so everything a driver can do it can still do
and the notebook code is the same either way:

    from labdrivers.client import connect

    lockin = connect("lockin")
    lockin.time_constant = 0.3
    x, y = lockin.measure()

The difference is where the connection lives. The server holds it, so the
kernel can be restarted underneath this without the instrument noticing, and
somebody at another machine can use the same instrument at the same time.

Only the standard library is used here, so a measurement computer needs nothing
installed beyond labdrivers itself.
"""

import inspect
import json
import urllib.error
import urllib.parse
import urllib.request

from .core.errors import ConnectionFailure, LabdriversError
from .core.instrument import Instrument
from .core.transport import DEFAULT_SERVER, RemoteTransport, as_address

DEFAULT_TIMEOUT = 10.0


def _address(server):
    """Returns a server address with a scheme on the front."""
    address = as_address(server)
    return address if "://" in address else f"http://{address}"


def request(server, path, method="GET", body=None, timeout=DEFAULT_TIMEOUT):
    """Make one call to the server and return the decoded reply.

    :raises ConnectionFailure: If the server cannot be reached.
    :raises LabdriversError: If the server refuses the request.
    """
    url = f"{_address(server)}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    call = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise LabdriversError(detail)
    except urllib.error.URLError as error:
        raise ConnectionFailure(
            f"No labdrivers server answered at {_address(server)}. Start one with "
            f"'labdrivers-server', or pass server= if it runs on another machine. "
            f"The underlying error was {error.reason}."
        )


def _own_arguments(driver):
    """Returns the constructor arguments a driver names for itself.

    Every driver takes its connection through the base class and names anything
    else in its own __init__: a Mercury's axes and field limits, a rotation
    probe's calibration, an ITC's default sensor. Those decide what the driver
    will refuse, so a magnet registered on the server at two tesla has to reach
    a notebook as a two-tesla magnet rather than as the six-tesla default.
    """
    named = set()
    for cls in driver.__mro__:
        if cls is Instrument:
            break
        own = cls.__dict__.get("__init__")
        if own is None:
            continue
        for parameter in inspect.signature(own).parameters.values():
            if parameter.default is not inspect.Parameter.empty:
                named.add(parameter.name)
    return named


def connect(name, server=DEFAULT_SERVER, timeout=DEFAULT_TIMEOUT):
    """Returns a driver for an instrument the server holds open.

    The server is asked which driver the instrument uses, and that class is
    built here on a RemoteTransport, so the object handed back is a real
    Sr830 or Keithley6221 with its own docstrings and autocompletion.

    An instrument the server holds as a description rather than a driver comes
    back the same way. Its class is built here from the same description the
    server used, so it too has real properties with real docstrings.

    Anything the instrument was registered with that decides what the driver
    will refuse, such as a magnet's field limits, is carried across, so a script
    running on another machine is held to the same bounds as one running beside
    the instrument.

    :param name: Name the instrument is registered under.
    :param server: Host and port of the server.
    :raises LabdriversError: If no instrument goes by that name.
    """
    from .server import generic
    from .server.drivers import GENERIC, find_driver

    described = request(
        server, f"/api/instruments/{urllib.parse.quote(name)}", timeout=timeout
    )
    if described["driver"] == GENERIC:
        description = described.get("description") or {}
        driver = generic.build(
            description.get("settings", ()), description.get("actions", ())
        )
    else:
        driver = find_driver(described["driver"])

    # Whatever the instrument was registered with that shapes what the driver
    # allows, carried across so that the bounds are the same at both ends.
    connection = described.get("connection") or {}
    settings = {
        key: value for key, value in connection.items() if key in _own_arguments(driver)
    }
    return driver(transport=RemoteTransport(name, server, timeout=timeout), **settings)


class Server:
    """A labdrivers server, for looking after what it holds.

        lab = Server("cryostat-pc:8000")
        lab.instruments()
        lockin = lab.connect("lockin")

    :param address: Host and port, defaulting to a server on this machine.
    """

    def __init__(self, address=DEFAULT_SERVER, timeout=DEFAULT_TIMEOUT):
        self.address = address
        self.timeout = timeout

    def _call(self, path, method="GET", body=None):
        return request(self.address, path, method, body, self.timeout)

    def instruments(self):
        """Returns a line for each instrument, with its driver and status."""
        return self._call("/api/instruments")

    def drivers(self):
        """Returns every driver this server can offer."""
        return self._call("/api/drivers")

    def scan(self):
        """Returns the VISA resources on the server's machine.

        Each carries the reply to *IDN? and the drivers that match it, which is
        how an instrument is identified without anyone looking up an address.
        """
        return self._call("/api/scan")

    def connect(self, name):
        """Returns a driver for one of the instruments held here."""
        return connect(name, self.address, self.timeout)

    def add(self, name, driver, settings=(), actions=(), **connection):
        """Register an instrument and open it.

            lab.add("lockin", "Sr830", gpib_address=8)

        An instrument with no driver in the package is added by describing what
        its commands are, which needs no module written for it:

            lab.add(
                "psu", "Generic", resource_name="GPIB0::5::INSTR",
                settings=[{"name": "voltage", "query": "VOLT?",
                           "write": "VOLT {}", "unit": "V",
                           "minimum": 0, "maximum": 30}],
                actions=[{"name": "reset", "command": "*RST"}],
            )

        :param name: What to call it.
        :param driver: Class name of the driver, or 'Generic' to describe one.
        :param settings: Setting descriptions, for a described instrument.
        :param actions: Action descriptions, for a described instrument.
        :param connection: The driver's usual connection arguments.
        """
        return self._call(
            "/api/instruments",
            "POST",
            {
                "name": name,
                "driver": driver,
                "connection": connection,
                "settings": list(settings),
                "actions": list(actions),
            },
        )

    def describe(self, name, settings=(), actions=()):
        """Change what a described instrument offers, keeping it connected."""
        return self._call(
            f"/api/instruments/{urllib.parse.quote(name)}/describe",
            "POST",
            {"settings": list(settings), "actions": list(actions)},
        )

    def remove(self, name):
        """Close an instrument and forget it.

        The instrument is left exactly as it stands. Nothing is reset, no field
        is dropped and no heater is turned off.
        """
        return self._call(f"/api/instruments/{urllib.parse.quote(name)}", "DELETE")

    def reconnect(self, name):
        """Close and reopen one instrument, for after it has been power-cycled."""
        return self._call(
            f"/api/instruments/{urllib.parse.quote(name)}/reconnect", "POST"
        )

    def read(self, name):
        """Returns every readable property of one instrument, in a dict."""
        return self._call(f"/api/instruments/{urllib.parse.quote(name)}/values")

    def __repr__(self):
        return f"Server({self.address!r})"
