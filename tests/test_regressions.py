"""Defects a driver could commit silently, each pinned so it cannot appear."""

import inspect
import socket
import threading
import time

import pytest

from labdrivers.coppermountain import Vna
from labdrivers.core.errors import (
    ConnectionFailure,
    InstrumentError,
    InstrumentTimeoutError,
    LabdriversError,
    RangeError,
    UnknownSetting,
)
from labdrivers.core.instrument import Instrument
from labdrivers.core.sweep import sweep_values
from labdrivers.core.transport import (
    DEFAULT_SERVER,
    RecordingTransport,
    RemoteTransport,
    SocketTransport,
)
from labdrivers.client import _address
from labdrivers.core.validators import check_choice, check_range, nearest_allowed
from labdrivers.keithley import Keithley2400
from labdrivers.keysight import InfiniiVision
from labdrivers.lakeshore import Ls332
from labdrivers.oxford import Ips120, Itc503, MercuryIps
from labdrivers.rigol import RigolDG1000Z
from labdrivers.server import generic
from labdrivers.server.drivers import available_drivers
from labdrivers.server.hub import Hub, resource_of
from labdrivers.server.introspect import describe_actions, describe_settings
from labdrivers.srs import Sr830


@pytest.mark.parametrize("stop,step", [(0.3, 0.1), (0.7, 0.1), (0.9, 0.3), (2.1, 0.7)])
def test_a_staircase_sweep_is_programmed_with_every_level(stop, step):
    # int() on a float division drops the last level whenever the division
    # falls a hair short, so 0.3 / 0.1 counts 3 levels instead of 4 and the
    # instrument stops with the source parked partway through the sweep.
    transport = RecordingTransport(
        responses={"*IDN?": "KEITHLEY,MODEL 2400", ":SOUR:FUNC?": "VOLT"},
        default="VOLT",
    )
    source = Keithley2400(transport=transport)
    transport.clear()

    points = source.configure_sweep(0, stop, step=step)
    assert points == len(sweep_values(0, stop, step=step))
    assert f":TRIG:COUN {points}" in transport.writes


def test_a_bound_named_on_one_side_is_still_enforced():
    # A supply described as never going below zero is relying on this, and a
    # check that needs both ends given would skip it entirely.
    floor = {"name": "voltage", "write": "VOLT {}", "minimum": 0}
    ceiling = {"name": "voltage", "write": "VOLT {}", "maximum": 30}

    with pytest.raises(RangeError) as failure:
        generic.validate(-500, floor)
    assert "at least 0" in str(failure.value)

    with pytest.raises(RangeError) as failure:
        generic.validate(1e9, ceiling)
    assert "at most 30" in str(failure.value)

    assert generic.validate(5, floor) == 5.0
    assert generic.validate(5, ceiling) == 5.0


def test_a_range_with_both_ends_reads_as_the_standard_sentence():
    with pytest.raises(RangeError) as failure:
        check_range(500, 0.1, 105.0, "compliance voltage", " V")
    assert str(failure.value) == (
        "The compliance voltage must be between 0.1 V and 105.0 V, but got 500 V."
    )


def test_scanning_skips_an_instrument_held_by_gpib_address():
    # Comparing only resource_name misses these, and a scan that misses one
    # opens a second session to an instrument this server already holds and
    # sends *IDN? into whatever measurement is running on it.
    assert resource_of({"gpib_address": 8}) == "GPIB0::8::INSTR"
    assert resource_of({"gpib_address": 8, "gpib_board": 1}) == "GPIB1::8::INSTR"
    assert resource_of({"resource_name": "ASRL3::INSTR"}) == "ASRL3::INSTR"
    assert resource_of({"ip_address": "192.168.0.11"}) is None


def test_an_instrument_never_spoken_to_is_not_called_dead(hub):
    # A socket transport opens on its first command, so is_open is False until
    # something is sent. A health check that trusts is_open reads that as a
    # dead instrument.
    entry = hub.entry("lockin")
    assert not entry.used
    assert hub.check_health() == []
    assert entry.status == "connected"


def test_using_an_instrument_marks_it_worth_checking(hub):
    hub.read("lockin")
    assert hub.entry("lockin").used


@pytest.mark.parametrize(
    "reply,shape",
    [
        ("SIN", "sine"),
        ("SQU", "square"),
        ("TRI", "triangle"),
        ("RAMP", "ramp"),
        ("PULS", "pulse"),
        ("DC", "dc"),
    ],
)
def test_the_generator_understands_its_own_short_replies(reply, shape):
    # A DG1000Z answers with the short form of the mnemonic. Matching against
    # the long form leaves the getter recognizing nothing, and feeding its
    # return value back into the setter raises.
    transport = RecordingTransport(
        responses={"*IDN?": "Rigol Technologies,DG1032Z,1,1"}, default=reply
    )
    generator = RigolDG1000Z(transport=transport)
    assert generator.waveform == shape
    generator.waveform = generator.waveform


def test_brightness_reads_back_in_the_units_it_was_set_in():
    transport = RecordingTransport(responses={"BRIGT?": "2"}, default="0")
    controller = Ls332(transport=transport)
    transport.clear()
    controller.display_brightness = 75
    assert transport.writes == ["BRIGT 2"]
    assert controller.display_brightness == 75


def test_an_empty_buffer_reads_as_nothing_rather_than_an_error():
    # An empty buffer is an ordinary state, not a bad argument, and raising
    # "the number of points must be between 1 and 16383" blames the caller
    # for a default they never chose.
    transport = RecordingTransport(responses={"SPTS?": "0"}, default="0")
    assert Sr830(transport=transport).read_buffer(1) == []


def test_a_connection_closed_mid_reply_is_not_a_reply():
    # Returning the fragment hands a driver a number that parses and is
    # wrong: half of "0.5432" reads as a perfectly plausible 0.5 T.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        connection.recv(1024)
        connection.sendall(b"STAT:DEV:GRPX:PSU:SIG:FLD:0.5")
        connection.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with pytest.raises(ConnectionFailure) as failure:
            SocketTransport("127.0.0.1", port=port, timeout=2).query("READ:X")
        assert "partway through a reply" in str(failure.value)
    finally:
        listener.close()


# Refusing to let somebody be quietly wrong


def test_a_misspelled_setting_is_refused_and_the_right_name_offered():
    # Python puts a new attribute on any object, so an unguarded assignment
    # sends nothing and the measurement runs at whatever the instrument was
    # already on.
    transport = RecordingTransport(default="0")
    lockin = Sr830(transport=transport)
    transport.clear()

    with pytest.raises(AttributeError) as failure:
        lockin.time_const = 0.3
    assert str(failure.value) == (
        "The Sr830 has no setting called 'time_const'. " "Did you mean 'time_constant'?"
    )
    assert transport.writes == []

    lockin.time_constant = 0.3
    assert transport.writes == ["OFLT 9"]


@pytest.mark.parametrize("name", sorted(available_drivers()))
def test_every_driver_refuses_an_invented_setting(name):
    instrument = available_drivers()[name](transport=RecordingTransport(default="0"))
    with pytest.raises(AttributeError) as failure:
        instrument.not_a_real_setting = 1
    # Nothing to suggest for a name unlike anything, so nothing is offered.
    assert str(failure.value) == (
        f"The {name} has no setting called 'not_a_real_setting'."
    )


def test_a_suggestion_is_offered_for_the_typos_people_actually_make():
    lockin = Sr830(transport=RecordingTransport(default="0"))
    for typo, meant in (
        ("sensitivty", "sensitivity"),  # dropped letter
        ("freqency", "frequency"),  # dropped letter
        ("time_constantt", "time_constant"),  # doubled letter
        ("time_cosntant", "time_constant"),  # transposed
        ("timeconstant", "time_constant"),  # missing underscore
        ("harmonics", "harmonic"),
    ):  # stray plural
        with pytest.raises(AttributeError) as failure:
            setattr(lockin, typo, 1)
        assert f"Did you mean '{meant}'?" in str(failure.value), typo


def test_a_driver_may_still_keep_its_own_attributes():
    # Several drivers set public attributes while building, and the check must
    # not start until construction is over.
    magnet = Ips120(transport=RecordingTransport(default="A0"), field_limit=8.0)
    assert magnet.field_limit == 8.0
    magnet.field_limit = 6.0
    assert magnet.field_limit == 6.0


def test_a_value_off_the_end_of_a_ladder_is_refused():
    # Snapping to the nearest is right inside the ladder. Outside it, a
    # sensitivity meant as 500 nV quietly becomes 1 V, two million times over.
    with pytest.raises(RangeError) as failure:
        nearest_allowed(500, [2e-9, 5e-9, 1e-8, 1.0], "sensitivity", " V")
    assert "between 2e-09 V and 1.0 V" in str(failure.value)

    with pytest.raises(RangeError):
        nearest_allowed(1e-12, [2e-9, 5e-9, 1e-8, 1.0], "sensitivity", " V")

    # Inside the ladder it still snaps.
    assert nearest_allowed(6e-9, [2e-9, 5e-9, 1e-8, 1.0], "sensitivity")[1] == 5e-9


# Reaching the server


def test_the_default_server_is_an_address_not_a_name():
    # localhost resolves to ::1 first on Windows while the server binds IPv4,
    # and urllib waits out the failed connection on every command: two seconds
    # each, against two milliseconds.
    assert "127.0.0.1" in DEFAULT_SERVER
    assert RemoteTransport("lockin").server == "http://127.0.0.1:8000"
    assert RemoteTransport("lockin", "localhost:8000").server == "http://127.0.0.1:8000"
    assert RemoteTransport("lockin", "cryostat-pc:8000").server == (
        "http://cryostat-pc:8000"
    )


def test_the_server_gives_up_before_its_caller_does():
    # Otherwise a command the caller abandoned still reaches the instrument,
    # and the retry that follows applies it twice.
    holder = Hub(config=None)
    holder.add("lockin", "Sr830", {"transport": RecordingTransport(default="0")})
    entry = holder.entry("lockin")

    entry.lock.acquire()
    try:
        start = time.monotonic()
        with pytest.raises(InstrumentTimeoutError):
            with entry.hold_for(client_timeout=3.0):
                pass
        assert time.monotonic() - start < 3.0
    finally:
        entry.lock.release()


# Walking away from something that delivers energy


@pytest.mark.parametrize(
    "name",
    [
        "Ips120",
        "Itc503",
        "Ls332",
        "Triton200",
        "MercuryIps",
        "Keithley2400",
        "Keithley6221",
    ],
)
def test_anything_that_delivers_energy_can_be_left_safely(name):
    assert hasattr(available_drivers()[name], "safe_shutdown")


def test_the_heater_controller_turns_its_heater_off():
    transport = RecordingTransport(responses={"RANGE?": "0"}, default="0")
    controller = Ls332(transport=transport)
    transport.clear()
    controller.safe_shutdown()
    assert transport.writes == ["RANGE 0"]


def test_the_legacy_controller_goes_manual_before_it_goes_to_zero():
    # The other order would let automatic control drive the heater back up.
    transport = RecordingTransport(
        responses=lambda command: (
            "R+0.0000" if command[0] == "R" else command[0] + "0"
        )
    )
    controller = Itc503(transport=transport)
    transport.clear()
    controller.safe_shutdown()
    assert transport.commands == ["A0", "O0.0"]


# Knowing an instrument has gone


def test_the_mercury_line_is_asked_rather_than_assumed():
    # These are not SCPI, so the check they would otherwise inherit only
    # reports whether a socket object exists, and a magnet switched off goes
    # on looking healthy.
    assert MercuryIps.is_responding is not Instrument.is_responding

    alive = MercuryIps(transport=RecordingTransport(default="ok"))
    assert alive.is_responding()

    class Dead(RecordingTransport):
        def query(self, command):
            raise ConnectionFailure("connection reset")

    assert not MercuryIps(transport=Dead()).is_responding()


# Readings a panel could not show


def test_a_temperature_controller_shows_its_temperature():
    rows = {s["name"] for s in describe_settings(Ls332)}
    assert "temperature" in rows

    holder = Hub(config=None)
    holder.add(
        "fridge",
        "Ls332",
        {"transport": RecordingTransport(responses={"KRDG? A": "4.2000"}, default="0")},
    )
    assert holder.read("fridge")["temperature"]["value"] == 4.2


@pytest.mark.parametrize("name", ["Itc503", "Triton200"])
def test_the_other_temperature_controllers_do_too(name):
    rows = {s["name"] for s in describe_settings(available_drivers()[name])}
    assert "temperature" in rows


def test_a_panel_does_not_pull_in_something_slow():
    # measure() deliberately waits and read_waveform() transfers a whole trace,
    # so neither belongs in a reading of every value on a panel.
    for driver, heavy in (
        (Sr830, "measure"),
        (InfiniiVision, "read_waveform"),
        (Vna, "read_trace"),
    ):
        assert heavy not in {s["name"] for s in describe_settings(driver)}


# Where a driver can contradict its own documentation


def test_forcing_the_switch_heater_is_named_for_what_it_does():
    # Code 2 opens the switch without checking the supply output against the
    # current already in the magnet: it forces the heater on. A label of "off
    # forced" turns it on, with that check skipped, for a request that meant
    # off.
    transport = RecordingTransport({"H2": "H"}, default="A0")
    magnet = Ips120(transport=transport, field_limit=8.0)
    magnet.switch_heater = "on forced"
    assert transport.last_command == "H2"

    with pytest.raises(RangeError):
        magnet.switch_heater = "off forced"


def test_the_magnet_display_is_switched_with_the_mode_command():
    # F selects which parameter the front panel shows, so F9 points the
    # display at parameter 9 instead of putting it into tesla. The units
    # switch is M.
    transport = RecordingTransport({"M9": "M", "M8": "M"}, default="A0")
    magnet = Ips120(transport=transport, field_limit=8.0)
    magnet.set_display("tesla")
    assert transport.last_command == "M9"


def test_a_reading_comes_back_in_the_order_it_was_asked_for():
    # The instrument sends the fields it was told to send in its own order, so
    # asking for current and then voltage hands back the voltage first, and a
    # caller trusting the request order reads one as the other.
    transport = RecordingTransport({":READ?": "0.5,0.001"}, default="0")
    meter = Keithley2400(transport=transport)
    assert meter.read("current", "voltage") == [0.001, 0.5]
    assert meter.read("voltage", "current") == [0.5, 0.001]


def test_a_socket_that_died_mid_reply_is_not_kept_for_the_next_command():
    # A dead socket cleared under a misspelled attribute name stays cached,
    # the transport still calls itself open, and the reconnect it promises
    # never happens.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        connection.recv(1024)
        connection.sendall(b"0.5")
        connection.close()

    threading.Thread(target=serve, daemon=True).start()
    transport = SocketTransport("127.0.0.1", port=port, timeout=2)
    try:
        with pytest.raises(ConnectionFailure):
            transport.query("READ:X")
        assert not transport.is_open
    finally:
        transport.close()
        listener.close()


def test_a_driver_built_on_a_driver_still_catches_a_mistyped_setting():
    # A guard that arms only for the class writing the constructor leaves a
    # lab subclass with no constructor of its own unprotected.
    class HouseSr830(Sr830):
        pass

    transport = RecordingTransport(default="0")
    lockin = HouseSr830(transport=transport)
    transport.clear()

    with pytest.raises(AttributeError) as failure:
        lockin.time_const = 0.3
    assert "time_constant" in str(failure.value)
    assert transport.writes == []


def test_the_last_bin_of_the_buffer_is_one_below_its_size():
    # A 16383-point buffer numbers its bins 0 to 16382, and a read that starts
    # inside it can still run off the end.
    lockin = Sr830(transport=RecordingTransport(default="0"))
    with pytest.raises(RangeError):
        lockin.read_buffer(1, start=16383, count=1)
    with pytest.raises(RangeError) as failure:
        lockin.read_buffer(1, start=16000, count=1000)
    assert "past the end" in str(failure.value)


def test_reading_a_panel_does_not_take_the_status_flags_away():
    # Both of these clear when they are read, so a panel refresh would carry
    # off the overload and error flags the measurement needed to see.
    rows = {setting["name"] for setting in describe_settings(Sr830)}
    assert "lockin_status" not in rows
    assert "error_status" not in rows


def test_an_operation_that_cannot_run_does_not_reach_the_instrument(hub):
    # A check sitting after the block that takes the lock and opens the
    # connection lets a malformed request touch the hardware before failing.
    entry = hub.entry("lockin")
    with pytest.raises(LabdriversError):
        hub.io("lockin", {"kind": "twiddle", "command": "*IDN?"})
    assert not entry.used


# The package's outer surface


@pytest.mark.parametrize(
    "build",
    [
        lambda t: Sr830(transport=t),
        lambda t: Ls332(transport=t),
        lambda t: Ips120(transport=t, field_limit=8.0),
        lambda t: Itc503(transport=t),
    ],
)
def test_an_instrument_switched_off_underneath_the_server_is_noticed(build):
    # These four speak their own languages rather than SCPI, so the check they
    # would otherwise inherit only reports whether a handle exists. A GPIB
    # session outlives the instrument being switched off, so their panels
    # would say CONNECTED over readings that have stopped being true.
    transport = RecordingTransport(default="A0")
    instrument = build(transport)
    assert instrument.is_responding()

    transport.responses = {}
    transport.default = None
    assert not instrument.is_responding()


@pytest.mark.parametrize(
    "given,expected",
    [
        ("localhost:8000", "http://127.0.0.1:8000"),
        ("http://localhost:8000/api", "http://127.0.0.1:8000/api"),
        ("cryo.localhost:8000", "http://cryo.localhost:8000"),
        ("localhost.lab.example:8000", "http://localhost.lab.example:8000"),
        ("mylocalhost:8000", "http://mylocalhost:8000"),
        ("cryostat.lab:9000", "http://cryostat.lab:9000"),
    ],
)
def test_only_the_host_actually_called_localhost_is_written_as_an_address(
    given, expected
):
    # Replacing the name wherever it appears in the string sends a real
    # machine called cryo.localhost to cryo.127.0.0.1.
    assert _address(given) == expected


def test_a_choice_is_checked_before_it_is_made_a_number():
    # int() running first turns a value off the list into a ValueError from
    # the conversion instead of the sentence the rest of the package uses,
    # and silently truncates a value between two choices onto one of them.
    lockin = Sr830(transport=RecordingTransport(default="0"))
    with pytest.raises(RangeError) as failure:
        lockin.filter_slope = 12.5
    assert "12.5" in str(failure.value)

    with pytest.raises(RangeError):
        lockin.filter_slope = "0.5"

    # The page sends what was typed, as text, and a notebook may work a value
    # out as a float. Both still name a real choice.
    assert check_choice("12", {6: 0, 12: 1, 18: 2}, "slope") == 1
    assert check_choice(12.0, {6: 0, 12: 1, 18: 2}, "slope") == 1


# Where one composition further out defeats a guard


def test_a_class_may_add_settings_by_declaring_them():
    # The class is the whole list of what an object has, whatever order the
    # classes are put together in. A mixin declares what it fills in, exactly
    # as a driver does, and gets the same protection from a misspelling.
    class Logging:
        log = None

        def __init__(self, *args, **keywords):
            super().__init__(*args, **keywords)
            self.log = []

    class LoggedSr830(Logging, Sr830):
        pass

    lockin = LoggedSr830(transport=RecordingTransport(default="0"))
    assert lockin.log == []

    with pytest.raises(AttributeError) as failure:
        lockin.time_const = 0.3
    assert "time_constant" in str(failure.value)


def test_an_axis_answers_to_either_of_its_names():
    # The boards are GRPX, GRPY and GRPZ and the properties are x, y and z, so
    # a limit named for the property reached the wrong key and a magnet
    # declared at 0.5 T kept the six-tesla default.
    for named in ({"z": 0.5}, {"GRPZ": 0.5}, {"Z": 0.5}):
        supply = MercuryIps(
            transport=RecordingTransport(default="0"), field_limits=named
        )
        assert supply.z.field_limit == 0.5

    one_axis = MercuryIps(transport=RecordingTransport(default="0"), axes=("z",))
    assert sorted(one_axis.magnets) == ["GRPZ"]


def test_an_expansion_is_checked_before_it_is_made_a_number():
    lockin = Sr830(transport=RecordingTransport(default="0"))
    with pytest.raises(RangeError):
        lockin.set_offset_and_expand("x", 0.0, 100.7)


@pytest.mark.parametrize("points", [2.9, 10.5, "7.5"])
def test_a_sweep_needs_a_whole_number_of_points(points):
    # int() rounds a mistake down instead of reporting it, so a sweep asked for
    # 2.9 points quietly runs two.
    with pytest.raises(RangeError) as failure:
        sweep_values(0, 1, points=points)
    assert "whole number" in str(failure.value)


# What a fresh reader found by asking what the package does not cover


def test_a_setting_misspelled_on_an_axis_is_refused_too():
    # An instrument is not the only thing that carries settings. A magnet axis
    # is a plain object handed out by the driver, and a name misspelled on one
    # of those assigns and sends nothing exactly as it would on the driver.
    supply = MercuryIps(transport=RecordingTransport(default="0"))
    with pytest.raises(AttributeError) as failure:
        supply.z.feild_setpoint = 1.0
    assert "field_setpoint" in str(failure.value)

    # And the real one still works.
    assert isinstance(type(supply.z).field_setpoint, property)


def test_the_rotation_probe_and_the_quantum_design_systems_refuse_them_as_well():
    from labdrivers.funky_rotator.rotator import Rotator
    from labdrivers.quantumdesign.qdinstrument import QdInstrument

    for driver in (Rotator, QdInstrument):
        assert "__setattr__" in dir(driver)
        assert driver.__setattr__ is not object.__setattr__


@pytest.mark.parametrize(
    "name", ["Keithley2400", "Keithley6221", "MercuryIps", "Ips120"]
)
def test_the_key_for_leaving_an_instrument_safe_is_on_its_panel(name):
    # It takes defaulted arguments on several drivers, which kept it off the
    # panel it is most wanted on.
    driver = available_drivers()[name]
    assert hasattr(driver, "safe_shutdown")
    assert "safe_shutdown" in {action["name"] for action in describe_actions(driver)}


def test_a_timeout_can_be_caught_as_a_timeout():
    # RangeError is a ValueError for the same reason: catch what you would
    # naturally reach for.
    assert issubclass(InstrumentTimeoutError, TimeoutError)
    assert issubclass(RangeError, ValueError)


def test_setting_up_a_source_refuses_the_one_that_has_no_level():
    # Memory mode is a source function the instrument has, and naming it here
    # quietly set up a voltage source instead.
    meter = Keithley2400(transport=RecordingTransport(default="VOLT"))
    with pytest.raises(RangeError) as failure:
        meter.configure_source("memory", level=1.0)
    assert "memory" in str(failure.value)


def test_a_source_function_the_package_does_not_know_is_reported():
    # An unrecognized reply reached a dict lookup and came back as KeyError
    # naming the raw reply, which tells a physicist nothing.
    meter = Keithley2400(transport=RecordingTransport(default="0"))
    with pytest.raises(InstrumentError) as failure:
        meter.source_value = 1.0
    assert "'0'" in str(failure.value)


def test_display_text_refuses_a_quote_it_cannot_send():
    # The message travels inside a quoted string and SCPI cannot escape a quote
    # within one, so the rest of the message would arrive as commands.
    meter = Keithley2400(transport=RecordingTransport(default="VOLT"))
    with pytest.raises(RangeError):
        meter.display_text = 'say "hello"'


def test_a_two_axis_trace_is_not_handed_back_as_one_number_a_point():
    # Smith and polar traces carry two numbers a point and both mean something.
    analyzer = Vna(
        transport=RecordingTransport(
            {
                ":SENS1:FREQ:DATA?": "1e9,2e9",
                ":CALC1:TRAC1:DATA:FDAT?": "0.5,0.25,0.6,0.35",
            },
            default="1",
        )
    )
    with pytest.raises(InstrumentError) as failure:
        analyzer.read_trace(1)
    assert "read_complex_trace" in str(failure.value)


# What three people looking at it as their own kind of work found


def test_assigning_to_something_the_instrument_does_is_refused():
    # The likelier mistake than a typo, because the same word is a setting on a
    # different instrument in the same rack. Assignment lands on the method,
    # sends nothing, and the measurement runs at whatever was already set.
    lockin = Sr830(transport=RecordingTransport(default="0"))
    with pytest.raises(UnknownSetting) as failure:
        lockin.output = True
    assert str(failure.value) == (
        "'output' on the Sr830 is something it does, not something it has. "
        "Call output(...) instead of assigning to it."
    )

    controller = Ls332(transport=RecordingTransport(default="0"))
    with pytest.raises(UnknownSetting):
        controller.setpoint = 4.2


def test_a_refused_setting_is_catchable_both_ways():
    # The README promises one except catches everything the package raises, and
    # assigning to a name an object does not have has always been AttributeError.
    assert issubclass(UnknownSetting, LabdriversError)
    assert issubclass(UnknownSetting, AttributeError)


def test_a_timeout_carries_the_name_it_is_documented_under():
    # The class was named with a trailing underscore, so a traceback showed a
    # name that appears nowhere in the documentation.
    assert InstrumentTimeoutError.__name__ == "InstrumentTimeoutError"


def test_a_temperature_controller_shows_a_temperature():
    # The Mercury iTC reads a named sensor, so its reading needed an argument
    # and the panel walk skipped it. A panel for a temperature controller then
    # had a needle valve on it and no temperature.
    from labdrivers.oxford.mercuryitc import MercuryItc

    rows = {s["name"] for s in describe_settings(MercuryItc)}
    assert "temperature" in rows


@pytest.mark.parametrize(
    "documentation,unit",
    [
        ("Returns the rate the field ramps at, in tesla per minute.", "T/min"),
        ("Returns the sweep rate, in amps per minute.", "A/min"),
        ("Returns the ramp rate, in kelvin per minute.", "K/min"),
        ("Returns the vertical scale, in volts per division.", "V/div"),
        ("Returns the temperature, in degrees Celsius.", "degC"),
        ("Returns the field, in tesla.", "T"),
        ("Returns the angle, in degrees.", "deg"),
    ],
)
def test_a_rate_is_labelled_as_a_rate(documentation, unit):
    # Matching the first half of a compound unit put a magnet ramping at
    # 0.5 T/min on the page as 0.5 T, beside the field it was ramping.
    from labdrivers.server.introspect import unit_of

    assert unit_of(documentation) == unit


def test_a_sweep_will_not_start_with_the_heater_switched_off():
    # Nothing in the wait can tell a heater that is off from a cryostat that is
    # slow, so without asking first the answer arrives with the timeout, two
    # hours later.
    controller = Ls332(transport=RecordingTransport({"RANGE?": "0"}, default="4.2"))
    with pytest.raises(InstrumentError) as failure:
        list(controller.sweep_temperature(2, 4, points=3))
    assert "heater range is off" in str(failure.value)


def test_a_field_sweep_will_not_start_with_the_switch_heater_closed():
    # The sweep completes with the magnet parked at one field, and every point
    # reads the same number.
    closed = MercuryIps(
        transport=RecordingTransport(default="STAT:DEV:GRPZ:PSU:SIG:SWHT:OFF")
    )
    with pytest.raises(InstrumentError) as failure:
        list(closed.z.sweep_field(-0.4, 0.4, points=3))
    assert "switch heater is closed" in str(failure.value)


def test_a_magnet_that_cannot_say_is_left_alone():
    # Only a definite OFF stops a sweep. A magnet with no persistent switch
    # fitted has nothing to say about one, and has to keep working.
    mute = MercuryIps(transport=RecordingTransport(default="0"))
    assert mute.z._switch_heater_word() != "OFF"


def test_the_rotation_probe_raises_what_the_package_raises():
    # It was the one driver outside the taxonomy the README presents as
    # universal, so except RangeError around a rotator caught nothing.
    from labdrivers.funky_rotator import rotator

    source = inspect.getsource(rotator)
    assert "raise RuntimeError(" not in source
    assert "RangeError" in source and "InstrumentTimeoutError" in source
