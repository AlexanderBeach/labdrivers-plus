"""The part of the server that owns the instruments.

One process holds every connection open, so a notebook restarting does not drop
the GPIB session and two people can use one cryostat without fighting over it.
Each instrument gets a lock, because a GPIB board has one owner at a time and a
VISA session is not safe to use from two threads at once. Commands from the web
page and from every notebook queue on that lock rather than interleaving.

Removing an instrument here closes the connection and nothing else. It does not
reset the instrument, drop a field or turn off a heater, because a server that
changed hardware state when a client went away would be far more dangerous than
one that did not.
"""

import contextlib
import json
import logging
import threading
import time

from ..core.errors import (
    ConnectionFailure,
    InstrumentTimeoutError,
    LabdriversError,
)
from . import generic, introspect
from .drivers import GENERIC, find_driver, match_identity

logger = logging.getLogger(__name__)

# How long to wait for *IDN? when scanning. Short, because a resource that is
# busy or does not speak SCPI should not hold the scan up.
SCAN_TIMEOUT = 1.0

# Longest a request waits for an instrument that is already busy. Long enough
# for a slow sweep of readings to finish, short enough that a wedged instrument
# gives the server back rather than taking every thread with it.
LOCK_TIMEOUT = 30.0

# How much sooner than its client the server gives up waiting for an
# instrument. If the server outwaits the client, a command the caller has
# already abandoned still reaches the instrument, and the retry that follows
# applies it a second time. Losing the race by a margin makes a client
# timeout mean the command did not run, which is what makes retrying safe.
LOCK_MARGIN = 2.0

# How long a machine counts as still using an instrument after its last
# command. Long enough to cover the gaps in a slow sweep, short enough that
# somebody who has gone home stops showing up.
USER_MEMORY = 300.0


def sendable(value):
    """Whether a connection argument can be sent to a client as it stands.

    A transport is a live object and cannot travel, while a magnet's field
    limits and axis list are a dict and a tuple that can. Asking JSON keeps
    the ones that decide what a driver refuses, which is the point of sending
    them at all.
    """
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False


def resource_of(connection):
    """Returns the VISA resource a connection names, however it was given.

    gpib_address=8 and resource_name='GPIB0::8::INSTR' name the same
    instrument, and anything comparing connections has to see that.
    """
    if connection.get("resource_name"):
        return str(connection["resource_name"])
    if connection.get("gpib_address") is not None:
        board = connection.get("gpib_board", 0)
        return f"GPIB{board}::{int(connection['gpib_address'])}::INSTR"
    return None


class Entry:
    """One instrument the server holds open, and everything known about it."""

    def __init__(self, name, driver_name, connection, settings=(), actions=()):
        self.name = name
        self.driver_name = driver_name
        self.connection = dict(connection)
        # Only a described instrument uses these. A written driver states its
        # settings as properties, so there is nothing to carry here.
        self.settings = list(settings)
        self.actions = list(actions)
        self.lock = threading.Lock()
        self.instrument = None
        self.error = None
        # Kept so that opening the page shows what the instrument last said
        # rather than putting a fresh burst of traffic on a shared bus.
        self.last_read = None
        self.last_values = {}
        # Whether anything has actually been sent to this instrument yet.
        self.used = False
        # Which machines have used this instrument, and when each was last
        # seen. Somebody about to take a magnet over wants to know whether
        # anyone else is already driving it.
        self.users = {}

    @property
    def described(self):
        """Returns True if this instrument was described rather than written."""
        return self.driver_name == GENERIC

    def driver_class(self):
        """Returns the class for this entry, building it if it was described."""
        if self.described:
            return generic.build(self.settings, self.actions)
        return find_driver(self.driver_name)

    @property
    def status(self):
        """Returns 'connected', 'disconnected' or 'error'."""
        if self.error:
            return "error"
        return "connected" if self.instrument is not None else "disconnected"

    def open(self):
        """Build the driver and connect it, recording any failure.

        The class is looked up inside the try, so a driver name that does not
        exist is recorded against the entry like any other failure instead of
        escaping before anything has noted it.
        """
        try:
            self.instrument = self.driver_class()(**self.connection)
            self.error = None
        except Exception as failure:
            self.instrument = None
            self.error = str(failure)
            raise

    def hold_for(self, client_timeout=None):
        """Take the lock, giving up before the caller does.

        :param client_timeout: How long the caller is prepared to wait, if it
                               said. The wait is kept under that by a margin so
                               a caller that times out knows nothing was sent.
        """
        limit = LOCK_TIMEOUT
        if client_timeout:
            limit = min(limit, max(float(client_timeout) - LOCK_MARGIN, 0.5))
        return self.hold(limit)

    @contextlib.contextmanager
    def hold(self, timeout=LOCK_TIMEOUT):
        """Take this instrument's lock, giving up rather than queueing forever.

        One instrument that has stopped answering must not be able to wedge the
        server. Without a timeout the request threads pile up on the lock until
        the pool is exhausted and even the page that would let somebody fix it
        stops loading.

        :raises InstrumentTimeoutError: If the instrument is still busy.
        """
        if not self.lock.acquire(timeout=timeout):
            raise InstrumentTimeoutError(
                f"'{self.name}' was still busy after {timeout} s. Something "
                f"else is using it, or it has stopped answering. Try again, or "
                f"reconnect it."
            )
        try:
            yield self
        finally:
            self.lock.release()

    def close(self):
        """Release the connection, leaving the instrument as it stands."""
        if self.instrument is not None:
            try:
                self.instrument.close()
            except Exception as failure:
                logger.warning("Closing %s failed: %s", self.name, failure)
        self.instrument = None

    def note_user(self, who):
        """Record that a machine has just used this instrument."""
        if who:
            self.users[str(who)] = time.time()

    def recent_users(self, within=USER_MEMORY):
        """Returns the machines that have used this instrument lately."""
        now = time.time()
        return sorted(who for who, when in self.users.items() if now - when <= within)

    def summary(self):
        """Returns what the web page shows in the panel header."""
        return {
            "name": self.name,
            "driver": self.driver_name,
            "connection": {
                key: value for key, value in self.connection.items() if sendable(value)
            },
            "status": self.status,
            "error": self.error,
            "described": self.described,
            "age": None if self.last_read is None else time.time() - self.last_read,
            "users": self.recent_users(),
        }


class Hub:
    """Every instrument the server knows about.

    :param config: A Config to load from and save to, or None to keep the set
                   of instruments only for as long as the server runs.
    """

    def __init__(self, config=None):
        self.config = config
        self.entries = {}
        self._guard = threading.Lock()
        # Held while the file is written, so two instruments added at once
        # cannot each snapshot the set and then overwrite the other.
        self._writing = threading.Lock()
        # Server-wide settings from the configuration file, for the few things
        # the page needs to know about.
        self.settings = {}
        # Rows the file holds that this server did not take up, either because
        # they would not register or because it was told not to load them.
        # Kept so that saving writes the file back with them still in it.
        self.unloaded = []

    def snapshot(self):
        """Returns the registered entries as a list, taken under the guard.

        Iterating self.entries directly races with adding and removing, which
        raises RuntimeError in whichever thread happened to be listing.
        """
        with self._guard:
            return list(self.entries.values())

    # Membership

    def add(
        self,
        name,
        driver_name,
        connection,
        settings=(),
        actions=(),
        connect=True,
        persist=True,
    ):
        """Register an instrument and open it.

        :param name: What to call it, unique within the server.
        :param driver_name: Class name of the driver, as listed by /api/drivers.
        :param connection: Keyword arguments for the driver's constructor.
        :param settings: For a described instrument, its setting descriptions.
        :param actions: For a described instrument, its action descriptions.
        :param connect: Open it now. False registers it without touching
                        hardware, which is how a saved configuration is loaded
                        when an instrument happens to be switched off.
        :param persist: Write the configuration file afterwards. False while
                        that file is being read, so reading it never rewrites
                        it a line at a time.
        :raises LabdriversError: If the name is already taken, or is not
                                 usable as one.
        """
        if not isinstance(name, str) or not name.strip():
            raise LabdriversError(
                f"An instrument's name has to be text, but got {name!r}."
            )
        if "/" in name:
            # The name is the last part of the instrument's web address, so one
            # with a slash in it registers and then cannot be read, removed or
            # reconnected by anybody.
            raise LabdriversError(
                "An instrument's name is part of its web address, so it cannot "
                f"contain '/', but got {name!r}."
            )
        if driver_name == GENERIC:
            # Checked before anything is registered, because a description that
            # cannot work is a typing mistake rather than an absent instrument.
            generic.check_description(list(settings), list(actions))

        entry = Entry(name, driver_name, connection, settings, actions)
        with self._guard:
            if name in self.entries:
                raise LabdriversError(
                    f"An instrument called '{name}' is already registered. Remove "
                    f"it first, or choose another name."
                )
            self.entries[name] = entry

        if connect:
            # Opened while holding the entry's own lock. Publishing it first
            # and opening afterwards would let a request arriving in between
            # find no instrument and open a second session to the same address.
            with entry.hold():
                try:
                    entry.open()
                except (ConnectionFailure, InstrumentTimeoutError):
                    # An instrument that is switched off or unplugged keeps its
                    # registration, so the page can show why and offer a
                    # reconnect rather than losing what was typed.
                    pass
                except Exception:
                    # A mistake in what was asked for, such as a driver name
                    # that does not exist. Retrying cannot cure it, so the
                    # registration goes rather than coming back every restart.
                    with self._guard:
                        self.entries.pop(name, None)
                    raise
        if persist:
            self.save()
        return entry

    def remove(self, name):
        """Close an instrument and forget it."""
        entry = self.entry(name)
        with entry.hold():
            entry.close()
        with self._guard:
            self.entries.pop(name, None)
            # Anything set aside under the same name goes with it. Leaving the
            # row behind writes the instrument back into the file and opens it
            # again at the next start, which is not what removing it meant.
            self.unloaded = [row for row in self.unloaded if row.get("name") != name]
        self.save()

    def entry(self, name):
        """Returns the entry registered under a name.

        :raises LabdriversError: If nothing goes by that name.
        """
        try:
            return self.entries[name]
        except KeyError:
            with self._guard:
                known = ", ".join(str(key) for key in self.entries) or "none"
            raise LabdriversError(
                f"No instrument called '{name}' is registered. Registered: {known}."
            )

    def live(self, name):
        """Returns the open driver for a name, connecting if it has dropped.

        :raises ConnectionFailure: If it cannot be opened.
        """
        entry = self.entry(name)
        if entry.instrument is None:
            entry.open()
        return entry.instrument

    def reconnect(self, name):
        """Close and reopen one instrument.

        This is what to do after power-cycling something, since the old session
        refers to a device that is no longer there.
        """
        entry = self.entry(name)
        with entry.hold():
            entry.close()
            entry.open()
        return entry

    # Using an instrument

    def read(self, name, who=None):
        """Read the properties of one instrument."""
        entry = self.entry(name)
        entry.note_user(who)
        entry.used = True
        with entry.hold():
            instrument = self.live(name)
            values = introspect.read_settings(instrument)
            entry.last_values = values
            entry.last_read = time.time()
            return values

    def set(self, name, setting, value, who=None):
        """Set one property, and return what it reads back as.

        Reading back matters because an instrument often rounds what it is
        given to a step it actually supports, and the caller should see the
        value that took effect rather than the one requested.
        """
        entry = self.entry(name)
        entry.note_user(who)
        entry.used = True
        with entry.hold():
            instrument = self.live(name)
            described = {
                s["name"]: s for s in introspect.describe_settings(type(instrument))
            }
            if setting not in described:
                raise LabdriversError(
                    f"'{entry.driver_name}' has no setting called '{setting}'."
                )
            if not described[setting]["writable"]:
                raise LabdriversError(
                    f"The {described[setting]['label']} of '{name}' can be read but "
                    f"not set."
                )
            target, last = introspect.reach(instrument, setting)
            setattr(target, last, value)
            if described[setting]["readable"]:
                return introspect.as_json(getattr(target, last))
            return None

    def call(self, name, action, who=None):
        """Run one no-argument method on an instrument."""
        entry = self.entry(name)
        entry.note_user(who)
        entry.used = True
        with entry.hold():
            instrument = self.live(name)
            allowed = {a["name"] for a in introspect.describe_actions(type(instrument))}
            if action not in allowed:
                raise LabdriversError(
                    f"'{entry.driver_name}' has no action called '{action}'."
                )
            return introspect.as_json(getattr(instrument, action)())

    def io(self, name, payload, who=None):
        """Carry out one raw transport operation, for RemoteTransport.

        This is the path every remote driver takes, so a notebook talks to the
        instrument through exactly the commands its own driver builds.
        """
        kind = payload.get("kind")
        if kind not in ("write", "read", "query", "query_binary"):
            # Checked before the instrument is touched. Taking the lock and
            # opening a connection for an operation that cannot run means a
            # malformed request reaches the hardware.
            raise LabdriversError(
                "The operation can be 'write', 'read', 'query' or "
                f"'query_binary', but got {kind!r}."
            )
        entry = self.entry(name)
        entry.note_user(who)
        entry.used = True
        command = payload.get("command", "")
        with entry.hold_for(payload.get("timeout")):
            transport = self.live(name).transport
            if kind == "write":
                transport.write(command)
                return None
            if kind == "read":
                return transport.read()
            if kind == "query":
                return transport.query(command)
            if kind == "query_binary":
                return transport.query_binary(
                    command,
                    datatype=payload.get("datatype", "B"),
                    is_big_endian=payload.get("is_big_endian", False),
                )

    def redescribe(self, name, settings, actions):
        """Change what a described instrument offers, without disconnecting.

        The class is rebuilt and the existing transport moved onto it, so
        adding a setting to a running instrument does not drop its connection.

        :raises LabdriversError: If the instrument was not a described one, or
                                 the new description is not usable.
        """
        entry = self.entry(name)
        if not entry.described:
            raise LabdriversError(
                f"'{name}' uses the {entry.driver_name} driver, so its settings "
                f"come from that driver and cannot be described here."
            )
        with entry.hold():
            generic.check_description(settings, actions)
            entry.settings, entry.actions = list(settings), list(actions)
            if entry.instrument is not None:
                transport = entry.instrument.transport
                entry.instrument = entry.driver_class()(transport=transport)
        self.save()
        return entry

    # Checking the instruments are still there

    def check_health(self):
        """Ask each idle instrument whether it is still answering.

        An open handle is not the same as a live instrument: a GPIB session
        survives the box being switched off underneath it, so a panel can sit
        there reporting CONNECTED over numbers that stopped being true hours
        ago.

        An instrument that is busy is skipped rather than queued behind, for
        two reasons. It is demonstrably alive, since something is using it. And
        the lock serializes single calls rather than transactions, so a probe
        that waited its turn could land between the two commands of somebody
        else's measurement, which is exactly what this must never do.

        :return: The names of the instruments that were found not to answer.
        """
        gone = []
        for entry in self.snapshot():
            if entry.instrument is None:
                continue
            if not entry.lock.acquire(blocking=False):
                continue
            try:
                if not entry.used:
                    # Never spoken to, so there is nothing to conclude. A
                    # socket transport opens on its first command, and calling
                    # a never-opened one dead is worse than saying nothing.
                    continue
                if entry.instrument.is_responding():
                    entry.error = None
                else:
                    entry.error = (
                        "The instrument stopped answering. It may have been "
                        "switched off or unplugged. Reconnect it once it is "
                        "back."
                    )
                    gone.append(entry.name)
            except Exception as failure:
                entry.error = str(failure)
                gone.append(entry.name)
            finally:
                entry.lock.release()
        return gone

    def watch_health(self, interval, stopping):
        """Run check_health on a timer until asked to stop.

        :param interval: Seconds between rounds. Zero never checks at all.
        :param stopping: A threading.Event that ends the loop when set.
        """
        if not interval:
            return
        while not stopping.wait(interval):
            try:
                for name in self.check_health():
                    logger.warning("%s stopped answering", name)
            except Exception:
                logger.exception("The health check failed")

    # Finding hardware

    def scan(self):
        """List the VISA resources on this machine and identify what answers.

        Each resource is asked for *IDN? with a short timeout. Anything that
        does not answer is still listed, since the Oxford controllers and the
        rotator do not speak SCPI but are still there.
        """
        from ..core.transport import VisaTransport, get_resource_manager

        try:
            resources = get_resource_manager().list_resources()
        except Exception as failure:
            raise ConnectionFailure(
                f"VISA could not list the instruments on this machine. Check that "
                f"a VISA runtime is installed. The underlying error was {failure}."
            )

        # An instrument registered by GPIB address occupies a resource string
        # too. Comparing only resource_name misses those, and a scan that misses
        # one opens a second session to an instrument this server already holds
        # and sends *IDN? into somebody's running measurement.
        in_use = {resource_of(entry.connection) for entry in self.snapshot()}
        in_use.discard(None)

        found = []
        for resource in resources:
            identity, drivers = None, []
            if resource not in in_use:
                try:
                    with VisaTransport(resource, timeout=SCAN_TIMEOUT) as transport:
                        identity = transport.query("*IDN?")
                    drivers = match_identity(identity)
                except Exception:
                    # Not every instrument answers *IDN?, and one that is busy
                    # should not stop the rest of the scan being reported.
                    identity = None
            found.append(
                {
                    "resource": resource,
                    "identity": identity,
                    "suggested": drivers,
                    "in_use": resource in in_use,
                }
            )
        return found

    # Description and persistence

    def describe(self, name):
        """Returns the full description of one instrument, for its panel."""
        entry = self.entry(name)
        described = introspect.describe(entry.driver_class())
        instrument = entry.instrument
        if instrument is not None:
            described["settings"] = introspect.present_only(
                instrument, described["settings"]
            )
        described.update(entry.summary())
        # What it said last time, so the panel can be drawn with numbers on it
        # without anybody talking to the instrument.
        described["values"] = entry.last_values
        if entry.described:
            # The client needs these to rebuild the same class at its end.
            described["description"] = {
                "settings": entry.settings,
                "actions": entry.actions,
            }
        return described

    def summaries(self):
        """Returns the header line for every instrument."""
        return [entry.summary() for entry in self.snapshot()]

    def save(self):
        """Write the current set of instruments to the configuration file."""
        if self.config is None:
            return
        with self._writing:
            live = [
                {
                    "name": entry.name,
                    "driver": entry.driver_name,
                    **entry.connection,
                    **(
                        {"settings": entry.settings, "actions": entry.actions}
                        if entry.described
                        else {}
                    ),
                }
                for entry in self.snapshot()
            ]
            taken = {row["name"] for row in live}
            self.config.save(
                live + [row for row in self.unloaded if row.get("name") not in taken]
            )

    def load(self):
        """Register everything the configuration file lists, and open it.

        An instrument that fails to open is still registered, so the web page
        shows it with its error and a reconnect button rather than silently
        dropping it. One that cannot be registered at all, such as a driver
        name with a spelling mistake in a file somebody edited by hand, is set
        aside and written back untouched, because a name this server does not
        know is still something a person meant.
        """
        if self.config is None:
            return
        for row in self.config.load():
            saved = dict(row)
            name = saved.pop("name", None)
            driver = saved.pop("driver", None)
            settings = saved.pop("settings", [])
            actions = saved.pop("actions", [])
            if not name or not driver:
                self.unloaded.append(dict(row))
                continue
            try:
                self.add(name, driver, saved, settings, actions, persist=False)
            except Exception as failure:
                logger.warning("Could not register %s: %s", name, failure)
                self.unloaded.append(dict(row))

    def close(self):
        """Close every instrument, for shutting the server down."""
        for entry in self.snapshot():
            with entry.hold():
                entry.close()
