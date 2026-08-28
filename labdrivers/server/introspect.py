"""Turning a driver class into a description the web page can draw.

Nothing here knows about any particular instrument. Every driver in this
package states its settings as properties and documents each one in a sentence
starting with "Returns", so a control panel can be built by reading the class
rather than by writing a panel per instrument. A driver added next year gets a
web page without anyone drawing one.
"""

import inspect

# The one method a panel offers as a button.
SAFE_SHUTDOWN = "safe_shutdown"

# Properties that are plumbing rather than instrument state. The transport is
# how the driver is wired up, which is shown in the panel header already.
HIDDEN_SETTINGS = {
    "transport",
    # The IEEE 488.2 registers every SCPI instrument inherits. They are
    # protocol bookkeeping rather than anything a person watches a cryostat
    # for, and they remain reachable from a driver and from the console.
    "status_byte",
    # Cleared by the act of reading them. A panel read would take the
    # overload and error flags away from the measurement that needed them.
    "lockin_status",
    "error_status",
    # Remembered by the driver rather than reported by the controller, so a
    # panel would draw the default beside real measurements and read as though
    # somebody had checked the front panel.
    "heater_sensor",
    "auto_mode",
    "automatic_pid",
}


# Units as the docstrings spell them, and the symbol to show beside a value.
UNITS = {
    # Compound spellings first, so that a rate matches as a rate rather than as
    # the unit it is built out of. A magnet ramping at 0.5 T/min sitting on a
    # panel labelled 0.5 T reads as a field, next to the real one.
    "tesla per minute": "T/min",
    "amps per minute": "A/min",
    "kelvin per minute": "K/min",
    "oersted per second": "Oe/s",
    "volts per division": "V/div",
    "seconds per division": "s/div",
    "samples per second": "Sa/s",
    "degrees celsius": "degC",
    "amps": "A",
    "volts": "V",
    "ohms": "Ohm",
    "hertz": "Hz",
    "seconds": "s",
    "kelvin": "K",
    "tesla": "T",
    "degrees": "deg",
    "percent": "%",
    "decibels": "dB",
    "watts": "W",
    "millibar": "mbar",
}


def summarize(text):
    """Returns the first line of a docstring, with no trailing full stop."""
    if not text:
        return ""
    first = text.strip().splitlines()[0].strip()
    return first[:-1] if first.endswith(".") else first


def unit_of(documentation):
    """Returns the unit a getter's docstring names, or an empty string.

    The convention every driver follows is to end the sentence with "in amps"
    or "in hertz", so the unit can be shown beside the value without each
    property having to declare one separately. A rate names both halves, as
    "in tesla per minute", and those longer spellings are listed first in UNITS
    so that one of them matches before the unit it is built out of.
    """
    text = (documentation or "").lower()
    for spelling, symbol in UNITS.items():
        if f"in {spelling}" in text or f"in {spelling}." in text:
            return symbol
    return ""


def properties_in_order(driver):
    """Returns a driver's properties, most useful first.

    Alphabetical order is the wrong order for an instrument. It leads a lock-in
    panel with alarms and buffer count while X and Y sit somewhere below the
    fold. Walking the class before its bases, and taking each class in the
    order its properties were written, puts the driver's own settings first
    and in the groups their author arranged them in, leaving the inherited
    status registers at the bottom where they belong.
    """
    found, seen = [], set()
    for klass in driver.__mro__:
        readings, controls = [], []
        for name, value in vars(klass).items():
            if not isinstance(value, property) or name in seen:
                continue
            seen.add(name)
            # What the instrument reports, then what it is told. A front panel
            # is arranged this way for the same reason: the reading is what you
            # look at, and the control is what you reach for afterwards.
            (controls if value.fset else readings).append((name, value))
        found.extend(readings + controls)
    return found


def held_type(value):
    """Returns the class a property says it hands back, if it names one.

    Taken from the getter's return annotation rather than by calling it,
    because describing a panel must never put traffic on the bus. A driver that
    returns a sub-device says so:

        @property
        def x(self) -> Magnet:

    which is enough for the panel to offer that magnet's field and current
    instead of the object printed as text.
    """
    if value.fget is None:
        return None
    annotation = getattr(value.fget, "__annotations__", {}).get("return")
    if isinstance(annotation, str):
        annotation = getattr(
            inspect.getmodule(value.fget), annotation.strip("'\""), None
        )
    return annotation if inspect.isclass(annotation) else None


def reach(instrument, name):
    """Returns the object a dotted name belongs to, and the last part of it.

    'x.field' on a Mercury iPS reaches the X axis and the name 'field'.
    """
    target = instrument
    parts = name.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    return target, parts[-1]


def describe_one(name, value, prefix=""):
    """Returns the description of a single property."""
    documentation = inspect.getdoc(value.fget) if value.fget else ""
    # A getter may name its unit outright. Otherwise it is read from the
    # docstring, which every written driver ends with "in amps" or similar.
    stated = getattr(value.fget, "unit", None)
    full = f"{prefix}{name}"
    return {
        "name": full,
        "label": full.replace("_", " ").replace(".", " "),
        "help": summarize(documentation),
        "unit": stated or unit_of(documentation),
        "readable": value.fget is not None,
        "writable": value.fset is not None,
    }


def readings_behind_methods(driver):
    """Returns readings a driver offers as a method rather than a property.

    A temperature controller reads a channel, so its temperature is
    temperature(channel="A") and not a plain property. That falls between the
    two panel rules, and without this a cryostat panel shows its heater but not
    its temperature.

    Only methods that need no argument and say they return a single number are
    included, which is what the return annotation is for. That leaves out the
    ones that would make reading a panel expensive, since measure() deliberately
    waits and read_waveform() transfers a whole trace.
    """
    found = []
    for name, value in inspect.getmembers(driver, inspect.isfunction):
        if name.startswith("_"):
            continue
        signature = inspect.signature(value)
        parameters = [p for p in signature.parameters.values() if p.name != "self"]
        if any(p.default is inspect.Parameter.empty for p in parameters):
            continue
        if signature.return_annotation not in (float, int, bool):
            continue
        found.append((name, value))
    return found


def describe_settings(driver):
    """Returns a description of every property a driver class exposes.

    Properties inherited from a base class are included, so an instrument shows
    its identity and error queue alongside its own settings, but they come after
    the ones the driver itself defines.

    A property that names a sub-device as its return type is described by what
    that sub-device offers rather than by itself. Without this the Mercury iPS
    panel shows three objects printed as text and nothing about the field, which
    is the one thing anybody is watching it for.

    :param driver: The driver class, not an instance.
    :return: A list of dicts with name, label, unit, readable and writable.
    """
    settings = []
    for name, value in properties_in_order(driver):
        if name.startswith("_") or name in HIDDEN_SETTINGS:
            continue

        held = held_type(value)
        inner = properties_in_order(held) if held is not None else []
        if inner:
            for inner_name, inner_value in inner:
                if inner_name.startswith("_") or inner_name in HIDDEN_SETTINGS:
                    continue
                settings.append(describe_one(inner_name, inner_value, f"{name}."))
            continue

        settings.append(describe_one(name, value))

    # Readings a driver exposes as a method, which the property walk above
    # cannot see.
    for name, method in readings_behind_methods(driver):
        documentation = inspect.getdoc(method) or ""
        settings.append(
            {
                "name": name,
                "label": name.replace("_", " "),
                "help": summarize(documentation),
                "unit": unit_of(documentation),
                "readable": True,
                "writable": False,
            }
        )
    return settings


def describe_actions(driver):
    """Returns the methods the panel offers as buttons.

    A described instrument names its own, so those are drawn as given. Somebody
    typed the name and the command, and there is nothing left to work out.

    A written driver offers one, safe_shutdown. Everything else it can do is a
    driver call, where the arguments, the return values and the docstrings are.
    A panel offering every method would have to tell from prose which of them
    answer a question and which do something, and that reading goes wrong often
    enough to put buttons on a panel that do nothing when pressed. This one is
    the exception because it is what somebody wants from the panel itself, in a
    hurry, without finding a notebook first.
    """
    described = getattr(driver, "DESCRIPTION", None)
    if described:
        names = [action["name"] for action in described.get("actions", ())]
    else:
        names = [SAFE_SHUTDOWN] if hasattr(driver, SAFE_SHUTDOWN) else []
    return [
        {
            "name": name,
            "label": name.replace("_", " "),
            "help": summarize(inspect.getdoc(getattr(driver, name))),
        }
        for name in names
    ]


def describe(driver):
    """Returns the full description of a driver class."""
    return {
        "driver": driver.__name__,
        "summary": summarize(inspect.getdoc(driver)),
        "settings": describe_settings(driver),
        "actions": describe_actions(driver),
    }


def present_only(instrument, rows):
    """Drop the rows belonging to a sub-device this instrument does not have.

    A driver class carries every sub-device its model line can have, such as
    all three Mercury iPS axes, while one system has whichever of them it was
    built with. Describing from the class alone paints a panel where the axes
    that were never fitted are permanent faults.
    """
    kept = []
    missing = set()
    for row in rows:
        head, dot, _ = row["name"].partition(".")
        if not dot:
            kept.append(row)
            continue
        if head not in missing:
            try:
                getattr(instrument, head)
            except Exception:
                missing.add(head)
        if head not in missing:
            kept.append(row)
    return kept


def read_settings(instrument):
    """Read every readable property of a live instrument.

    A property that raises is reported as an error against that one name rather
    than failing the whole reading, because one unplugged sensor should not
    blank the panel.

    :return: A dict of name to {"value": ...} or {"error": "..."}.
    """
    values = {}
    for setting in describe_settings(type(instrument)):
        if not setting["readable"]:
            continue
        name = setting["name"]
        try:
            target, last = reach(instrument, name)
            reading = getattr(target, last)
            # A reading offered as a method is called with its own defaults.
            values[name] = {
                "value": as_json(reading() if callable(reading) else reading)
            }
        except Exception as error:
            values[name] = {"error": str(error)}
    return values


def as_json(value):
    """Returns a value in a form the web page can display."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [as_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): as_json(item) for key, item in value.items()}
    return str(value)
