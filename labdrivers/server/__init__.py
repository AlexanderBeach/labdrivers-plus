"""A server that holds instruments open, and a web page for driving them.

A Jupyter kernel is a poor owner of a GPIB session. Restarting it drops every
connection, only one kernel can hold an instrument at a time, and nothing is
watching when an instrument is switched off and on again. Moving the
connections into a process that outlives the kernel fixes all three.

Start it with::

    labdrivers-server

then open the address it prints. The page lists every instrument, draws a panel
for each from the driver itself, and lets you add one by scanning for it.

Notebooks reach the same instruments through :mod:`labdrivers.client`::

    from labdrivers.client import connect
    lockin = connect("lockin")

:mod:`.hub` owns the instruments and the locks that keep them safe to share,
:mod:`.introspect` turns a driver class into the panel drawn for it,
:mod:`.drivers` finds the drivers available, and :mod:`.app` is the web layer.
"""

from .config import Config
from .hub import Hub

__all__ = ["Config", "Hub"]
