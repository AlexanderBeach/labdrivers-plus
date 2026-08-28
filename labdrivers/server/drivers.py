"""Finding the driver classes a server can offer, and matching them to hardware.

The server needs two things this module provides. It needs the list of drivers
a user can pick from, which is every Instrument subclass in the package rather
than a list someone has to keep up to date. It needs to guess which of them
belongs to a resource found by scanning, which is what the IDENTIFIER on each
driver is for.
"""

import importlib
import inspect
import pkgutil

import labdrivers
from ..core.errors import LabdriversError
from ..core.instrument import Instrument

# Modules holding only shared bases. Their classes are real Instrument
# subclasses but no instrument is one of them, so they are not offered.
BASE_MODULES = (
    "labdrivers.core.instrument",
    "labdrivers.oxford.mercury",
    "labdrivers.oxford.legacy",
)

# What a described instrument is registered as. It is not a class in the
# package, since the class is built when the description is given.
GENERIC = "Generic"

_drivers = None


def available_drivers():
    """Returns every driver class in the package, keyed by class name.

    Found by walking the package rather than from a list, so a driver added
    later is offered by the server and drawn by the web page without anything
    here being edited.
    """
    global _drivers
    if _drivers is not None:
        return _drivers

    found = {}
    for module in pkgutil.walk_packages(labdrivers.__path__, "labdrivers."):
        if module.name in BASE_MODULES or ".server" in module.name:
            continue
        try:
            imported = importlib.import_module(module.name)
        except Exception:
            # A driver whose optional dependency is missing is not offered.
            # Importing labdrivers must never require every backend.
            continue
        for name, value in vars(imported).items():
            if (
                inspect.isclass(value)
                and issubclass(value, Instrument)
                and value.__module__ == module.name
                and value.__module__ not in BASE_MODULES
            ):
                found[name] = value

    _drivers = dict(sorted(found.items()))
    return _drivers


def find_driver(name):
    """Returns the driver class registered under a name.

    :raises LabdriversError: If no driver goes by that name.
    """
    drivers = available_drivers()
    if name not in drivers:
        raise LabdriversError(
            f"There is no driver called '{name}'. The ones available are "
            f"{', '.join(drivers)}."
        )
    return drivers[name]


def describe_drivers():
    """Returns a list describing each driver, for the web page to choose from."""
    described = [
        {
            "name": GENERIC,
            "module": "labdrivers.server.generic",
            "identifier": None,
            "summary": "Any SCPI instrument, described rather than written",
        }
    ]
    for name, driver in available_drivers().items():
        summary = (driver.__doc__ or "").strip().splitlines()
        described.append(
            {
                "name": name,
                "module": driver.__module__,
                "identifier": getattr(driver, "IDENTIFIER", None),
                "summary": summary[0] if summary else "",
            }
        )
    return described


def match_identity(reply):
    """Returns the names of drivers whose IDENTIFIER appears in an *IDN? reply.

    Used to suggest a driver for a resource found by scanning. Several drivers
    can match, since a Keithley 2400 and a 2410 answer similarly, so the caller
    is offered all of them rather than given one silently.
    """
    if not reply:
        return []
    text = str(reply).upper()
    return [
        name
        for name, driver in available_drivers().items()
        if getattr(driver, "IDENTIFIER", None)
        and str(driver.IDENTIFIER).upper() in text
    ]
