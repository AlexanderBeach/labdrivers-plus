"""Tests for the shared foundation.

The recording transport makes the exact bytes a driver would put on the wire
visible, so these tests assert command strings directly.
"""

import logging

import pytest

from labdrivers.core import (
    ConnectionFailure,
    Instrument,
    InstrumentError,
    InstrumentTimeoutError,
    LabdriversError,
    RangeError,
    RecordingTransport,
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
    nearest_allowed,
    open_transport,
)


@pytest.fixture
def transport():
    return RecordingTransport()


@pytest.fixture
def instrument(transport):
    return Instrument(transport=transport)


# Errors


def test_every_error_is_a_runtime_error():
    """Lab scripts catch RuntimeError, so that must keep working."""
    for error in (
        LabdriversError,
        ConnectionFailure,
        InstrumentError,
        RangeError,
        InstrumentTimeoutError,
    ):
        assert issubclass(error, RuntimeError)


def test_range_error_is_also_a_value_error():
    assert issubclass(RangeError, ValueError)


# Validators


def test_check_range_accepts_the_endpoints():
    assert check_range(0, 0, 10, "level") == 0.0
    assert check_range(10, 0, 10, "level") == 10.0


@pytest.mark.parametrize("value", [-0.001, 10.001, 1e6])
def test_check_range_rejects_outside(value):
    with pytest.raises(RangeError, match="must be between"):
        check_range(value, 0, 10, "level")


def test_check_range_message_names_the_range_and_unit():
    with pytest.raises(RangeError) as caught:
        check_range(1, 2e-12, 0.105, "wave amplitude", " A")
    message = str(caught.value)
    assert "wave amplitude" in message
    assert "2e-12 A" in message
    assert "0.105 A" in message
    assert "but got 1 A" in message


def test_check_range_rejects_non_numbers():
    with pytest.raises(RangeError, match="must be a number"):
        check_range("banana", 0, 10, "level")


def test_check_integer_range_rejects_fractions():
    assert check_integer_range(3, 1, 10, "count") == 3
    with pytest.raises(RangeError, match="whole number"):
        check_integer_range(2.5, 1, 10, "count")


def test_check_choice_is_case_insensitive():
    assert check_choice("MOVING", ["moving", "repeating"], "filter type") == "moving"


def test_check_choice_maps_to_the_instrument_string():
    assert check_choice("best", {"best": "BEST", "fixed": "FIX"}, "range") == "BEST"


def test_check_choice_lists_what_is_accepted():
    with pytest.raises(RangeError) as caught:
        check_choice("sideways", ["moving", "repeating"], "filter type")
    assert "'moving'" in str(caught.value)
    assert "'repeating'" in str(caught.value)


@pytest.mark.parametrize(
    "value", [1, True, "on", "ON", "True", "yes", "enable", "enabled"]
)
def test_check_boolean_accepts_true_spellings(value):
    assert check_boolean(value, "output") is True


@pytest.mark.parametrize(
    "value", [0, False, "off", "OFF", "False", "no", "disable", "disabled"]
)
def test_check_boolean_accepts_false_spellings(value):
    assert check_boolean(value, "output") is False


def test_check_boolean_rejects_nonsense():
    with pytest.raises(RangeError, match="can either be"):
        check_boolean("maybe", "output")


def test_nearest_allowed_snaps_to_the_closest_setting():
    ladder = [1e-3, 3e-3, 10e-3, 30e-3, 100e-3]
    index, value = nearest_allowed(47e-3, ladder, "time constant", " s")
    assert (index, value) == (3, 30e-3)


# Transport selection


def test_gpib_address_becomes_a_resource_string(monkeypatch):
    built = {}

    class FakeVisaTransport:
        def __init__(self, resource_name, **kwargs):
            built["resource_name"] = resource_name

    monkeypatch.setattr("labdrivers.core.transport.VisaTransport", FakeVisaTransport)
    open_transport(gpib_address=24)
    assert built["resource_name"] == "GPIB0::24::INSTR"


def test_an_injected_transport_is_used_as_is(transport):
    assert open_transport(transport=transport) is transport


def test_specifying_nothing_says_what_to_pass():
    with pytest.raises(ConnectionFailure, match="gpib_address"):
        open_transport()


# Recording transport


def test_recording_transport_records_writes(instrument, transport):
    instrument.write("OUTP ON")
    instrument.write("SOUR:CURR 0.001")
    assert transport.commands == ["OUTP ON", "SOUR:CURR 0.001"]
    assert transport.last_command == "SOUR:CURR 0.001"


def test_unexpected_query_fails_loudly(instrument):
    """A query with no canned reply must fail, not silently return ''."""
    with pytest.raises(ConnectionFailure, match="no reply configured"):
        instrument.query("VOLT?")


def test_responses_may_be_a_callable():
    transport = RecordingTransport(responses=lambda command: command.upper())
    assert Instrument(transport=transport).query("idn?") == "IDN?"


# Reply parsing


def test_query_float_parses():
    instrument = Instrument(transport=RecordingTransport({"VOLT?": "1.234E-03"}))
    assert instrument.query_float("VOLT?") == pytest.approx(1.234e-3)


def test_query_float_reports_a_bad_reply():
    instrument = Instrument(transport=RecordingTransport({"VOLT?": "OVERLOAD"}))
    with pytest.raises(InstrumentError, match="Expected a number"):
        instrument.query_float("VOLT?")


def test_query_integer_tolerates_float_form():
    """Instruments often answer an integer query as '1.000000E+00'."""
    instrument = Instrument(transport=RecordingTransport({"COUN?": "1.000000E+00"}))
    assert instrument.query_integer("COUN?") == 1


def test_query_floats_splits_on_commas():
    instrument = Instrument(transport=RecordingTransport({"READ?": "1.0,2.5,-3.0"}))
    assert instrument.query_floats("READ?") == [1.0, 2.5, -3.0]


@pytest.mark.parametrize("reply", ["1", "ON", "1.000000E+00"])
def test_query_boolean_reads_true(reply):
    instrument = Instrument(transport=RecordingTransport({"OUTP?": reply}))
    assert instrument.query_boolean("OUTP?") is True


@pytest.mark.parametrize("reply", ["0", "OFF", "0.000000E+00"])
def test_query_boolean_reads_false(reply):
    instrument = Instrument(transport=RecordingTransport({"OUTP?": reply}))
    assert instrument.query_boolean("OUTP?") is False


def test_query_boolean_does_not_compare_a_string_to_an_int():
    """A queried "0" must read as False.

    Comparing the raw reply string against an integer never matches, so the
    conversion has to happen before the comparison.
    """
    instrument = Instrument(transport=RecordingTransport({"OUTP:STAT?": "0"}))
    assert instrument.query_boolean("OUTP:STAT?") is False


# Waiting


def test_wait_until_returns_when_satisfied(instrument):
    calls = []

    def condition():
        calls.append(1)
        return len(calls) >= 3

    instrument.wait_until(condition, timeout=5, interval=0.001)
    assert len(calls) == 3


def test_wait_until_times_out_with_a_description(instrument):
    with pytest.raises(InstrumentTimeoutError, match="the field to settle"):
        instrument.wait_until(
            lambda: False,
            timeout=0.02,
            interval=0.001,
            description="the field to settle",
        )


# SCPI common commands


@pytest.fixture
def scpi():
    return ScpiInstrument(
        transport=RecordingTransport(
            {
                "*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234,C30",
                "*TST?": "0",
                "*OPC?": "1",
                "*ESR?": "0",
                "*STB?": "0",
                "SYST:ERR?": '0,"No error"',
            }
        )
    )


def test_identify(scpi):
    assert "MODEL 2400" in scpi.identify()


def test_reset_and_clear_send_the_standard_commands(scpi):
    scpi.reset()
    scpi.clear_status()
    assert scpi.transport.commands == ["*RST", "*CLS"]


def test_verify_identity_accepts_the_right_model(scpi):
    scpi.IDENTIFIER = "MODEL 2400"
    assert "MODEL 2400" in scpi.verify_identity()


def test_verify_identity_rejects_the_wrong_model(scpi):
    scpi.IDENTIFIER = "MODEL 6221"
    with pytest.raises(InstrumentError, match="identifies itself as"):
        scpi.verify_identity()


def test_empty_error_queue_reads_clean(scpi):
    assert scpi.errors() == []
    scpi.check_errors()


def test_check_errors_raises_and_reports_the_code():
    replies = iter(
        [
            '-113,"Undefined header"',
            '-222,"Parameter data out of range"',
            '0,"No error"',
        ]
    )
    instrument = ScpiInstrument(
        transport=RecordingTransport(responses=lambda command: next(replies))
    )
    with pytest.raises(InstrumentError) as caught:
        instrument.check_errors()
    assert "Undefined header" in str(caught.value)
    assert "out of range" in str(caught.value)
    assert caught.value.code == -113


def test_error_queue_drain_stops_at_no_error():
    replies = iter(['-113,"Undefined header"', '0,"No error"'])
    instrument = ScpiInstrument(
        transport=RecordingTransport(responses=lambda command: next(replies))
    )
    assert instrument.errors() == [(-113, "Undefined header")]


# Lifetime


def test_context_manager_closes(transport):
    with Instrument(transport=transport):
        pass
    assert transport.closed


def test_repr_names_the_class_and_transport(instrument):
    assert "Instrument" in repr(instrument)


# Logging


def test_no_handler_is_installed_on_import():
    """Importing a library must not configure logging for the whole program."""
    package_logger = logging.getLogger("labdrivers")
    installed = [
        handler
        for handler in package_logger.handlers
        if not isinstance(handler, logging.NullHandler)
    ]
    assert installed == []


def test_writes_and_queries_are_recorded_not_inferred():
    """A trailing '?' does not identify a query on every instrument.

    The SR830 writes its queries as 'OUTP? 1', so classifying by suffix would
    miscount them.
    """
    transport = RecordingTransport({"OUTP? 1": "0.5"})
    instrument = Instrument(transport=transport)
    instrument.write("PHAS 45")
    instrument.query("OUTP? 1")
    assert transport.writes == ["PHAS 45"]
    assert transport.queries == ["OUTP? 1"]


def test_clear_forgets_both_writes_and_queries():
    transport = RecordingTransport({"X?": "1"})
    instrument = Instrument(transport=transport)
    instrument.write("A 1")
    instrument.query("X?")
    transport.clear()
    assert (transport.commands, transport.writes, transport.queries) == ([], [], [])
