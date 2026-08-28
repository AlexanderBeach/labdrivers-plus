"""Instruments the package has no driver for.

Most instruments answer SCPI over one of the connections labdrivers already
speaks, so an instrument with no driver here is still perfectly usable. This
builds one from a description instead of from a module: you give it the command
that reads a setting and the command that writes it, and it behaves like any
other driver.

That matters because everything downstream works on driver classes. Building a
real class, with real properties, means the panel drawing, the locks, the
validation and the client all treat a described instrument exactly as they
treat a written one, and none of them needs a special case.

A setting is described like this::

    {"name": "voltage", "query": "VOLT?", "write": "VOLT {}",
     "unit": "V", "type": "float", "minimum": 0, "maximum": 30}

Leave out ``query`` for something write-only, or ``write`` for something that
can only be read. The braces in ``write`` are where the value goes, and a
command with no braces is sent unchanged, which is what an action is.
"""

import string

from ..core.errors import RangeError
from ..core.instrument import Instrument, ScpiInstrument
from ..core.validators import (
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)

TYPES = ("float", "integer", "boolean", "string")

# The console, which every described instrument keeps so that a command nobody
# described can still be sent. Replacing it would take that away.
CONSOLE = ("send", "ask", "DESCRIPTION")


class GenericInstrument(ScpiInstrument):
    """An instrument described rather than written.

    Beyond whatever settings it was described with, it always offers the raw
    console, so a command nobody thought to describe can still be sent.
    """

    #: Filled in by :func:`build` with the description the class came from.
    DESCRIPTION = {}

    def send(self, command):
        """Send one command exactly as typed, and return nothing.

        This is the escape hatch. Anything the description does not cover can
        still be done, which is the point of being able to add an instrument
        the package has never heard of.
        """
        self.write(command)

    def ask(self, command):
        """Send one command exactly as typed and return its reply."""
        return self.query(command)


def parse(reply, kind):
    """Turn an instrument's reply into the type a setting was described as."""
    text = str(reply).strip()
    if kind == "float":
        return float(text)
    if kind == "integer":
        return int(float(text))
    if kind == "boolean":
        try:
            return bool(int(float(text)))
        except ValueError:
            return check_boolean(text, "reply")
    return text


def validate(value, setting):
    """Check a value against the description, in the usual house wording.

    The same validators every written driver uses, so an instrument added by
    describing it rejects a bad value with the same sentence as one that has a
    module of its own.
    """
    name = setting.get("label") or setting["name"].replace("_", " ")
    kind = setting.get("type", "float")
    unit = f" {setting['unit']}" if setting.get("unit") else ""

    if setting.get("choices"):
        return check_choice(value, setting["choices"], name)
    if kind == "boolean":
        return int(check_boolean(value, name))

    # A description naming only one end still means it. Filling the other end
    # with an infinity keeps the check rather than dropping it, which is what a
    # supply described as never going below zero is relying on.
    minimum = setting.get("minimum")
    maximum = setting.get("maximum")
    minimum = float("-inf") if minimum is None else minimum
    maximum = float("inf") if maximum is None else maximum
    if kind == "integer":
        return check_integer_range(value, minimum, maximum, name, unit)
    if kind == "float":
        return check_range(value, minimum, maximum, name, unit)
    return str(value)


def make_property(setting):
    """Build one property from a setting description."""
    query = setting.get("query")
    write = setting.get("write")
    kind = setting.get("type", "float")

    getter = None
    if query:

        def getter(self):
            return parse(self.query(query), kind)

        unit = f", in {setting['unit']}" if setting.get("unit") else ""
        getter.__doc__ = (
            setting.get("help")
            or f"Returns the {setting['name'].replace('_', ' ')}{unit}."
        )
        # Stated rather than left to be read back out of the sentence above,
        # since a description gives the unit as a symbol and not as a word.
        getter.unit = setting.get("unit", "")

    setter = None
    if write:

        def setter(self, value):
            self.write(write.format(validate(value, setting)))

    return property(getter, setter)


def make_action(action):
    """Build one no-argument method from an action description.

    It takes nothing but self, because a method with any parameter at all is
    not offered as a button by :func:`introspect.describe_actions`.
    """
    command = action["command"]

    def method(self):
        self.write(command)

    method.__name__ = action["name"]
    method.__doc__ = action.get("help") or f"Sends {action['command']}."
    return method


def check_name(name, kind):
    """Reject a name that is unusable or that would shadow the driver's own.

    A description becomes a real class, so a setting called 'write' or an
    action called 'close' does not sit alongside the machinery, it replaces it.
    An action called 'close' would mean that removing the instrument sent a
    command to the hardware and left the connection open.

    The line is drawn at Instrument, which owns the transport and the reading
    and writing. What ScpiInstrument adds on top, such as reset or identify, is
    a convenience wrapping one standard command, and an instrument that spells
    that command differently is entitled to say so.

    :raises RangeError: If the name cannot be used.
    """
    if not str(name).isidentifier():
        raise RangeError(
            f"The name of a {kind} has to work as a Python attribute, but got "
            f"{name!r}."
        )
    if hasattr(Instrument, name) or name in CONSOLE:
        raise RangeError(
            f"The {kind} cannot be called '{name}', because that is how every "
            f"instrument is talked to and describing one by that name would "
            f"replace the machinery. Choose another name."
        )


def check_template(template, name):
    """Reject a write command the value cannot be substituted into.

    :raises RangeError: If the command has anything other than one plain {}.
    """
    try:
        fields = [
            field
            for _, field, _, _ in string.Formatter().parse(template)
            if field is not None
        ]
    except ValueError as failure:
        raise RangeError(f"The write command for '{name}' is not usable: {failure}.")
    if fields != [""]:
        raise RangeError(
            f"The write command for '{name}' needs exactly one plain {{}} where "
            f"the value goes, but got {template!r}. A command containing other "
            f"braces has to have them doubled, as {{{{ and }}}}."
        )


def check_description(settings, actions):
    """Reject a description that cannot work, before anything is connected.

    :raises RangeError: If a setting or action is missing what it needs.
    """
    seen = set()
    for setting in settings:
        name = setting.get("name", "")
        check_name(name, "setting")
        if name in seen:
            raise RangeError(f"There are two settings called '{name}'.")
        seen.add(name)
        if not setting.get("query") and not setting.get("write"):
            raise RangeError(
                f"The setting '{name}' needs a query command, a write command, "
                f"or both."
            )
        kind = setting.get("type", "float")
        if kind not in TYPES:
            raise RangeError(
                f"The type of '{name}' can be "
                f"{', '.join(TYPES[:-1])} or {TYPES[-1]}, but got {kind!r}."
            )
        if setting.get("write"):
            check_template(setting["write"], name)

    for action in actions:
        name = action.get("name", "")
        check_name(name, "action")
        if name in seen:
            raise RangeError(f"'{name}' is both a setting and an action.")
        seen.add(name)
        if not action.get("command"):
            raise RangeError(f"The action '{name}' needs a command to send.")


def build(settings=(), actions=(), name="Generic"):
    """Returns a driver class with the described settings as properties.

    :param settings: Setting descriptions, as above.
    :param actions: Action descriptions, each a name and a command.
    :param name: Class name, which is what the web page calls it.
    :raises RangeError: If the description is not usable.
    """
    settings, actions = list(settings), list(actions)
    check_description(settings, actions)

    body = {"DESCRIPTION": {"settings": settings, "actions": actions}}
    for setting in settings:
        body[setting["name"]] = make_property(setting)
    for action in actions:
        body[action["name"]] = make_action(action)

    built = type(name, (GenericInstrument,), body)
    built.__doc__ = f"{name}, built from a description rather than written."
    return built
