"""Where the server remembers which instruments it has.

The file exists so that restarting the server, or the machine, brings the same
instruments back. It is written by the server whenever an instrument is added
or removed, so nobody has to edit it, but it is TOML rather than something
opaque so that it can be read and corrected by hand when that is quicker.

    [server]
    host = "0.0.0.0"
    port = 8000
    refresh = 20
    health_check = 60

    [[instrument]]
    name = "lockin"
    driver = "Sr830"
    gpib_address = 8

    [[instrument]]
    name = "magnet"
    driver = "MercuryIps"
    ip_address = "192.168.1.50"
    port = 7020
"""

import logging
import pathlib
import tomllib

logger = logging.getLogger(__name__)

DEFAULT_PATH = pathlib.Path.home() / ".labdrivers" / "server.toml"


# Characters TOML will not accept raw inside a basic string, and how it spells
# them. Anything else below a space becomes a \uXXXX escape.
ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def quote_key(key):
    """Returns a name written the way TOML spells a key.

    A bare key holds only letters, digits, underscores and dashes. The keys in
    a described instrument are whatever somebody typed, so one with a space in
    it has to be quoted rather than written through. An unquoted one makes the
    file unparseable, which loses every instrument in it and not only the one
    that carried it.
    """
    text = str(key)
    bare = text and all(
        character in "-_"
        or "a" <= character <= "z"
        or "A" <= character <= "Z"
        or "0" <= character <= "9"
        for character in text
    )
    return text if bare else quote(text)


def quote(value):
    """Returns a Python value written the way TOML spells it.

    Lists and dicts become arrays and inline tables, which is how a described
    instrument's settings and actions are written out.

    Control characters are escaped rather than written through. A serial
    instrument is routinely given a carriage return as its write terminator,
    and one raw carriage return makes the whole file unparseable, which loses
    every instrument in it and not only the one that carried it.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return '""'
    if isinstance(value, dict):
        inside = ", ".join(
            f"{quote_key(key)} = {quote(item)}"
            for key, item in value.items()
            if item is not None
        )
        return "{" + inside + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(quote(item) for item in value if item is not None) + "]"

    written = []
    for character in str(value):
        if character in ESCAPES:
            written.append(ESCAPES[character])
        elif character < " " or character == "\x7f":
            written.append(f"\\u{ord(character):04X}")
        else:
            written.append(character)
    return '"' + "".join(written) + '"'


class Config:
    """The server's configuration file.

    :param path: Where to read and write, defaulting to ~/.labdrivers/server.toml.
    """

    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path else DEFAULT_PATH
        self.settings = {}

    def read(self):
        """Returns the whole file as a dict, or an empty one if it is absent."""
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as failure:
            logger.warning("Could not read %s: %s", self.path, failure)
            return {}

    def load(self):
        """Returns the list of instruments the file describes."""
        contents = self.read()
        self.settings = contents.get("server", {})
        instruments = contents.get("instrument", [])
        return instruments if isinstance(instruments, list) else []

    def save(self, instruments):
        """Write the instruments out, keeping any server settings already there.

        A failure to write is logged rather than raised, because losing the
        record of an instrument is not a reason to interrupt a measurement.
        """
        lines = []
        if self.settings:
            lines.append("[server]")
            for key, value in self.settings.items():
                lines.append(f"{quote_key(key)} = {quote(value)}")
            lines.append("")

        for instrument in instruments:
            lines.append("[[instrument]]")
            for key, value in instrument.items():
                if value is not None:
                    lines.append(f"{quote_key(key)} = {quote(value)}")
            lines.append("")

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the real file and moved into place, so a crash
            # partway through leaves the previous configuration rather than
            # half of one.
            beside = self.path.with_suffix(self.path.suffix + ".new")
            beside.write_text("\n".join(lines), encoding="utf-8")
            beside.replace(self.path)
        except OSError as failure:
            logger.warning("Could not write %s: %s", self.path, failure)
