"""Reusable argument checks that produce the drivers' house-style messages.

Every setter in this package validates its argument and, on rejection, raises
with a sentence naming what was wrong and what the instrument will accept.
For example::

    The wave amplitude must be between 2E-12 A and 105E-3 A, but got 1 A.

Using them keeps that message identical across every instrument, and keeps a
setter down to one line.
"""

from .errors import RangeError


def check_range(value, minimum, maximum, name, unit=""):
    """Require a numeric value to lie within an inclusive range.

    :param value: The value to check.
    :param minimum: Smallest accepted value.
    :param maximum: Largest accepted value.
    :param name: What the value is, for the message ("wave amplitude").
    :param unit: Unit suffix for the message (" A"), including any space.
    :return: The value as a float.
    :raises RangeError: If the value is outside the range or not a number.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RangeError(
            f"The {name} must be a number between {minimum}{unit} and "
            f"{maximum}{unit}, but got {value!r}."
        )
    if not minimum <= number <= maximum:
        raise RangeError(
            f"The {name} must be between {minimum}{unit} and {maximum}{unit}, "
            f"but got {value}{unit}."
        )
    return number


def check_integer_range(value, minimum, maximum, name, unit=""):
    """Require an integer value to lie within an inclusive range.

    :return: The value as an int.
    :raises RangeError: If the value is outside the range or not a whole number.
    """
    try:
        number = int(value)
        if float(value) != number:
            raise ValueError
    except (TypeError, ValueError):
        raise RangeError(
            f"The {name} must be a whole number between {minimum}{unit} and "
            f"{maximum}{unit}, but got {value!r}."
        )
    if not minimum <= number <= maximum:
        raise RangeError(
            f"The {name} must be between {minimum}{unit} and {maximum}{unit}, "
            f"but got {value}{unit}."
        )
    return number


def check_choice(value, choices, name):
    """Require a value to be one of a fixed set, matched case-insensitively.

    :param choices: Either a sequence of accepted values, or a mapping from
                    accepted value to the string the instrument expects.
    :return: The mapped instrument string if ``choices`` is a mapping,
             otherwise the matched choice in its canonical form.
    :raises RangeError: If the value is not among the choices.
    """
    lookup = {}
    for choice in choices:
        lookup[str(choice).strip().lower()] = choice

    key = str(value).strip().lower()
    if key not in lookup:
        quoted = [f"'{choice}'" for choice in choices]
        accepted = (
            " or ".join(quoted)
            if len(quoted) < 3
            else ", ".join(quoted[:-1]) + " or " + quoted[-1]
        )
        raise RangeError(f"The {name} can be {accepted}, but got {value!r}.")

    matched = lookup[key]
    if isinstance(choices, dict):
        return choices[matched]
    return matched


# Accepted spellings of on and off, so that every driver takes the same ones.
TRUE_VALUES = {"1", "on", "true", "yes", "enable", "enabled"}
FALSE_VALUES = {"0", "off", "false", "no", "disable", "disabled"}


def check_boolean(value, name):
    """Accept the many ways a lab script spells on and off.

    Takes True/False, 1/0, and the strings 'on'/'off', 'true'/'false',
    'yes'/'no', 'enable'/'disable', in any case.

    :return: True or False.
    :raises RangeError: If the value is not recognizable as either.
    """
    if isinstance(value, bool):
        return value

    key = str(value).strip().lower()
    if key in TRUE_VALUES:
        return True
    if key in FALSE_VALUES:
        return False
    raise RangeError(
        f"The {name} can either be 0 (False, 'off') or 1 (True, 'on'), "
        f"but got {value!r}."
    )


def nearest_allowed(value, allowed, name, unit=""):
    """Snap a value to the nearest entry in a list of discrete settings.

    Many instruments only accept a fixed ladder of values. Lock-in time
    constants and sensitivities are both like this, so asking for 47 ms should
    select the 30 ms setting rather than fail.

    :param allowed: Sequence of the values the instrument actually supports.
    :return: A tuple of (index of the chosen setting, the chosen value).
    :raises RangeError: If the value is not a number, or the list is empty.
    """
    if not allowed:
        raise RangeError(f"No settings are available for the {name}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RangeError(f"The {name} must be a number, but got {value!r}.")

    index = min(range(len(allowed)), key=lambda i: abs(float(allowed[i]) - number))
    return index, allowed[index]
