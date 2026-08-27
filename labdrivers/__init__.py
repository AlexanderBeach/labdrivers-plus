"""labdrivers: Python drivers for laboratory instruments.

Each vendor has a subpackage. Every driver is built on labdrivers.core, which
provides the transports, reply parsing, argument validation and error types.

    from labdrivers.keithley import Keithley2400

    source = Keithley2400(gpib_address=24)
    source.source_function = "voltage"
    source.current_compliance = 1e-3

Subpackages are imported on demand, so a machine without pythonnet or the NI
DAQmx driver can still use everything else.
"""

from .version import __version__

name = "labdrivers"

__all__ = [
    "__version__",
    "coppermountain",
    "core",
    "funky_rotator",
    "keithley",
    "keysight",
    "lakeshore",
    "ni",
    "oxford",
    "quantumdesign",
    "rigol",
    "srs",
]
