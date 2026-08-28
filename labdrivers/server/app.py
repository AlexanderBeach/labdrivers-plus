"""The web server: a small HTTP interface in front of the hub.

Every endpoint that touches hardware is a plain function rather than an async
one, so FastAPI runs it in a worker thread and a slow instrument never stops
the server answering everyone else. The locks in the hub do the serializing.

Errors raised by a driver are returned as their own sentence, because those
sentences already say what the instrument accepts and are more use to whoever
is looking at the page than a status code would be.
"""

import argparse
import logging
import pathlib
import socket
import threading
import time
import webbrowser

from ..core.errors import LabdriversError
from .config import Config
from .drivers import describe_drivers
from .hub import Hub

logger = logging.getLogger(__name__)

STATIC = pathlib.Path(__file__).parent / "static"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

# Seconds between the page re-reading the server's own view of what is
# connected. Costs nothing on any instrument bus.
DEFAULT_REFRESH = 20

# Seconds between asking each idle instrument whether it still answers. This
# one does put a command on the bus, so it is deliberately infrequent, and it
# skips anything in use. Zero turns it off.
DEFAULT_HEALTH_CHECK = 60


def build(hub):
    """Returns a FastAPI application serving one hub.

    :param hub: The Hub holding the instruments.
    """
    try:
        from fastapi import Body, FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse
    except ImportError:
        raise LabdriversError(
            "The server needs FastAPI, which is not installed. Clone the "
            "repository and run 'pip install \".[server]\"'."
        )

    app = FastAPI(title="labdrivers", docs_url="/docs")

    def caller(request):
        """Returns the machine behind a request, by name where it gave one.

        A notebook's RemoteTransport sends its hostname, which is more use to
        somebody deciding whether to take an instrument over than an address.
        """
        return request.headers.get("X-Labdrivers-Client") or (
            request.client.host if request.client else None
        )

    def guard(work):
        """Run something against an instrument, turning failure into a reply.

        A LabdriversError means the request asked for something the instrument
        or this package will not do, which is the caller's to fix, so it comes
        back as a 400 carrying the sentence the driver wrote. Anything else is
        a fault in this package: it is logged with its traceback and returned
        as a 500, so a notebook can tell the two apart.
        """
        try:
            return work()
        except LabdriversError as failure:
            raise HTTPException(status_code=400, detail=str(failure))
        except Exception as failure:
            logger.exception("Unhandled failure serving a request")
            raise HTTPException(status_code=500, detail=str(failure))

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/settings")
    def settings():
        """How often the page should re-check, in seconds."""
        return {"refresh": int(hub.settings.get("refresh", DEFAULT_REFRESH))}

    @app.get("/api/drivers")
    def drivers():
        """Every driver this server can offer."""
        return describe_drivers()

    @app.get("/api/scan")
    def scan():
        """VISA resources on this machine, with a suggested driver for each."""
        return guard(hub.scan)

    @app.get("/api/instruments")
    def instruments():
        """One line for each registered instrument."""
        return hub.summaries()

    @app.post("/api/instruments")
    def add(body: dict = Body(...)):
        """Register an instrument and open it."""
        name = body.get("name")
        driver = body.get("driver")
        connection = {
            key: value
            for key, value in (body.get("connection") or {}).items()
            if value not in (None, "")
        }
        entry = guard(
            lambda: hub.add(
                name,
                driver,
                connection,
                body.get("settings") or [],
                body.get("actions") or [],
            )
        )
        return entry.summary()

    @app.delete("/api/instruments/{name}")
    def remove(name: str):
        """Close an instrument and forget it, leaving its state alone."""
        guard(lambda: hub.remove(name))
        return {"removed": name}

    @app.get("/api/instruments/{name}")
    def describe(name: str):
        """Everything needed to draw one instrument's panel."""
        return guard(lambda: hub.describe(name))

    @app.get("/api/instruments/{name}/values")
    def values(name: str, request: Request):
        """Read every readable property."""
        return guard(lambda: hub.read(name, who=caller(request)))

    @app.post("/api/instruments/{name}/set")
    def set_value(name: str, request: Request, body: dict = Body(...)):
        """Set one property and return what it reads back as."""
        setting = body.get("setting")
        if not setting:
            raise HTTPException(status_code=400, detail="No setting was named.")
        who = caller(request)
        return {
            "value": guard(lambda: hub.set(name, setting, body.get("value"), who=who))
        }

    @app.post("/api/instruments/{name}/action")
    def run_action(name: str, request: Request, body: dict = Body(...)):
        """Run one no-argument method."""
        action = body.get("action")
        if not action:
            raise HTTPException(status_code=400, detail="No action was named.")
        who = caller(request)
        return {"result": guard(lambda: hub.call(name, action, who=who))}

    @app.post("/api/instruments/{name}/describe")
    def describe_again(name: str, body: dict = Body(...)):
        """Change what a described instrument offers, keeping it connected."""
        return guard(
            lambda: hub.redescribe(
                name, body.get("settings") or [], body.get("actions") or []
            ).summary()
        )

    @app.post("/api/instruments/{name}/reconnect")
    def reconnect(name: str):
        """Close and reopen, for after an instrument has been power-cycled."""
        return guard(lambda: hub.reconnect(name).summary())

    @app.post("/api/instruments/{name}/io")
    def raw_io(name: str, request: Request, body: dict = Body(...)):
        """Carry one command for a RemoteTransport in somebody's notebook."""
        try:
            return {"reply": hub.io(name, body, who=caller(request))}
        except Exception as failure:
            # Carried in the body rather than as a status, because the caller
            # is a RemoteTransport standing in for a wire and the failure it
            # wants is the instrument's, not HTTP's. Never empty: an exception
            # that stringifies to nothing would otherwise read as success.
            return {"error": str(failure) or type(failure).__name__}

    return app


def open_when_ready(url, host, port, timeout=15.0):
    """Open the page in a browser once the server is answering.

    Waiting for the port matters: opening straight away races the server and
    lands on a connection error often enough to be annoying.
    """
    reachable = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((reachable, port), 0.25):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)
    logger.warning("The server did not start in time to open %s", url)


def main(argv=None):
    """Run the server from the command line."""
    parser = argparse.ArgumentParser(
        prog="labdrivers-server",
        description="Hold instruments open and serve a control page for them.",
    )
    parser.add_argument("--host", default=None, help="Address to listen on.")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on.")
    parser.add_argument("--config", default=None, help="Path to the TOML file.")
    parser.add_argument(
        "--health-check",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "How often to ask each idle instrument whether it still answers. "
            "Zero never asks. Default 60."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window. Use this when running as a service.",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="Start with no instruments instead of loading the saved ones.",
    )
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
    )

    config = Config(arguments.config)
    # Read before anything else, so --empty still honors the configured host
    # and port instead of binding the defaults and then saving them over.
    config.load()
    hub = Hub(config)
    hub.settings = config.settings
    if arguments.empty:
        # Asked to start without them, which is not the same as asked to throw
        # them away. They are set aside so that adding an instrument in this
        # session writes the file back with them still in it.
        hub.unloaded = list(config.load())
    else:
        hub.load()

    host = arguments.host or config.settings.get("host", DEFAULT_HOST)
    port = arguments.port or int(config.settings.get("port", DEFAULT_PORT))

    try:
        import uvicorn
    except ImportError:
        raise LabdriversError(
            "The server needs uvicorn, which is not installed. Clone the "
            "repository and run 'pip install \".[server]\"'."
        )

    # An address rather than a name, because localhost costs two seconds a
    # command on Windows: it resolves to ::1 first and the server binds IPv4.
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    address = f"http://{shown}:{port}"
    logger.info("Instruments: %s", ", ".join(hub.entries) or "none yet")
    logger.info("Watch them at %s", address)

    if not arguments.no_browser:
        threading.Thread(
            target=open_when_ready, args=(address, host, port), daemon=True
        ).start()

    health = float(
        arguments.health_check
        if arguments.health_check is not None
        else config.settings.get("health_check", DEFAULT_HEALTH_CHECK)
    )
    stopping = threading.Event()
    if health:
        logger.info("Checking each idle instrument answers every %g s", health)
        threading.Thread(
            target=hub.watch_health, args=(health, stopping), daemon=True
        ).start()

    try:
        uvicorn.run(build(hub), host=host, port=port, log_level="warning")
    finally:
        stopping.set()
        hub.close()


if __name__ == "__main__":
    main()
