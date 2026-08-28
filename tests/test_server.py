"""Tests for the instrument server.

No hardware and no network. Instruments are built on a RecordingTransport, so
the tests can assert the exact commands the server put on the wire, the same
way the driver tests do. The threaded tests are the exception: sharing one
instrument safely is what the hub exists for, so it is exercised for real.
"""

import threading
import time
import tomllib

import pytest

from labdrivers.core.errors import (
    ConnectionFailure,
    InstrumentTimeoutError,
    LabdriversError,
    RangeError,
)
from labdrivers import client as remote
from labdrivers.core.instrument import Instrument
from labdrivers.core.transport import RecordingTransport, RemoteTransport
from labdrivers.keithley.keithley6221 import Keithley6221
from labdrivers.oxford.mercuryips_teslatron import MercuryIpsTeslatron
from labdrivers.server import generic, introspect
from labdrivers.server.config import Config, quote, quote_key
from labdrivers.server.drivers import (
    GENERIC,
    available_drivers,
    describe_drivers,
    find_driver,
    match_identity,
)
from labdrivers.server.hub import Hub
from labdrivers.srs.sr830 import Sr830


# Finding drivers


def test_base_classes_are_not_offered_as_drivers():
    drivers = available_drivers()
    for base in (
        "Instrument",
        "ScpiInstrument",
        "MercuryInstrument",
        "OxfordLegacyInstrument",
    ):
        assert base not in drivers


def test_real_drivers_are_offered():
    drivers = available_drivers()
    for driver in ("Sr830", "Keithley2400", "Keithley6221", "Vna", "Triton200"):
        assert driver in drivers


def test_unknown_driver_says_what_is_available():
    with pytest.raises(LabdriversError) as failure:
        find_driver("Sr840")
    assert "no driver called 'Sr840'" in str(failure.value)
    assert "Sr830" in str(failure.value)


def test_identity_matches_a_driver():
    assert match_identity("Stanford_Research_Systems,SR830,s/n1,v1.07") == ["Sr830"]
    assert "Keithley6221" in match_identity("KEITHLEY INSTRUMENTS INC.,MODEL 6221")


def test_identity_of_nothing_matches_nothing():
    assert match_identity(None) == []
    assert match_identity("") == []


# Reading a driver class


def test_settings_are_found_with_their_units():
    settings = {s["name"]: s for s in introspect.describe_settings(Sr830)}
    assert settings["time_constant"]["readable"]
    assert settings["time_constant"]["writable"]
    assert settings["x"]["readable"]
    assert not settings["x"]["writable"]


def test_readings_come_before_controls():
    names = [s["name"] for s in introspect.describe_settings(Sr830)]
    # What the lock-in reports is what you look at, so it leads the panel.
    assert names[:4] == ["x", "y", "magnitude", "theta"]
    assert names.index("x") < names.index("time_constant")


def test_inherited_settings_come_last():
    # Spelled out here rather than taken from a driver, so the test states
    # exactly which setting is the class's own and which is inherited.
    class Base(Instrument):
        @property
        def from_base(self):
            """Returns something a base class offers."""

    class Derived(Base):
        @property
        def from_derived(self):
            """Returns something the driver itself offers."""

    names = [s["name"] for s in introspect.describe_settings(Derived)]
    assert names == ["from_derived", "from_base"]


def test_protocol_registers_are_not_on_the_panel():
    # A described instrument inherits the IEEE 488.2 registers, which are
    # bookkeeping rather than anything a person watches a cryostat for.
    driver = generic.build([{"name": "voltage", "query": "VOLT?"}])
    names = [s["name"] for s in introspect.describe_settings(driver)]
    assert names == ["voltage"]


def test_plumbing_is_not_shown_as_a_setting():
    names = {s["name"] for s in introspect.describe_settings(Sr830)}
    assert "transport" not in names


def test_unit_comes_from_the_docstring():
    assert introspect.unit_of("Returns the amplitude of the wave, in amps") == "A"
    assert introspect.unit_of("Returns the frequency, in hertz") == "Hz"
    assert introspect.unit_of("Returns the field, in tesla") == "T"
    assert introspect.unit_of("Returns whether the output is on") == ""


def test_summary_drops_the_full_stop():
    assert (
        introspect.summarize("Returns the range.\n\nMore text.") == "Returns the range"
    )
    assert introspect.summarize("") == ""


def test_actions_take_no_arguments_and_exclude_raw_io():
    actions = {a["name"] for a in introspect.describe_actions(Keithley6221)}
    assert "close" not in actions
    assert "query" not in actions
    assert "write" not in actions
    for action in actions:
        assert not action.startswith("_")


def test_a_written_driver_offers_one_key_and_no_guesses():
    # Which of a driver's methods answer a question and which do something can
    # only be read out of their prose, and reading it wrong puts buttons on a
    # panel that do nothing when pressed. Everything but the one that matters
    # in a hurry is left to a driver call.
    assert {a["name"] for a in introspect.describe_actions(Sr830)} == set()
    assert {a["name"] for a in introspect.describe_actions(Keithley6221)} == {
        "safe_shutdown"
    }


def test_a_failing_property_does_not_blank_the_panel():
    class Broken(Sr830):
        @property
        def x(self):
            raise LabdriversError("The lock-in is unplugged.")

    instrument = Broken(transport=RecordingTransport(default="0"))
    values = introspect.read_settings(instrument)
    assert values["x"]["error"] == "The lock-in is unplugged."
    assert "value" in values["phase"]


# Holding instruments


def test_adding_twice_is_refused(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.add("lockin", "Sr830", {"transport": RecordingTransport()})
    assert "already registered" in str(failure.value)


def test_unknown_instrument_lists_what_is_registered(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.entry("magnet")
    assert "No instrument called 'magnet'" in str(failure.value)
    assert "lockin" in str(failure.value)


def test_setting_a_property_sends_the_command(hub):
    transport = hub.entry("lockin").instrument.transport
    hub.set("lockin", "time_constant", 0.3)
    assert any("OFLT" in command for command in transport.writes)


def test_setting_something_read_only_is_refused(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.set("lockin", "x", 1.0)
    assert "can be read but not set" in str(failure.value)


def test_setting_something_unknown_is_refused(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.set("lockin", "nonexistent", 1.0)
    assert "no setting called 'nonexistent'" in str(failure.value)


def test_unknown_action_is_refused(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.call("lockin", "detonate")
    assert "no action called 'detonate'" in str(failure.value)


def test_raw_io_carries_the_exact_command(hub):
    transport = hub.entry("lockin").instrument.transport
    transport.responses = {"OUTP? 1": "1.234"}
    assert hub.io("lockin", {"kind": "query", "command": "OUTP? 1"}) == "1.234"
    assert transport.queries == ["OUTP? 1"]

    hub.io("lockin", {"kind": "write", "command": "PHAS 45"})
    assert "PHAS 45" in transport.writes


def test_raw_io_rejects_an_unknown_operation(hub):
    with pytest.raises(LabdriversError) as failure:
        hub.io("lockin", {"kind": "detonate"})
    assert "but got 'detonate'" in str(failure.value)


def test_removing_closes_but_leaves_the_instrument_alone(hub):
    transport = hub.entry("lockin").instrument.transport
    hub.remove("lockin")
    assert "lockin" not in hub.entries
    # Nothing was reset, no output switched off. Closing is the only action.
    assert transport.writes == []


def test_summaries_describe_each_instrument(hub):
    summaries = hub.summaries()
    assert len(summaries) == 1
    assert summaries[0]["name"] == "lockin"
    assert summaries[0]["driver"] == "Sr830"
    assert summaries[0]["status"] == "connected"


def test_describe_carries_settings_and_status(hub):
    described = hub.describe("lockin")
    assert described["driver"] == "Sr830"
    assert described["status"] == "connected"
    assert any(s["name"] == "time_constant" for s in described["settings"])


# Remembering them


def test_config_round_trip(tmp_path):
    config = Config(tmp_path / "server.toml")
    config.settings = {"host": "0.0.0.0", "port": 8000}
    config.save(
        [
            {"name": "lockin", "driver": "Sr830", "gpib_address": 8},
            {"name": "magnet", "driver": "MercuryIps", "ip_address": "192.168.1.50"},
        ]
    )
    loaded = config.load()
    assert [item["name"] for item in loaded] == ["lockin", "magnet"]
    assert loaded[0]["gpib_address"] == 8
    assert loaded[1]["ip_address"] == "192.168.1.50"
    assert config.settings["port"] == 8000


def test_config_that_does_not_exist_is_empty(tmp_path):
    assert Config(tmp_path / "absent.toml").load() == []


def test_toml_quoting():
    assert quote(True) == "true"
    assert quote(8) == "8"
    assert quote("GPIB0::8::INSTR") == '"GPIB0::8::INSTR"'
    assert quote('a "quoted" name') == '"a \\"quoted\\" name"'


# Reaching the server from a notebook


def test_remote_transport_builds_its_address():
    assert RemoteTransport("lockin", "cryostat:8000").server == "http://cryostat:8000"
    assert RemoteTransport("lockin", "https://c:8000").server == "https://c:8000"


def test_remote_transport_explains_a_server_that_is_not_there():
    from labdrivers.core.errors import ConnectionFailure

    transport = RemoteTransport("lockin", "localhost:1", timeout=0.5)
    with pytest.raises(ConnectionFailure) as failure:
        transport.query("*IDN?")
    assert "labdrivers-server" in str(failure.value)


# The web layer


@pytest.fixture
def client(hub):
    """A test client for the application, with one lock-in registered."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from labdrivers.server.app import build

    return TestClient(build(hub))


def test_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "labdrivers" in response.text


def test_instruments_are_listed(client):
    body = client.get("/api/instruments").json()
    assert [item["name"] for item in body] == ["lockin"]


def test_drivers_are_listed(client):
    names = [item["name"] for item in client.get("/api/drivers").json()]
    assert "Sr830" in names
    assert "ScpiInstrument" not in names


def test_values_are_read(client):
    body = client.get("/api/instruments/lockin/values").json()
    assert "phase" in body


def test_setting_a_bad_value_returns_the_drivers_own_sentence(client, hub):
    response = client.post(
        "/api/instruments/lockin/set",
        json={"setting": "time_constant", "value": "not a number"},
    )
    assert response.status_code == 400
    assert "must be" in response.json()["detail"]


def test_unknown_instrument_is_a_400(client):
    response = client.get("/api/instruments/magnet/values")
    assert response.status_code == 400
    assert "No instrument called 'magnet'" in response.json()["detail"]


def test_adding_without_a_driver_is_refused(client):
    response = client.post("/api/instruments", json={"name": "magnet"})
    assert response.status_code == 400
    assert "needs a name and a driver" in response.json()["detail"]


def test_raw_io_endpoint_carries_a_query(client, hub):
    transport = hub.entry("lockin").instrument.transport
    transport.responses = {"FREQ?": "42"}
    body = client.post(
        "/api/instruments/lockin/io", json={"kind": "query", "command": "FREQ?"}
    ).json()
    assert body["reply"] == "42"
    assert transport.queries == ["FREQ?"]


def test_raw_io_endpoint_reports_failure_without_raising(client):
    body = client.post(
        "/api/instruments/absent/io", json={"kind": "query", "command": "FREQ?"}
    ).json()
    assert "No instrument called 'absent'" in body["error"]


def test_removing_forgets_the_instrument(client):
    assert client.delete("/api/instruments/lockin").status_code == 200
    assert client.get("/api/instruments").json() == []


# Instruments the package has no driver for


PSU = [
    {
        "name": "voltage",
        "query": "VOLT?",
        "write": "VOLT {}",
        "unit": "V",
        "type": "float",
        "minimum": 0,
        "maximum": 30,
    },
    {"name": "identity", "query": "*IDN?", "type": "string"},
    {"name": "output", "query": "OUTP?", "write": "OUTP {}", "type": "boolean"},
]
RESET = [{"name": "reset", "command": "*RST"}]


@pytest.fixture
def described():
    """A hub holding a power supply that has no driver in the package."""
    holder = Hub(config=None)
    transport = RecordingTransport(
        responses={"VOLT?": "12.5", "*IDN?": "ACME,PSU-1,0,1.0", "OUTP?": "1"},
        default="0",
    )
    holder.add("psu", GENERIC, {"transport": transport}, PSU, RESET)
    return holder


def test_a_description_becomes_a_real_driver_class():
    driver = generic.build(PSU, RESET)
    assert isinstance(driver.voltage, property)
    assert driver.voltage.fget is not None
    assert driver.voltage.fset is not None
    # Read-only, because no write command was given for it.
    assert driver.identity.fset is None
    assert callable(driver.reset)


def test_a_described_class_introspects_like_any_other():
    driver = generic.build(PSU, RESET)
    settings = {s["name"]: s for s in introspect.describe_settings(driver)}
    assert settings["voltage"]["writable"]
    assert settings["voltage"]["unit"] == "V"
    assert not settings["identity"]["writable"]
    assert "reset" in {a["name"] for a in introspect.describe_actions(driver)}


def test_described_settings_send_the_commands_they_were_given():
    transport = RecordingTransport(responses={"VOLT?": "12.5"}, default="0")
    psu = generic.build(PSU, RESET)(transport=transport)

    assert psu.voltage == 12.5
    assert transport.queries == ["VOLT?"]

    psu.voltage = 5
    assert "VOLT 5.0" in transport.writes

    psu.reset()
    assert "*RST" in transport.writes


def test_a_described_setting_validates_in_the_house_wording():
    psu = generic.build(PSU, RESET)(transport=RecordingTransport(default="0"))
    with pytest.raises(LabdriversError) as failure:
        psu.voltage = 40
    assert str(failure.value) == (
        "The voltage must be between 0 V and 30 V, but got 40 V."
    )


def test_a_described_boolean_takes_the_usual_spellings():
    transport = RecordingTransport(responses={"OUTP?": "1"}, default="0")
    psu = generic.build(PSU, RESET)(transport=transport)
    psu.output = "on"
    assert "OUTP 1" in transport.writes
    psu.output = False
    assert "OUTP 0" in transport.writes
    assert psu.output is True


def test_the_console_is_always_there():
    transport = RecordingTransport(responses={"MEAS?": "3.3"}, default="0")
    psu = generic.build(PSU, RESET)(transport=transport)
    assert psu.ask("MEAS?") == "3.3"
    psu.send("SYST:BEEP")
    assert "SYST:BEEP" in transport.writes


def test_a_setting_needs_a_command():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "voltage"}])
    assert "needs a query command, a write command, or both" in str(failure.value)


def test_a_write_command_needs_somewhere_for_the_value():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "voltage", "write": "VOLT"}])
    assert "exactly one plain {}" in str(failure.value)


def test_a_write_command_with_two_places_is_refused():
    # Accepting this raises IndexError on first use, with a message
    # meaningless to anyone but a Python programmer.
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "voltage", "write": "VOLT {}; CURR {}"}])
    assert "exactly one plain {}" in str(failure.value)


def test_a_write_command_with_a_named_field_is_refused():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "output", "write": "OUTP {} {ON}"}])
    assert "exactly one plain {}" in str(failure.value)


@pytest.mark.parametrize(
    "name", ["close", "write", "read", "query", "transport", "wait_until", "send"]
)
def test_a_description_cannot_replace_the_machinery(name):
    # An action called close was the worst of these: removing the instrument
    # would have sent a command to the hardware and left the session open.
    with pytest.raises(LabdriversError) as failure:
        generic.build([], [{"name": name, "command": "X"}])
    assert "replace the machinery" in str(failure.value)


@pytest.mark.parametrize("name", ["reset", "identify", "errors", "self_test"])
def test_a_description_may_replace_a_standard_command(name):
    # These wrap one standard command, and an instrument that spells it
    # differently is entitled to say so.
    driver = generic.build([], [{"name": name, "command": "SYST:PRES"}])
    transport = RecordingTransport(default="0")
    getattr(driver(transport=transport), name)()
    assert transport.writes == ["SYST:PRES"]


def test_a_setting_name_has_to_be_usable():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "out put", "query": "V?"}])
    assert "has to work as a Python attribute" in str(failure.value)


def test_two_settings_cannot_share_a_name():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "v", "query": "A?"}, {"name": "v", "query": "B?"}])
    assert "two settings called 'v'" in str(failure.value)


def test_an_unknown_type_is_refused():
    with pytest.raises(LabdriversError) as failure:
        generic.build([{"name": "v", "query": "V?", "type": "complex"}])
    assert "but got 'complex'" in str(failure.value)


def test_an_action_needs_a_command():
    with pytest.raises(LabdriversError) as failure:
        generic.build([], [{"name": "reset"}])
    assert "needs a command to send" in str(failure.value)


def test_the_hub_holds_a_described_instrument(described):
    assert described.entry("psu").described
    values = described.read("psu")
    assert values["voltage"]["value"] == 12.5
    assert values["identity"]["value"] == "ACME,PSU-1,0,1.0"


def test_a_described_instrument_is_set_through_the_hub(described):
    transport = described.entry("psu").instrument.transport
    transport.clear()
    described.set("psu", "voltage", 7)
    assert "VOLT 7.0" in transport.writes


def test_describing_again_keeps_the_connection(described):
    entry = described.entry("psu")
    transport = entry.instrument.transport
    described.redescribe("psu", PSU + [{"name": "current", "query": "CURR?"}], RESET)
    # Same transport, so the GPIB session was never dropped.
    assert described.entry("psu").instrument.transport is transport
    assert hasattr(described.entry("psu").instrument, "current")


def test_a_written_driver_cannot_be_redescribed():
    holder = Hub(config=None)
    holder.add("lockin", "Sr830", {"transport": RecordingTransport(default="0")})
    with pytest.raises(LabdriversError) as failure:
        holder.redescribe("lockin", PSU, [])
    assert "cannot be described here" in str(failure.value)


def test_a_bad_description_is_refused_before_anything_changes(described):
    entry = described.entry("psu")
    with pytest.raises(LabdriversError):
        described.redescribe("psu", [{"name": "broken"}], [])
    assert entry.settings == PSU
    assert hasattr(entry.instrument, "voltage")


def test_a_description_survives_the_config_file(tmp_path):
    config = Config(tmp_path / "server.toml")
    holder = Hub(config)
    holder.add("psu", GENERIC, {"resource_name": "GPIB0::5::INSTR"}, PSU, RESET)

    again = Hub(Config(tmp_path / "server.toml"))
    for saved in again.config.load():
        assert saved["name"] == "psu"
        assert saved["settings"][0]["write"] == "VOLT {}"
        assert saved["actions"] == RESET


def test_generic_is_offered_as_a_driver():
    names = [item["name"] for item in describe_drivers()]
    assert names[0] == GENERIC


# The web layer, for described instruments


@pytest.fixture
def psu_client(described):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from labdrivers.server.app import build

    return TestClient(build(described))


def test_the_description_is_sent_to_the_client(psu_client):
    body = psu_client.get("/api/instruments/psu").json()
    assert body["described"] is True
    assert body["description"]["settings"][0]["name"] == "voltage"
    assert any(s["name"] == "voltage" for s in body["settings"])


def test_a_described_instrument_is_added_over_http(client):
    response = client.post(
        "/api/instruments",
        json={
            "name": "psu",
            "driver": GENERIC,
            "connection": {},
            "settings": [{"name": "voltage", "query": "VOLT?", "write": "VOLT {}"}],
        },
    )
    # No transport was given, so it registers and reports why it will not open.
    assert response.status_code == 200
    assert response.json()["described"] is True


def test_a_bad_description_over_http_says_what_is_wrong(client):
    response = client.post(
        "/api/instruments",
        json={
            "name": "psu",
            "driver": GENERIC,
            "connection": {},
            "settings": [{"name": "voltage", "write": "VOLT"}],
        },
    )
    assert response.status_code == 400
    assert "exactly one plain {}" in response.json()["detail"]


def test_describing_again_over_http(psu_client):
    response = psu_client.post(
        "/api/instruments/psu/describe",
        json={
            "settings": PSU + [{"name": "current", "query": "CURR?"}],
            "actions": RESET,
        },
    )
    assert response.status_code == 200
    body = psu_client.get("/api/instruments/psu").json()
    assert any(s["name"] == "current" for s in body["settings"])


def test_the_console_works_over_http_for_any_instrument(psu_client, described):
    transport = described.entry("psu").instrument.transport
    transport.responses = {"SYST:ERR?": "0,No error"}
    body = psu_client.post(
        "/api/instruments/psu/io", json={"kind": "query", "command": "SYST:ERR?"}
    ).json()
    assert body["reply"] == "0,No error"


# Keeping off the bus
#
# The server is shared. A reading taken on a timer can land between the two
# commands of a measurement running on another machine, because the lock
# serializes calls and not transactions.


def test_drawing_a_panel_sends_nothing_to_the_instrument(client, hub):
    transport = hub.entry("lockin").instrument.transport
    transport.clear()

    client.get("/")
    client.get("/api/instruments")
    client.get("/api/instruments/lockin")
    client.get("/api/drivers")

    assert transport.commands == []


def test_the_status_check_sends_nothing_to_the_instrument(client, hub):
    # The page re-checks status on a timer. That must cost nothing on any
    # instrument bus, or the timer is exactly the thing being avoided.
    transport = hub.entry("lockin").instrument.transport
    transport.clear()
    for _ in range(5):
        client.get("/api/instruments")
    assert transport.commands == []


def test_a_panel_is_drawn_from_what_was_last_read(client, hub):
    # So that opening the page shows numbers without asking for fresh ones.
    client.get("/api/instruments/lockin/values")
    transport = hub.entry("lockin").instrument.transport
    transport.clear()

    described = client.get("/api/instruments/lockin").json()
    assert described["values"], "the panel would open blank"
    assert "phase" in described["values"]
    assert transport.commands == []


def test_reading_is_the_only_thing_that_talks_to_the_instrument(client, hub):
    transport = hub.entry("lockin").instrument.transport
    transport.clear()
    client.get("/api/instruments/lockin/values")
    assert transport.commands, "Read should be the one thing that does talk"


# Sharing an instrument between threads
#
# Per-instrument locking is the whole point of the hub, so it is exercised
# with real threads rather than asserted about.


class SlowToOpen(Sr830):
    """A lock-in that takes its time connecting, to widen the race window."""

    opened = 0

    def __init__(self, **arguments):
        SlowToOpen.opened += 1
        time.sleep(0.25)
        super().__init__(**arguments)


def test_adding_and_reading_at_once_opens_one_connection(monkeypatch):
    # Publishing the entry and then opening it would let a request arriving
    # in between find no instrument and open a second session to the same
    # address.
    SlowToOpen.opened = 0
    monkeypatch.setattr("labdrivers.server.hub.find_driver", lambda name: SlowToOpen)
    holder = Hub(config=None)
    transport = RecordingTransport(default="0")

    adding = threading.Thread(
        target=holder.add, args=("lockin", "SlowToOpen", {"transport": transport})
    )
    adding.start()
    time.sleep(0.05)
    try:
        holder.read("lockin")
    except LabdriversError:
        pass
    adding.join()

    assert SlowToOpen.opened == 1


def test_listing_while_instruments_come_and_go():
    # Iterating self.entries directly races with adding and removing, and
    # raises RuntimeError in whichever thread happens to be listing.
    holder = Hub(config=None)
    failures = []

    def churn():
        for index in range(150):
            try:
                holder.add(
                    f"i{index}", "Sr830", {"transport": RecordingTransport(default="0")}
                )
                holder.remove(f"i{index}")
            except Exception as failure:
                failures.append(failure)

    def listing():
        for _ in range(400):
            try:
                holder.summaries()
                holder.save()
            except Exception as failure:
                failures.append(failure)

    threads = [threading.Thread(target=churn), threading.Thread(target=listing)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []


def test_a_busy_instrument_gives_the_server_back(monkeypatch):
    # Without a timeout, one instrument that stopped answering could take every
    # request thread with it, including the page that would let somebody fix it.
    monkeypatch.setattr("labdrivers.server.hub.LOCK_TIMEOUT", 0.2)
    holder = Hub(config=None)
    holder.add("lockin", "Sr830", {"transport": RecordingTransport(default="0")})
    entry = holder.entry("lockin")

    entry.lock.acquire()
    try:
        with pytest.raises(InstrumentTimeoutError) as failure:
            with entry.hold(timeout=0.2):
                pass
        assert "still busy" in str(failure.value)
    finally:
        entry.lock.release()


# Registering something that can never work


def test_a_driver_name_that_does_not_exist_is_refused():
    # Registering it quietly with status disconnected and no error is
    # indistinguishable from an instrument waiting to be switched on, and it
    # would then be written to the file and re-registered at every restart.
    holder = Hub(config=None)
    with pytest.raises(LabdriversError) as failure:
        holder.add("typo", "Sr831", {})
    assert "no driver called 'Sr831'" in str(failure.value)
    assert "typo" not in holder.entries


def test_a_connection_that_says_nothing_is_reported_not_hidden():
    # This one stays registered, because it fails as a ConnectionFailure like
    # any unreachable instrument. What matters is that it does not look
    # healthy: disconnected with no error at all would be indistinguishable
    # from an instrument merely switched off.
    holder = Hub(config=None)
    entry = holder.add("lockin", "Sr830", {"nonsense": 1})
    assert entry.status == "error"
    assert "No instrument was specified" in entry.error


def test_an_instrument_that_is_switched_off_keeps_its_registration():
    # The one failure a reconnect can cure, so it is the one that is forgiven.
    holder = Hub(config=None)
    entry = holder.add("lockin", "Sr830", {"gpib_address": 99})
    assert "lockin" in holder.entries
    assert entry.status == "error"
    assert "GPIB0::99::INSTR" in entry.error


# The configuration file


@pytest.mark.parametrize(
    "label,connection",
    [
        ("carriage return", {"write_termination": "\r"}),
        ("newline", {"write_termination": "\n"}),
        ("tab", {"resource_name": "a\tb"}),
        ("quote", {"resource_name": 'a"b'}),
        ("backslash", {"dll_path": "C:\\lib\\WJ.dll"}),
        ("bell", {"resource_name": "x\x07y"}),
        ("unicode", {"resource_name": "probe \u00b5"}),
    ],
)
def test_every_value_survives_the_file(tmp_path, label, connection):
    # Round-tripped through tomllib rather than compared against what the
    # writer produced, because parsing is what actually has to work. A raw
    # carriage return, which a serial Oxford controller genuinely needs,
    # makes the whole file unreadable.
    config = Config(tmp_path / "server.toml")
    config.save([{"name": "one", "driver": "Itc503", **connection}])

    with open(tmp_path / "server.toml", "rb") as handle:
        tomllib.load(handle)

    saved = config.load()[0]
    for key, value in connection.items():
        assert saved[key] == value, label


def test_one_awkward_instrument_does_not_lose_the_others(tmp_path):
    # The real damage: an unparseable file is read as empty, so every good
    # instrument goes too and the next save writes the emptiness back.
    config = Config(tmp_path / "server.toml")
    config.save(
        [
            {"name": "lockin", "driver": "Sr830", "gpib_address": 8},
            {"name": "itc", "driver": "Itc503", "write_termination": "\r"},
            {"name": "magnet", "driver": "MercuryIps", "ip_address": "192.168.0.11"},
        ]
    )
    names = [item["name"] for item in Config(tmp_path / "server.toml").load()]
    assert names == ["lockin", "itc", "magnet"]


def test_server_settings_survive_an_instrument_being_added(tmp_path):
    # --empty must not skip the only call that reads them, or the configured
    # address is ignored and then written away by the next save.
    (tmp_path / "server.toml").write_text(
        '[server]\nhost = "127.0.0.1"\nport = 9001\n', encoding="utf-8"
    )
    config = Config(tmp_path / "server.toml")
    config.load()
    config.save([{"name": "lockin", "driver": "Sr830", "gpib_address": 8}])

    again = Config(tmp_path / "server.toml")
    again.load()
    assert again.settings == {"host": "127.0.0.1", "port": 9001}


# Reaching the server from a notebook


def test_a_name_with_a_space_is_quoted():
    # An unquoted space hands back a driver on which every operation raises
    # InvalidURL from underneath the transport.
    transport = RemoteTransport("probe 1", "localhost:1", timeout=0.3)
    with pytest.raises(ConnectionFailure) as failure:
        transport.query("*IDN?")
    assert "InvalidURL" not in str(failure.value)
    assert "No labdrivers server answered" in str(failure.value)


def test_a_closed_remote_transport_stops_sending():
    transport = RemoteTransport("lockin", "localhost:1", timeout=0.3)
    transport.close()
    with pytest.raises(ConnectionFailure) as failure:
        transport.query("*IDN?")
    assert "has been closed" in str(failure.value)


# Connection lifetime


def test_reconnecting_replaces_the_instrument(hub):
    before = hub.entry("lockin").instrument
    hub.reconnect("lockin")
    assert hub.entry("lockin").instrument is not before
    assert hub.entry("lockin").status == "connected"


def test_closing_the_hub_closes_every_instrument(hub):
    transport = hub.entry("lockin").instrument.transport
    hub.close()
    assert transport.closed
    assert hub.entry("lockin").status == "disconnected"


def test_loading_registers_what_the_file_holds(tmp_path):
    config = Config(tmp_path / "server.toml")
    config.save([{"name": "lockin", "driver": "Sr830", "gpib_address": 99}])

    holder = Hub(Config(tmp_path / "server.toml"))
    holder.load()
    # Switched off, so it is registered with its error rather than dropped.
    assert "lockin" in holder.entries
    assert holder.entry("lockin").status == "error"


def test_a_described_instrument_survives_a_restart(tmp_path):
    config = Config(tmp_path / "server.toml")
    holder = Hub(config)
    holder.add(
        "psu",
        GENERIC,
        {"resource_name": "GPIB0::5::INSTR"},
        [{"name": "voltage", "query": "VOLT?", "write": "VOLT {}", "unit": "V"}],
        [{"name": "preset", "command": "SYST:PRES"}],
    )

    again = Hub(Config(tmp_path / "server.toml"))
    again.load()
    entry = again.entry("psu")
    assert entry.described
    assert entry.settings[0]["write"] == "VOLT {}"
    assert entry.actions[0]["command"] == "SYST:PRES"


# Checking an instrument is still there


class Absent(Sr830):
    """A lock-in that has been switched off underneath an open session."""

    def is_responding(self):
        return False


def test_a_health_check_notices_an_instrument_that_stopped_answering(monkeypatch):
    # An open handle is not a live instrument. A GPIB session outlives the box
    # being switched off, so the panel would otherwise sit on CONNECTED.
    monkeypatch.setattr("labdrivers.server.hub.find_driver", lambda name: Absent)
    holder = Hub(config=None)
    holder.add("lockin", "Absent", {"transport": RecordingTransport(default="0")})
    holder.read("lockin")
    assert holder.entry("lockin").status == "connected"

    assert holder.check_health() == ["lockin"]
    assert holder.entry("lockin").status == "error"
    assert "stopped answering" in holder.entry("lockin").error


def test_a_health_check_clears_an_error_once_it_answers_again(hub):
    hub.read("lockin")
    hub.entry("lockin").error = "something earlier"
    assert hub.check_health() == []
    assert hub.entry("lockin").status == "connected"


def test_a_health_check_says_nothing_about_an_instrument_never_used(hub):
    # A socket transport opens on its first command, so an instrument nobody
    # has spoken to yet has no observable state. Calling it dead puts a red
    # lamp and "stopped answering" on a Mercury that is merely idle.
    entry = hub.entry("lockin")
    assert not entry.used
    assert hub.check_health() == []
    assert entry.status == "connected"


def test_a_health_check_leaves_a_busy_instrument_alone(hub):
    # The lock serializes calls and not transactions, so a probe that queued
    # behind a measurement could land between its write and its query. An
    # instrument in use is demonstrably alive anyway.
    entry = hub.entry("lockin")
    transport = entry.instrument.transport
    entry.lock.acquire()
    try:
        transport.clear()
        assert hub.check_health() == []
        assert transport.commands == []
    finally:
        entry.lock.release()


def test_a_health_check_asks_the_instrument_when_it_is_free(hub):
    transport = hub.entry("lockin").instrument.transport
    transport.clear()
    hub.check_health()
    # The SR830 is not a ScpiInstrument, so the base answers from the transport
    # without sending anything. What matters is that it did not raise and did
    # not disturb anything.
    assert hub.entry("lockin").status == "connected"


def test_the_health_loop_stops_when_asked():
    holder = Hub(config=None)
    stopping = threading.Event()
    thread = threading.Thread(
        target=holder.watch_health, args=(0.05, stopping), daemon=True
    )
    thread.start()
    time.sleep(0.15)
    stopping.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_an_interval_of_zero_never_checks():
    holder = Hub(config=None)
    stopping = threading.Event()
    # Returns at once rather than looping, so the caller starts no thread.
    holder.watch_health(0, stopping)


# Who is using an instrument


def test_an_instrument_remembers_who_used_it(hub):
    hub.read("lockin", who="cryostat-pc")
    hub.io("lockin", {"kind": "write", "command": "PHAS 0"}, who="alex-laptop")
    assert hub.entry("lockin").recent_users() == ["alex-laptop", "cryostat-pc"]
    assert hub.entry("lockin").summary()["users"] == ["alex-laptop", "cryostat-pc"]


def test_somebody_who_has_gone_home_stops_showing_up(hub):
    entry = hub.entry("lockin")
    entry.note_user("cryostat-pc")
    entry.users["someone-else"] = time.time() - 3600
    assert entry.recent_users() == ["cryostat-pc"]


def test_an_unnamed_caller_is_not_recorded(hub):
    hub.read("lockin", who=None)
    assert hub.entry("lockin").summary()["users"] == []


def test_the_server_names_the_machine_a_request_came_from(client, hub):
    client.get(
        "/api/instruments/lockin/values",
        headers={"X-Labdrivers-Client": "cryostat-pc"},
    )
    assert "cryostat-pc" in client.get("/api/instruments").json()[0]["users"]


def test_a_caller_that_gives_no_name_falls_back_to_its_address(client, hub):
    client.get("/api/instruments/lockin/values")
    users = client.get("/api/instruments").json()[0]["users"]
    # The test client reports an address rather than a hostname.
    assert users and all(who for who in users)


def test_the_page_is_told_how_often_to_re_check(client):
    assert client.get("/api/settings").json()["refresh"] > 0


# Keeping what somebody wrote down


def test_a_setting_named_with_a_space_does_not_take_the_file_with_it():
    # A key going out unquoted means one space in a described setting makes
    # the whole file unparseable, and every instrument in it is gone next
    # start.
    assert quote_key("range") == "range"
    assert quote_key("write-terminator") == "write-terminator"
    assert quote_key("bad key") == '"bad key"'
    assert quote_key("with\rreturn") == '"with\\rreturn"'


def test_a_described_setting_with_an_odd_key_survives_the_round_trip(tmp_path):
    path = tmp_path / "server.toml"
    Config(path).save(
        [
            {
                "name": "probe",
                "driver": "Generic",
                "settings": [{"name": "v", "query": "V?", "bad key": 1}],
            }
        ]
    )
    assert [row["name"] for row in Config(path).load()] == ["probe"]


def test_reading_the_configuration_does_not_rewrite_it(tmp_path):
    # A load() that saved after each registration would leave the file
    # transiently holding a prefix of itself, and a crash partway would
    # truncate it.
    path = tmp_path / "server.toml"
    Config(path).save(
        [{"name": "mystery", "driver": "NoSuchDriver", "gpib_address": 9}]
    )
    before = path.read_bytes()
    Hub(config=Config(path)).load()
    assert path.read_bytes() == before


def test_an_instrument_this_server_cannot_register_is_not_erased(tmp_path):
    # A driver name with a spelling mistake in it is dropped from the running
    # server, which is right. Dropping it from the file as well throws away
    # the address and settings somebody typed.
    path = tmp_path / "server.toml"
    Config(path).save(
        [{"name": "mystery", "driver": "NoSuchDriver", "gpib_address": 9}]
    )

    holder = Hub(config=Config(path))
    holder.load()
    assert "mystery" not in holder.entries

    holder.save()
    rows = Config(path).load()
    assert [row["name"] for row in rows] == ["mystery"]
    assert rows[0]["gpib_address"] == 9


def test_starting_without_the_saved_instruments_does_not_forget_them(tmp_path):
    # --empty says not to load them. Adding anything afterwards would
    # otherwise write the file back holding only what this session added.
    path = tmp_path / "server.toml"
    Config(path).save([{"name": "magnet", "driver": "Ips120", "gpib_address": 25}])

    holder = Hub(config=Config(path))
    holder.unloaded = list(Config(path).load())
    holder.add(
        "lockin",
        "Sr830",
        {"transport": RecordingTransport(default="0")},
    )
    assert sorted(row["name"] for row in Config(path).load()) == ["lockin", "magnet"]


def test_a_name_has_to_be_usable_as_one(hub):
    # A name is the last part of the instrument's web address. One that is not
    # text breaks every later "no such instrument" message as well as its own,
    # and one with a slash in it can be added and then never removed.
    with pytest.raises(LabdriversError):
        hub.add(123, "Sr830", {"transport": RecordingTransport(default="0")})
    with pytest.raises(LabdriversError) as failure:
        hub.add("probe/2", "Sr830", {"transport": RecordingTransport(default="0")})
    assert "web address" in str(failure.value)

    # The instruments that were registered properly are still findable, and
    # saying what is registered still works.
    with pytest.raises(LabdriversError) as failure:
        hub.entry("nothing")
    assert "lockin" in str(failure.value)


def test_a_panel_shows_only_the_sub_devices_this_system_has():
    # Described from the class alone, which carries all three Mercury axes, a
    # one-axis Teslatron draws twenty rows that are permanent faults.
    instrument = MercuryIpsTeslatron(transport=RecordingTransport(default="0"))
    rows = introspect.describe_settings(type(instrument))
    kept = introspect.present_only(instrument, rows)

    assert any(row["name"].startswith("x.") for row in rows)
    assert not any(row["name"].startswith("x.") for row in kept)
    assert not any(row["name"].startswith("y.") for row in kept)
    assert any(row["name"].startswith("z.") for row in kept)


def test_a_key_that_is_not_a_bare_word_is_quoted_wherever_it_is_written(tmp_path):
    # Inline tables are not the only place a key is written. A connection
    # keyword or a server setting with a space in it goes out at the top level
    # of the file, and unquoted there it stops the whole file parsing.
    path = tmp_path / "server.toml"
    config = Config(path)
    config.settings = {"refresh": 20, "read termination": "\r"}
    config.save([{"name": "probe", "driver": "Generic", "write terminator": "\r\n"}])

    back = Config(path)
    rows = back.load()
    assert [row["name"] for row in rows] == ["probe"]
    assert rows[0]["write terminator"] == "\r\n"
    assert back.settings["read termination"] == "\r"


def test_removing_an_instrument_does_not_bring_back_a_set_aside_one(tmp_path):
    # A row set aside under the same name is filtered out of the file while a
    # live instrument holds that name. Removing the live one puts the old row
    # back, so the removal does not survive a restart.
    path = tmp_path / "server.toml"
    Config(path).save([{"name": "magnet", "driver": "NoSuchDriver", "gpib_address": 9}])

    holder = Hub(config=Config(path))
    holder.load()
    assert holder.unloaded

    holder.add("magnet", "Sr830", {"transport": RecordingTransport(default="0")})
    assert [row["name"] for row in Config(path).load()] == ["magnet"]

    holder.remove("magnet")
    assert Config(path).load() == []


def test_a_remote_driver_is_held_to_the_limits_the_server_was_given(monkeypatch):
    # The client rebuilt the driver from its name alone, so a magnet registered
    # at two tesla reached a notebook as the six-tesla default and a five-tesla
    # setpoint went out unremarked.
    # The description comes from a real hub rather than a canned dict, because
    # what the server leaves out of it is exactly how this went wrong: a field
    # limit is a dict, and a summary that carried only scalars dropped it.
    holder = Hub(config=None)
    holder.add(
        "magnet",
        "MercuryIps",
        {"transport": RecordingTransport(default="0"), "field_limits": {"GRPZ": 2.0}},
    )
    described = holder.describe("magnet")
    assert described["connection"]["field_limits"] == {"GRPZ": 2.0}
    monkeypatch.setattr(remote, "request", lambda *args, **keywords: described)

    supply = remote.connect("magnet", server="127.0.0.1:8000")
    assert supply.z.field_limit == 2.0
    with pytest.raises(RangeError):
        supply.z.field_setpoint = 5.0


def test_the_declared_python_version_is_one_the_package_is_run_on():
    # A floor above the interpreter the tests pass on means pip refuses to
    # install the package on the machine it was built on.
    import sys
    import tomllib

    with open("pyproject.toml", "rb") as handle:
        wanted = tomllib.load(handle)["project"]["requires-python"]
    floor = tuple(int(part) for part in wanted.lstrip(">=").split("."))
    assert sys.version_info[: len(floor)] >= floor
