"""Tests for the Lake Shore 332, the NI DAQ driver and the Quantum Design wrapper.

The NI and Quantum Design drivers wrap third-party libraries that are not
installed on every machine, so those tests check the import guards and the
argument validation rather than driving hardware.
"""

import sys
import types

import pytest

from labdrivers.core import ConnectionFailure, RangeError, RecordingTransport
from labdrivers.lakeshore.ls332 import HEATER_RANGES, Ls332
from labdrivers.ni.nidaq import Nidaq
from labdrivers.quantumdesign.qdinstrument import (
    INSTRUMENT_TYPES,
    Dynacool,
    QdInstrument,
)


def build(responses=None):
    transport = RecordingTransport(responses or {})
    return Ls332(transport=transport), transport


@pytest.fixture
def controller():
    return build()


# Lake Shore 332


def test_subpackage_imports():
    """The subpackage must import on a stock pyvisa install."""
    import labdrivers.lakeshore

    assert labdrivers.lakeshore.Ls332 is Ls332


def test_temperature_reads_each_input():
    instrument, transport = build({"KRDG? A": "4.2", "KRDG? B": "77.0"})
    assert instrument.temperatures() == {"A": 4.2, "B": 77.0}
    assert transport.queries == ["KRDG? A", "KRDG? B"]


def test_input_letter_is_case_insensitive():
    instrument, transport = build({"KRDG? A": "4.2"})
    assert instrument.temperature("a") == 4.2


def test_unknown_input_is_rejected(controller):
    instrument, _ = controller
    with pytest.raises(RangeError, match="sensor input"):
        instrument.temperature("C")


def test_setpoint_command(controller):
    instrument, transport = controller
    instrument.setpoint(1, 4.2)
    assert transport.last_command == "SETP 1,4.2"


def test_loop_number_is_bounded(controller):
    instrument, _ = controller
    with pytest.raises(RangeError, match="control loop"):
        instrument.setpoint(3, 4.2)


def test_heater_range(controller):
    instrument, transport = controller
    instrument.heater_range = "medium"
    assert transport.last_command == "RANGE 2"
    with pytest.raises(RangeError, match="heater range"):
        instrument.heater_range = "turbo"


def test_heater_range_reads_back():
    instrument, _ = build({"RANGE?": "3"})
    assert instrument.heater_range == "high"


def test_heater_ranges_match_the_manual():
    assert HEATER_RANGES == {"off": 0, "low": 1, "medium": 2, "high": 3}


def test_reading_status_decodes_the_bit_field():
    """A 332 reports a bad sensor as a status bit, not as a silly number, so a
    reading of 0 K may just mean the sensor is unplugged."""
    instrument, _ = build({"RDGST? A": "0"})
    assert instrument.reading_status("A") == (True, "ok")

    instrument, _ = build({"RDGST? A": "16"})
    ok, description = instrument.reading_status("A")
    assert ok is False
    assert "under range" in description


def test_heater_status_names_the_fault():
    instrument, _ = build({"HTRST?": "5"})
    ok, description = instrument.heater_status()
    assert ok is False
    assert "open heater load" in description


def test_configure_loop_command(controller):
    instrument, transport = controller
    instrument.configure_loop(1, "B", units="kelvin", powerup_enable=False)
    assert transport.last_command == "CSET 1,B,1,0,1"


def test_pid_reads_all_three():
    instrument, _ = build({"PID? 1": "50.0,20.0,0.0"})
    assert instrument.pid(1) == (50.0, 20.0, 0.0)


def test_pid_keeps_untouched_terms():
    instrument, transport = build({"PID? 1": "50.0,20.0,0.0"})
    instrument.pid(1, proportional=100)
    assert transport.last_command == "PID 1,100,20.0,0.0"


def test_ramp_rate_is_bounded():
    instrument, _ = build({"RAMP? 1": "1,5.0"})
    with pytest.raises(RangeError, match="ramp rate"):
        instrument.ramp(1, enabled=True, rate=500)


def test_lock_code_is_three_digits(controller):
    instrument, transport = controller
    instrument.lock_front_panel(True, code=7)
    assert transport.last_command == "LOCK 1,007"


# NI DAQ


def test_missing_nidaqmx_says_what_to_install(monkeypatch):
    """The driver should name the package rather than raising ImportError."""
    monkeypatch.setitem(sys.modules, "nidaqmx", None)
    monkeypatch.delitem(sys.modules, "nidaqmx", raising=False)

    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__
    )

    def fake_import(name, *args, **kwargs):
        if name == "nidaqmx":
            raise ImportError("no nidaqmx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ConnectionFailure, match="pip install nidaqmx"):
        Nidaq("Dev1")


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    """A stand-in nidaqmx module recording the tasks a driver builds."""
    module = types.ModuleType("nidaqmx")
    recorded = {"analog_input": [], "analog_output": [], "written": []}

    class Channels:
        def __init__(self, kind):
            self.kind = kind

        def add_ai_voltage_chan(self, name, **kwargs):
            recorded["analog_input"].append((name, kwargs))

        def add_ai_current_chan(self, name, **kwargs):
            recorded["analog_input"].append((name, kwargs))

        def add_ao_voltage_chan(self, name, **kwargs):
            recorded["analog_output"].append((name, kwargs))

    class Task:
        def __init__(self):
            self.ai_channels = Channels("ai")
            self.ao_channels = Channels("ao")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, number_of_samples_per_channel=1):
            return (
                1.23
                if number_of_samples_per_channel == 1
                else [1.23] * (number_of_samples_per_channel)
            )

        def write(self, value, auto_start=True):
            recorded["written"].append(value)

    class TerminalConfiguration:
        DEFAULT = "DEFAULT"
        DIFF = "DIFF"
        RSE = "RSE"
        NRSE = "NRSE"
        PSEUDO_DIFF = "PSEUDO_DIFF"

    module.Task = Task
    module.constants = types.SimpleNamespace(
        TerminalConfiguration=TerminalConfiguration
    )
    monkeypatch.setitem(sys.modules, "nidaqmx", module)
    return recorded


def test_channel_names_are_qualified_with_the_device(fake_nidaqmx):
    daq = Nidaq("Dev2")
    daq.read_voltage("ai0")
    assert fake_nidaqmx["analog_input"][0][0] == "Dev2/ai0"


def test_a_fully_qualified_channel_is_left_alone(fake_nidaqmx):
    daq = Nidaq("Dev1")
    daq.read_voltage("Dev3/ai5")
    assert fake_nidaqmx["analog_input"][0][0] == "Dev3/ai5"


def test_output_voltage_is_range_checked(fake_nidaqmx):
    daq = Nidaq("Dev1")
    with pytest.raises(RangeError, match="output voltage"):
        daq.write_voltage("ao0", 50.0)


def test_inverted_input_range_is_rejected(fake_nidaqmx):
    daq = Nidaq("Dev1")
    with pytest.raises(RangeError, match="must be below the maximum"):
        daq.read_voltage("ai0", minimum=10.0, maximum=-10.0)


def test_terminal_configuration_is_validated(fake_nidaqmx):
    daq = Nidaq("Dev1")
    with pytest.raises(RangeError, match="terminal configuration"):
        daq.read_voltage("ai0", terminal_configuration="telepathic")


def test_write_voltage_passes_the_value_through(fake_nidaqmx):
    daq = Nidaq("Dev1")
    daq.write_voltage("ao0", 1.5)
    assert fake_nidaqmx["written"] == [1.5]


# Quantum Design


def test_missing_pythonnet_says_what_to_install(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "clr":
            raise ImportError("no clr")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ConnectionFailure, match="pip install pythonnet"):
        Dynacool("127.0.0.1")


def test_instrument_types_match_the_dotnet_enum():
    assert INSTRUMENT_TYPES == {
        "ppms": 0,
        "versalab": 1,
        "dynacool": 2,
        "svsm": 3,
        "mpms3": 4,
    }


def test_unknown_instrument_type_is_rejected(monkeypatch):
    monkeypatch.setattr(QdInstrument, "_load_assembly", lambda self: None)
    with pytest.raises(RangeError, match="instrument type"):
        QdInstrument("toaster")


def test_no_dll_path_is_hardcoded_to_one_machine():
    """Importing the module must not touch the .NET assembly.

    Loading it at import time, or pointing at one machine's Python install,
    would make the subpackage unimportable everywhere else.
    """
    import ast
    import inspect

    from labdrivers.quantumdesign import qdinstrument

    tree = ast.parse(inspect.getsource(qdinstrument))

    def add_reference_calls(node):
        return [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "AddReference"
        ]

    # It is called somewhere, but only from inside a function and never at
    # module level, so importing is safe without pythonnet or the DLL.
    assert add_reference_calls(tree), "the assembly must still be loaded somewhere"
    for statement in tree.body:
        if not isinstance(statement, (ast.FunctionDef, ast.ClassDef)):
            assert not add_reference_calls(statement)

    # No absolute path to one particular machine's Python install.
    paths = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    joined = " ".join(paths)
    assert "ProgramData" not in joined
