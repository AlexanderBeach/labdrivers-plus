"""Driver for the Keithley 2400 series SourceMeter.

Covers the whole series, meaning the 2400, 2400-LV, 2401, 2410, 2420, 2425,
2430 and 2440, which differ in how much they can source. Source and compliance
limits come from the model, detected from ``*IDN?`` at construction, so a 2410
is allowed its 1100 V while a 2440 is held to 42 V.

Commands and ranges are transcribed from the *2400 Series SourceMeter User's
Manual*, Section 18 (SCPI Command Reference).
"""

import time

from ..core import (
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import InstrumentError, RangeError
from ..core.sweep import round_trip, sweep_values

# Maximum source magnitudes per model, as (current in amps, voltage in volts),
# from the :SOURce:VOLTage:STARt/:STOP parameter tables, manual page 18-84.
MODEL_LIMITS = {
    "2400": (1.05, 210.0),
    "2400-LV": (1.05, 21.0),
    "2401": (1.05, 21.0),
    "2410": (1.05, 1100.0),
    "2420": (3.15, 63.0),
    "2425": (3.15, 105.0),
    "2430": (3.15, 105.0),
    "2430-PULSE": (10.5, 105.0),
    "2440": (5.25, 42.0),
}

DEFAULT_MODEL = "2400"

# Front-panel key codes for :SYSTem:KEY, from manual page 18-110. Code 25 is
# unassigned. These allow the instrument to be driven exactly as if someone
# were standing in front of it.
FRONT_PANEL_KEYS = {
    "range_up": 1,
    "source_down": 2,
    "left": 3,
    "menu": 4,
    "function": 5,
    "filter": 6,
    "speed": 7,
    "edit": 8,
    "auto": 9,
    "right": 10,
    "exit": 11,
    "source_voltage": 12,
    "limits": 13,
    "store": 14,
    "measure_voltage": 15,
    "toggle": 16,
    "range_down": 17,
    "enter": 18,
    "source_current": 19,
    "trigger": 20,
    "recall": 21,
    "measure_current": 22,
    "local": 23,
    "output": 24,
    "source_up": 26,
    "sweep": 27,
    "config": 28,
    "measure_resistance": 29,
    "relative": 30,
    "digits": 31,
    "front_rear": 32,
}

SOURCE_FUNCTIONS = {"voltage": "VOLT", "current": "CURR", "memory": "MEM"}
SOURCE_MODES = {"fixed": "FIX", "sweep": "SWE", "list": "LIST"}
MEASURE_FUNCTIONS = {"voltage": "VOLT:DC", "current": "CURR:DC", "resistance": "RES"}
OUTPUT_OFF_MODES = {
    "high impedance": "HIMP",
    "normal": "NORM",
    "zero": "ZERO",
    "guard": "GUAR",
}
FILTER_TYPES = {"moving": "MOV", "repeating": "REP"}
SWEEP_SPACINGS = {"linear": "LIN", "logarithmic": "LOG"}
SWEEP_DIRECTIONS = {"up": "UP", "down": "DOWN"}
SWEEP_RANGINGS = {"best": "BEST", "auto": "AUTO", "fixed": "FIX"}
BUFFER_SOURCES = {"sense": "SENS", "calculate1": "CALC1", "calculate2": "CALC2"}
BUFFER_CONTROLS = {"next": "NEXT", "never": "NEV"}
TIMESTAMP_FORMATS = {"absolute": "ABS", "delta": "DELT"}
TRIGGER_SOURCES = {"immediate": "IMM", "trigger link": "TLIN"}
ARM_SOURCES = {
    "immediate": "IMM",
    "timer": "TIM",
    "manual": "MAN",
    "bus": "BUS",
    "trigger link": "TLIN",
    "nstest": "NST",
    "pstest": "PST",
    "bstest": "BST",
}
# Whichever of these fields are switched on, the instrument sends them in this
# order and ignores the order they were named in, so the order here is the
# instrument's own and both reading paths depend on it.
DATA_ELEMENTS = {
    "voltage": "VOLT",
    "current": "CURR",
    "resistance": "RES",
    "time": "TIME",
    "status": "STAT",
}
STATISTICS = {
    "mean": "MEAN",
    "standard deviation": "SDEV",
    "maximum": "MAX",
    "minimum": "MIN",
    "peak to peak": "PKPK",
}

MAXIMUM_BUFFER_POINTS = 2500


class Keithley2400(ScpiInstrument):
    """Interface to a Keithley 2400 series SourceMeter.

        source = Keithley2400(gpib_address=24)
        source.source_function = "voltage"
        source.current_compliance = 1e-3
        source.source_value = 0.5
        source.output = True
        voltage, current = source.read("voltage", "current")

    :param model: Which model this is, e.g. '2410'. Detected from *IDN? when
                  not given. Worth passing explicitly for a 2400-LV or a 2401,
                  which both identify as a plain 2400 but source only 21 V.
    """

    IDENTIFIER = "MODEL 24"

    def __init__(self, *args, model=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._model = None
        self.model = model if model is not None else self._detect_model()

    def _detect_model(self):
        """Read the model number out of the instrument's identification."""
        try:
            identity = self.identify()
        except Exception:
            return DEFAULT_MODEL
        flattened = identity.replace("-", "").upper()
        for name in sorted(MODEL_LIMITS, key=len, reverse=True):
            if name.replace("-", "").upper() in flattened:
                return name
        return DEFAULT_MODEL

    @property
    def model(self):
        """Returns the model number, which sets the source and compliance limits."""
        return self._model

    @model.setter
    def model(self, value):
        name = str(value).upper().strip()
        if name not in MODEL_LIMITS:
            raise RangeError(
                f"'{value}' is not a Keithley 2400 series model. Known models "
                f"are {', '.join(sorted(MODEL_LIMITS))}."
            )
        self._model = name
        self._maximum_current, self._maximum_voltage = MODEL_LIMITS[name]

    @property
    def maximum_current(self):
        """Returns the largest current this model can source, in amps."""
        return self._maximum_current

    @property
    def maximum_voltage(self):
        """Returns the largest voltage this model can source, in volts."""
        return self._maximum_voltage

    def _active_source(self):
        """Returns the selected source as (name, keyword, limit, unit).

        Every setter that writes a source level asks this first, so the
        memory-mode check here guards all of them, and asking once means a
        level is written with one query rather than two.

        :raises RangeError: If the source is in memory mode, which has no
                            voltage or current level to set.
        """
        function = self.source_function
        if function == "memory":
            raise RangeError(
                "The source is in memory mode, which has no voltage or current "
                "level. Set source_function to 'voltage' or 'current' first."
            )
        try:
            code = SOURCE_FUNCTIONS[function]
        except KeyError:
            raise InstrumentError(
                f"The instrument says its source function is {function!r}, which "
                f"is not one of {', '.join(sorted(SOURCE_FUNCTIONS))}."
            )
        if function == "current":
            return function, code, self._maximum_current, " A"
        return function, code, self._maximum_voltage, " V"

    def _active_source_code(self):
        """SCPI keyword for whichever source is currently selected."""
        return self._active_source()[1]

    # Source configuration

    @property
    def source_function(self):
        """Returns which source is used: 'voltage', 'current' or 'memory'."""
        reply = self.query(":SOUR:FUNC:MODE?").strip().upper()
        for name, code in SOURCE_FUNCTIONS.items():
            if reply.startswith(code):
                return name
        return reply

    @source_function.setter
    def source_function(self, value):
        code = check_choice(value, SOURCE_FUNCTIONS, "source function")
        self.write(f":SOUR:FUNC:MODE {code}")

    @property
    def source_mode(self):
        """Returns how the active source steps: 'fixed', 'sweep' or 'list'."""
        reply = self.query(f":SOUR:{self._active_source_code()}:MODE?").strip().upper()
        for name, code in SOURCE_MODES.items():
            if reply.startswith(code):
                return name
        return reply

    @source_mode.setter
    def source_mode(self, value):
        code = check_choice(value, SOURCE_MODES, "source mode")
        self.write(f":SOUR:{self._active_source_code()}:MODE {code}")

    @property
    def source_value(self):
        """Returns the level of the active source, in amps or volts."""
        return self.query_float(f":SOUR:{self._active_source_code()}:LEV?")

    @source_value.setter
    def source_value(self, value):
        function, code, limit, unit = self._active_source()
        check_range(
            value,
            -limit,
            limit,
            f"{function} source level",
            unit,
        )
        self.write(f":SOUR:{code}:LEV {value}")

    @property
    def source_voltage(self):
        """Returns the voltage source level, in volts."""
        return self.query_float(":SOUR:VOLT:LEV?")

    @source_voltage.setter
    def source_voltage(self, value):
        limit = self._maximum_voltage
        check_range(value, -limit, limit, "source voltage", " V")
        self.write(f":SOUR:VOLT:LEV {value}")

    @property
    def source_current(self):
        """Returns the current source level, in amps."""
        return self.query_float(":SOUR:CURR:LEV?")

    @source_current.setter
    def source_current(self, value):
        limit = self._maximum_current
        check_range(value, -limit, limit, "source current", " A")
        self.write(f":SOUR:CURR:LEV {value}")

    @property
    def source_range(self):
        """Returns the range of the active source.

        Setting a range turns source autoranging off.
        """
        return self.query_float(f":SOUR:{self._active_source_code()}:RANG?")

    @source_range.setter
    def source_range(self, value):
        function, code, limit, unit = self._active_source()
        check_range(
            value,
            -limit,
            limit,
            f"{function} source range",
            unit,
        )
        self.write(f":SOUR:{code}:RANG {value}")

    @property
    def source_auto_range(self):
        """Returns whether the source picks its own range."""
        return self.query_boolean(f":SOUR:{self._active_source_code()}:RANG:AUTO?")

    @source_auto_range.setter
    def source_auto_range(self, value):
        state = check_boolean(value, "source autorange")
        self.write(f":SOUR:{self._active_source_code()}:RANG:AUTO {int(state)}")

    @property
    def source_delay(self):
        """Returns the settling time between setting the source and measuring.

        In seconds.
        """
        return self.query_float(":SOUR:DEL?")

    @source_delay.setter
    def source_delay(self, value):
        check_range(value, 0, 9999.999, "source delay", " s")
        self.write(f":SOUR:DEL {value}")

    @property
    def source_auto_delay(self):
        """Returns whether the instrument chooses its own source settling time."""
        return self.query_boolean(":SOUR:DEL:AUTO?")

    @source_auto_delay.setter
    def source_auto_delay(self, value):
        state = check_boolean(value, "source auto delay")
        self.write(f":SOUR:DEL:AUTO {int(state)}")

    @property
    def source_voltage_protection(self):
        """Returns the hard limit on the voltage source, in volts.

        The limit applies whatever the source level is set to.
        """
        return self.query_float(":SOUR:VOLT:PROT:LEV?")

    @source_voltage_protection.setter
    def source_voltage_protection(self, value):
        check_range(value, 0, self._maximum_voltage, "source voltage protection", " V")
        self.write(f":SOUR:VOLT:PROT:LEV {value}")

    @property
    def auto_output_off(self):
        """Returns whether the output switches off after each measurement."""
        return self.query_boolean(":SOUR:CLE:AUTO?")

    @auto_output_off.setter
    def auto_output_off(self, value):
        state = check_boolean(value, "auto output off")
        self.write(f":SOUR:CLE:AUTO {int(state)}")

    # Measurement configuration

    @property
    def measure_functions(self):
        """Returns which quantities are being measured, as a list of names."""
        reply = self.query(":SENS:FUNC:ON?").upper()
        return [name for name, code in MEASURE_FUNCTIONS.items() if code in reply]

    @measure_functions.setter
    def measure_functions(self, values):
        if isinstance(values, str):
            values = [values]
        codes = [
            check_choice(value, MEASURE_FUNCTIONS, "measure function")
            for value in values
        ]
        if not codes:
            raise RangeError(
                "At least one measure function is needed. Choose from "
                f"{', '.join(sorted(MEASURE_FUNCTIONS))}."
            )
        self.write(":SENS:FUNC:CONC ON")
        self.write(":SENS:FUNC:OFF:ALL")
        self.write(":SENS:FUNC:ON " + ",".join(f"'{code}'" for code in codes))

    @property
    def concurrent_measurement(self):
        """Returns whether more than one quantity can be measured at once."""
        return self.query_boolean(":SENS:FUNC:CONC?")

    @concurrent_measurement.setter
    def concurrent_measurement(self, value):
        state = check_boolean(value, "concurrent measurement")
        self.write(f":SENS:FUNC:CONC {int(state)}")

    @property
    def current_compliance(self):
        """Returns the current the source will not be allowed to exceed, in amps."""
        return self.query_float(":SENS:CURR:PROT:LEV?")

    @current_compliance.setter
    def current_compliance(self, value):
        limit = self._maximum_current
        check_range(value, -limit, limit, "current compliance", " A")
        self.write(f":SENS:CURR:PROT:LEV {value}")

    @property
    def voltage_compliance(self):
        """Returns the voltage the source will not be allowed to exceed, in volts."""
        return self.query_float(":SENS:VOLT:PROT:LEV?")

    @voltage_compliance.setter
    def voltage_compliance(self, value):
        limit = self._maximum_voltage
        check_range(value, -limit, limit, "voltage compliance", " V")
        self.write(f":SENS:VOLT:PROT:LEV {value}")

    @property
    def in_current_compliance(self):
        """Returns True while the source is clamped by the current compliance limit."""
        return self.query_boolean(":SENS:CURR:PROT:TRIP?")

    @property
    def in_voltage_compliance(self):
        """Returns True while the source is clamped by the voltage compliance limit."""
        return self.query_boolean(":SENS:VOLT:PROT:TRIP?")

    @property
    def in_compliance(self):
        """Returns True while the source is clamped by either compliance limit.

        A reading taken in compliance is not the measurement that was asked
        for, so this is worth checking before trusting one.
        """
        return self.in_current_compliance or self.in_voltage_compliance

    def measure_range(self, function):
        """Returns the measurement range of one function, in its own units."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        return self.query_float(f":SENS:{code}:RANG?")

    def set_measure_range(self, function, value):
        """Set one function's measurement range, turning its autorange off."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        self.write(f":SENS:{code}:RANG {value}")

    def measure_auto_range(self, function):
        """Returns whether one measure function picks its own range."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        return self.query_boolean(f":SENS:{code}:RANG:AUTO?")

    def set_measure_auto_range(self, function, value):
        """Turn autoranging on or off for one measure function."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        state = check_boolean(value, "measure autorange")
        self.write(f":SENS:{code}:RANG:AUTO {int(state)}")

    def integration_time(self, function):
        """Integration time of one function, in power line cycles."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        return self.query_float(f":SENS:{code}:NPLC?")

    def set_integration_time(self, function, cycles):
        """Set one function's integration time, in power line cycles.

        Longer is quieter: 0.01 is the fastest and noisiest setting, 10 the
        slowest and quietest. One cycle is the default.
        """
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        check_range(cycles, 0.01, 10, "integration time", " power line cycles")
        self.write(f":SENS:{code}:NPLC {cycles}")

    @property
    def four_wire_sense(self):
        """Returns whether remote (4-wire) sensing is used instead of 2-wire."""
        return self.query_boolean(":SYST:RSEN?")

    @four_wire_sense.setter
    def four_wire_sense(self, value):
        state = check_boolean(value, "four wire sense")
        self.write(f":SYST:RSEN {int(state)}")

    @property
    def resistance_mode(self):
        """Returns whether resistance ranging is 'auto' or 'manual'."""
        reply = self.query(":SENS:RES:MODE?").strip().upper()
        return "auto" if reply.startswith("AUTO") else "manual"

    @resistance_mode.setter
    def resistance_mode(self, value):
        code = check_choice(value, {"auto": "AUTO", "manual": "MAN"}, "resistance mode")
        self.write(f":SENS:RES:MODE {code}")

    @property
    def offset_compensated_resistance(self):
        """Returns whether offset-compensated ohms is on, which cancels thermal EMFs."""
        return self.query_boolean(":SENS:RES:OCOM?")

    @offset_compensated_resistance.setter
    def offset_compensated_resistance(self, value):
        state = check_boolean(value, "offset compensated resistance")
        self.write(f":SENS:RES:OCOM {int(state)}")

    @property
    def auto_zero(self):
        """Returns whether the instrument re-zeros its A/D before every reading.

        Accepts 'once' as well as on and off, to force a single update.
        """
        return self.query_boolean(":SYST:AZER:STAT?")

    @auto_zero.setter
    def auto_zero(self, value):
        if str(value).strip().lower() == "once":
            self.write(":SYST:AZER:STAT ONCE")
            return
        state = check_boolean(value, "auto zero")
        self.write(f":SYST:AZER:STAT {int(state)}")

    # Averaging filter

    @property
    def filter_enabled(self):
        """Returns whether the averaging filter is on."""
        return self.query_boolean(":SENS:AVER:STAT?")

    @filter_enabled.setter
    def filter_enabled(self, value):
        state = check_boolean(value, "filter")
        self.write(f":SENS:AVER:STAT {int(state)}")

    @property
    def filter_count(self):
        """Returns how many readings the averaging filter combines."""
        return self.query_integer(":SENS:AVER:COUN?")

    @filter_count.setter
    def filter_count(self, value):
        count = check_integer_range(value, 1, 100, "filter count")
        self.write(f":SENS:AVER:COUN {count}")

    @property
    def filter_type(self):
        """Returns the averaging filter type: 'moving' or 'repeating'."""
        reply = self.query(":SENS:AVER:TCON?").strip().upper()
        return "moving" if reply.startswith("MOV") else "repeating"

    @filter_type.setter
    def filter_type(self, value):
        code = check_choice(value, FILTER_TYPES, "filter type")
        self.write(f":SENS:AVER:TCON {code}")

    def enable_filter(self, count=10, filter_type="repeating"):
        """Turn on averaging with a given count and type, in one call."""
        self.filter_type = filter_type
        self.filter_count = count
        self.filter_enabled = True

    def disable_filter(self):
        """Turn the averaging filter off."""
        self.filter_enabled = False

    # Output

    @property
    def output(self):
        """Returns whether the output terminals are live."""
        return self.query_boolean(":OUTP:STAT?")

    @output.setter
    def output(self, value):
        state = check_boolean(value, "output")
        self.write(f":OUTP:STAT {int(state)}")

    @property
    def output_off_mode(self):
        """Returns what the output does when off.

        'high impedance' opens the output relay, 'normal' holds 0 V with
        compliance in effect, 'zero' sources 0 V, and 'guard' is a current
        source configuration. The 2410 defaults to 'guard'.
        """
        reply = self.query(":OUTP:SMOD?").strip().upper()
        for name, code in OUTPUT_OFF_MODES.items():
            if reply.startswith(code):
                return name
        return reply

    @output_off_mode.setter
    def output_off_mode(self, value):
        code = check_choice(value, OUTPUT_OFF_MODES, "output off mode")
        self.write(f":OUTP:SMOD {code}")

    def ramp_to(self, target, steps=100, delay=0.05):
        """Walk the source to a level in steps rather than jumping to it.

        Stepping avoids the transient a sudden change puts through a device,
        which matters for anything fragile on the end of the probes.

        :param target: Level to finish at, in amps or volts.
        :param steps: How many intermediate levels to pass through.
        :param delay: Seconds to wait at each step.
        """
        steps = check_integer_range(steps, 1, 100000, "number of steps")
        check_range(delay, 0, 3600, "step delay", " s")
        function, code, limit, unit = self._active_source()
        target = check_range(
            target,
            -limit,
            limit,
            f"{function} source level",
            unit,
        )

        start = self.source_value
        for step in range(1, steps + 1):
            self.source_value = start + (target - start) * step / steps
            time.sleep(delay)

    # Taking readings

    @property
    def data_elements(self):
        """Returns which fields a reading returns, as a list of names."""
        fields = [
            field.strip() for field in self.query(":FORM:ELEM?").upper().split(",")
        ]
        return [name for name, code in DATA_ELEMENTS.items() if code in fields]

    @data_elements.setter
    def data_elements(self, values):
        if isinstance(values, str):
            values = [values]
        codes = [check_choice(value, DATA_ELEMENTS, "data element") for value in values]
        if not codes:
            raise RangeError(
                "At least one data element is needed. Choose from "
                f"{', '.join(sorted(DATA_ELEMENTS))}."
            )
        self.write(":FORM:ELEM " + ",".join(codes))

    def read(self, *elements):
        """Trigger a measurement and return it.

        :param elements: Which fields to return, e.g. 'voltage', 'current'.
                         Sets the data elements first. With none given, returns
                         whatever the instrument is already configured to send.
        :return: A list of floats, one per element, in the order asked for.
        """
        if not elements:
            return self.query_floats(":READ?")
        asked = [
            check_choice(value, DATA_ELEMENTS, "data element") for value in elements
        ]
        self.data_elements = list(elements)
        reply = self.query_floats(":READ?")
        # Asking for current and then voltage still brings the voltage back
        # first, because the order lives in the instrument rather than in the
        # request. Putting the reply back into the order the caller used means
        # read('current', 'voltage') hands back current and then voltage.
        sent = [code for code in DATA_ELEMENTS.values() if code in asked]
        if len(reply) != len(sent):
            return reply
        measured = dict(zip(sent, reply))
        return [measured[code] for code in asked]

    def fetch(self):
        """Returns the last reading again, without triggering a new one."""
        return self.query_floats(":FETC?")

    def measure(self, function):
        """Configure for one function, trigger, and return the reading."""
        code = check_choice(function, MEASURE_FUNCTIONS, "measure function")
        return self.query_floats(f":MEAS:{code}?")

    def initiate(self):
        """Start the configured source-measure cycle."""
        self.write(":INIT")

    def abort(self):
        """Stop the source-measure cycle and return to idle."""
        self.write(":ABOR")

    # Buffer

    @property
    def buffer_size(self):
        """Returns how many readings the buffer will hold."""
        return self.query_integer(":TRAC:POIN?")

    @buffer_size.setter
    def buffer_size(self, value):
        points = check_integer_range(
            value, 1, MAXIMUM_BUFFER_POINTS, "buffer size", " readings"
        )
        self.write(f":TRAC:POIN {points}")

    @property
    def buffer_count(self):
        """Returns how many readings are in the buffer now."""
        return self.query_integer(":TRAC:POIN:ACT?")

    @property
    def buffer_source(self):
        """Returns where buffered readings come from."""
        reply = self.query(":TRAC:FEED?").strip().upper()
        for name, code in BUFFER_SOURCES.items():
            if reply.startswith(code):
                return name
        return reply

    @buffer_source.setter
    def buffer_source(self, value):
        code = check_choice(value, BUFFER_SOURCES, "buffer source")
        self.write(f":TRAC:FEED {code}")

    @property
    def buffer_control(self):
        """Returns whether the buffer is filling ('next') or idle ('never')."""
        reply = self.query(":TRAC:FEED:CONT?").strip().upper()
        return "next" if reply.startswith("NEXT") else "never"

    @buffer_control.setter
    def buffer_control(self, value):
        code = check_choice(value, BUFFER_CONTROLS, "buffer control")
        self.write(f":TRAC:FEED:CONT {code}")

    @property
    def buffer_free(self):
        """Returns the bytes available and bytes in use in the buffer, as a pair."""
        return self.query_floats(":TRAC:FREE?")

    @property
    def timestamp_format(self):
        """Returns whether buffer timestamps are 'absolute' or 'delta'."""
        reply = self.query(":TRAC:TST:FORM?").strip().upper()
        return "absolute" if reply.startswith("ABS") else "delta"

    @timestamp_format.setter
    def timestamp_format(self, value):
        code = check_choice(value, TIMESTAMP_FORMATS, "timestamp format")
        self.write(f":TRAC:TST:FORM {code}")

    def read_buffer(self):
        """Returns everything stored in the buffer, as a list of floats."""
        return self.query_floats(":TRAC:DATA?")

    def clear_buffer(self):
        """Discard the buffer contents."""
        self.write(":TRAC:CLE")

    def start_buffer(self, size=None):
        """Clear the buffer, size it, and arm it to fill.

        :param size: Number of readings to store. Left alone if not given.
        """
        self.clear_buffer()
        if size is not None:
            self.buffer_size = size
        self.buffer_control = "next"

    # Triggering

    @property
    def trigger_count(self):
        """Returns how many measurements one arm cycle takes."""
        return self.query_integer(":TRIG:COUN?")

    @trigger_count.setter
    def trigger_count(self, value):
        count = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "trigger count")
        self.write(f":TRIG:COUN {count}")

    @property
    def trigger_delay(self):
        """Returns the delay between the trigger and the measurement, in seconds."""
        return self.query_float(":TRIG:DEL?")

    @trigger_delay.setter
    def trigger_delay(self, value):
        check_range(value, 0, 999.9999, "trigger delay", " s")
        self.write(f":TRIG:DEL {value}")

    @property
    def trigger_source(self):
        """Returns what advances the trigger layer: 'immediate' or 'trigger link'."""
        reply = self.query(":TRIG:SOUR?").strip().upper()
        return "trigger link" if reply.startswith("TLIN") else "immediate"

    @trigger_source.setter
    def trigger_source(self, value):
        code = check_choice(value, TRIGGER_SOURCES, "trigger source")
        self.write(f":TRIG:SOUR {code}")

    @property
    def arm_count(self):
        """Returns how many times the arm layer repeats."""
        return self.query_integer(":ARM:COUN?")

    @arm_count.setter
    def arm_count(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write(":ARM:COUN INF")
            return
        count = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "arm count")
        self.write(f":ARM:COUN {count}")

    @property
    def arm_source(self):
        """Returns what advances the arm layer."""
        reply = self.query(":ARM:SOUR?").strip().upper()
        for name, code in ARM_SOURCES.items():
            if reply.startswith(code):
                return name
        return reply

    @arm_source.setter
    def arm_source(self, value):
        code = check_choice(value, ARM_SOURCES, "arm source")
        self.write(f":ARM:SOUR {code}")

    @property
    def arm_timer(self):
        """Returns the interval of the arm layer's timer source, in seconds."""
        return self.query_float(":ARM:TIM?")

    @arm_timer.setter
    def arm_timer(self, value):
        check_range(value, 0.001, 99999.99, "arm timer interval", " s")
        self.write(f":ARM:TIM {value}")

    def clear_trigger(self):
        """Put the trigger system back to idle."""
        self.write(":TRIG:CLE")

    def send_bus_trigger(self):
        """Send a bus trigger (``*TRG``)."""
        self.write("*TRG")

    # Sweeps

    @property
    def sweep_start(self):
        """Returns the first level of a sweep, in the active source's units."""
        return self.query_float(f":SOUR:{self._active_source_code()}:STAR?")

    @sweep_start.setter
    def sweep_start(self, value):
        function, code, limit, unit = self._active_source()
        check_range(value, -limit, limit, "sweep start level", unit)
        self.write(f":SOUR:{code}:STAR {value}")

    @property
    def sweep_stop(self):
        """Returns the last level of a sweep, in the active source's units."""
        return self.query_float(f":SOUR:{self._active_source_code()}:STOP?")

    @sweep_stop.setter
    def sweep_stop(self, value):
        function, code, limit, unit = self._active_source()
        check_range(value, -limit, limit, "sweep stop level", unit)
        self.write(f":SOUR:{code}:STOP {value}")

    @property
    def sweep_step(self):
        """Returns the step size of a linear sweep, in the active source's units."""
        return self.query_float(f":SOUR:{self._active_source_code()}:STEP?")

    @sweep_step.setter
    def sweep_step(self, value):
        function, code, limit, unit = self._active_source()
        check_range(value, -limit, limit, "sweep step size", unit)
        self.write(f":SOUR:{code}:STEP {value}")

    @property
    def sweep_points(self):
        """Returns how many points a sweep contains."""
        return self.query_integer(":SOUR:SWE:POIN?")

    @sweep_points.setter
    def sweep_points(self, value):
        points = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "sweep points")
        self.write(f":SOUR:SWE:POIN {points}")

    @property
    def sweep_spacing(self):
        """Returns whether sweep points are spaced 'linear' or 'logarithmic'."""
        reply = self.query(":SOUR:SWE:SPAC?").strip().upper()
        return "logarithmic" if reply.startswith("LOG") else "linear"

    @sweep_spacing.setter
    def sweep_spacing(self, value):
        code = check_choice(value, SWEEP_SPACINGS, "sweep spacing")
        self.write(f":SOUR:SWE:SPAC {code}")

    @property
    def sweep_direction(self):
        """Returns whether the sweep runs 'up' from start or 'down' from stop."""
        reply = self.query(":SOUR:SWE:DIR?").strip().upper()
        return "down" if reply.startswith("DOWN") else "up"

    @sweep_direction.setter
    def sweep_direction(self, value):
        code = check_choice(value, SWEEP_DIRECTIONS, "sweep direction")
        self.write(f":SOUR:SWE:DIR {code}")

    @property
    def sweep_ranging(self):
        """Returns how the source ranges during a sweep: 'best', 'auto' or 'fixed'."""
        reply = self.query(":SOUR:SWE:RANG?").strip().upper()
        for name, code in SWEEP_RANGINGS.items():
            if reply.startswith(code):
                return name
        return reply

    @sweep_ranging.setter
    def sweep_ranging(self, value):
        code = check_choice(value, SWEEP_RANGINGS, "sweep ranging")
        self.write(f":SOUR:SWE:RANG {code}")

    def configure_sweep(
        self,
        start,
        stop,
        points=None,
        step=None,
        spacing="linear",
        direction="up",
        ranging="best",
    ):
        """Set up a staircase sweep of the active source.

        Give either ``points`` or ``step``, not both. The trigger count is set
        to match the number of points, so one initiate() runs the whole sweep.

        :param start: First source level.
        :param stop: Last source level.
        :param points: Number of points in the sweep.
        :param step: Size of each step, as an alternative to points.
        :return: The number of points the sweep will produce.
        """
        if (points is None) == (step is None):
            raise RangeError("A sweep needs either points= or step=, but not both.")

        self.source_mode = "sweep"
        self.sweep_start = start
        self.sweep_stop = stop
        self.sweep_spacing = spacing
        self.sweep_direction = direction
        self.sweep_ranging = ranging

        if step is not None:
            if float(step) == 0:
                raise RangeError("The sweep step size cannot be zero.")
            self.sweep_step = step
            # Counted the way the instrument counts. It walks whole steps out
            # from the start, so it produces floor(span / step) + 1 levels and
            # never reaches the stop value unless the step divides the span
            # exactly. The small margin is there because 0.3 / 0.1 comes out
            # as 2.9999999999999996, which would otherwise drop a level the
            # sweep really does produce and park the source partway.
            span = abs(float(stop) - float(start))
            points = int(span / abs(float(step)) + 1e-9) + 1
        else:
            self.sweep_points = points

        self.trigger_count = points
        return points

    def configure_list_sweep(self, levels):
        """Sweep through an explicit list of source levels.

        :param levels: The levels to output, in order. Up to 100 of them.
        :return: How many levels were configured.
        """
        levels = list(levels)
        if not 1 <= len(levels) <= 100:
            raise RangeError(
                f"A list sweep takes between 1 and 100 levels, but got {len(levels)}."
            )
        function, code, limit, unit = self._active_source()
        for level in levels:
            check_range(level, -limit, limit, "list sweep level", unit)
        self.source_mode = "list"
        self.write(f":SOUR:LIST:{code} " + ",".join(str(level) for level in levels))
        self.trigger_count = len(levels)
        return len(levels)

    # Math, limit tests and statistics

    @property
    def math_expression(self):
        """Returns the name of the CALC1 math expression in use."""
        return self.query(":CALC:MATH:NAME?").strip().strip('"')

    @math_expression.setter
    def math_expression(self, value):
        self.write(f':CALC:MATH:NAME "{value}"')

    @property
    def math_enabled(self):
        """Returns whether the CALC1 math expression is applied to readings."""
        return self.query_boolean(":CALC:STAT?")

    @math_enabled.setter
    def math_enabled(self, value):
        state = check_boolean(value, "math")
        self.write(f":CALC:STAT {int(state)}")

    def math_data(self):
        """Returns the result of the CALC1 math expression."""
        return self.query_floats(":CALC:DATA?")

    def available_math_expressions(self):
        """List the math expression names the instrument knows."""
        reply = self.query(":CALC:MATH:CAT?")
        return [name.strip().strip('"') for name in reply.split(",") if name.strip()]

    def set_limit_test(self, number, lower, upper, enabled=True):
        """Configure one of the CALC2 limit tests.

        :param number: Which limit test, 1 to 12.
        :param lower: Lower bound a reading must stay above to pass.
        :param upper: Upper bound a reading must stay below to pass.
        """
        limit = check_integer_range(number, 1, 12, "limit test number")
        check_range(lower, -9.999999e20, 9.999999e20, "limit lower bound")
        check_range(upper, -9.999999e20, 9.999999e20, "limit upper bound")
        if float(lower) > float(upper):
            raise RangeError(
                f"The limit lower bound ({lower}) must not be above the upper "
                f"bound ({upper})."
            )
        state = check_boolean(enabled, "limit test")
        self.write(f":CALC2:LIM{limit}:LOW:DATA {lower}")
        self.write(f":CALC2:LIM{limit}:UPP:DATA {upper}")
        self.write(f":CALC2:LIM{limit}:STAT {int(state)}")

    def limit_test_failed(self, number):
        """Whether a limit test's most recent reading failed."""
        limit = check_integer_range(number, 1, 12, "limit test number")
        return self.query_boolean(f":CALC2:LIM{limit}:FAIL?")

    def statistic(self, name):
        """Return a statistic over the readings in the buffer.

        :param name: 'mean', 'standard deviation', 'maximum', 'minimum' or
                     'peak to peak'.
        """
        code = check_choice(name, STATISTICS, "statistic")
        self.write(f":CALC3:FORM {code}")
        return self.query_floats(":CALC3:DATA?")

    # Front panel
    #
    # These allow the instrument to be driven exactly as if someone were
    # standing in front of it, which is useful for demonstrating a procedure or
    # for leaving a message on the display during a long unattended run.

    @property
    def display_enabled(self):
        """Returns whether the front-panel display is on.

        Turning it off makes the instrument measurably faster, because it stops
        refreshing the display between readings.
        """
        return self.query_boolean(":DISP:ENAB?")

    @display_enabled.setter
    def display_enabled(self, value):
        state = check_boolean(value, "display")
        self.write(f":DISP:ENAB {int(state)}")

    @property
    def display_digits(self):
        """Returns how many digits the display shows, from 4 to 7."""
        return self.query_integer(":DISP:DIG?")

    @display_digits.setter
    def display_digits(self, value):
        digits = check_integer_range(value, 4, 7, "number of display digits")
        self.write(f":DISP:DIG {digits}")

    @property
    def display_text(self):
        """Returns the message shown on the top line of the display."""
        return self.query(":DISP:WIND1:TEXT:DATA?").strip().strip('"')

    @display_text.setter
    def display_text(self, value):
        text = str(value)
        if len(text) > 20:
            raise RangeError(
                f"Display text is at most 20 characters, but got {len(text)}."
            )
        if '"' in text:
            # The message goes to the instrument inside a quoted string, and
            # SCPI gives no way to escape a quote within one, so a message
            # carrying one would end early and leave the rest as commands.
            raise RangeError(
                f"Display text cannot contain a double quote, but got {text!r}."
            )
        self.write(f':DISP:WIND1:TEXT:DATA "{text}"')
        self.write(":DISP:WIND1:TEXT:STAT 1")

    def clear_display_text(self):
        """Stop showing a message and return the display to readings."""
        self.write(":DISP:WIND1:TEXT:STAT 0")

    def press_key(self, key):
        """Press a front-panel key, by name or by code.

        :param key: A name from FRONT_PANEL_KEYS, such as 'output' or
                    'measure_voltage', or a code from 1 to 32.
        """
        if isinstance(key, str):
            name = key.strip().lower().replace(" ", "_")
            if name not in FRONT_PANEL_KEYS:
                raise RangeError(
                    f"'{key}' is not a front-panel key. Choose from "
                    f"{', '.join(sorted(FRONT_PANEL_KEYS))}."
                )
            code = FRONT_PANEL_KEYS[name]
        else:
            code = check_integer_range(key, 1, 32, "front-panel key code")
        self.write(f":SYST:KEY {code}")

    @property
    def last_key(self):
        """Returns the code of the last key pressed, whether by hand or by press_key."""
        return self.query_integer(":SYST:KEY?")

    @property
    def beeper_enabled(self):
        """Returns whether the beeper will sound."""
        return self.query_boolean(":SYST:BEEP:STAT?")

    @beeper_enabled.setter
    def beeper_enabled(self, value):
        state = check_boolean(value, "beeper")
        self.write(f":SYST:BEEP:STAT {int(state)}")

    def beep(self, frequency=500, duration=1.0):
        """Sound the beeper, to signal the end of a long measurement.

        :param frequency: Tone in hertz, from 65 to 2000000.
        :param duration: Length in seconds, from 0 to 7.9.
        """
        check_range(frequency, 65, 2000000, "beeper frequency", " Hz")
        check_range(duration, 0, 7.9, "beeper duration", " s")
        self.write(f":SYST:BEEP {frequency},{duration}")

    def go_to_local(self):
        """Hand the instrument back to front-panel control."""
        self.write(":SYST:LOC")

    def go_to_remote(self):
        """Put the instrument under remote control."""
        self.write(":SYST:REM")

    def lock_front_panel(self):
        """Put the instrument in remote with the LOCAL key disabled.

        Stops anyone taking manual control part-way through a measurement.
        """
        self.write(":SYST:RWL")

    # System

    @property
    def line_frequency(self):
        """Returns the power line frequency the instrument synchronizes to."""
        return self.query_integer(":SYST:LFR?")

    @line_frequency.setter
    def line_frequency(self, value):
        code = check_choice(value, {50: "50", 60: "60"}, "line frequency")
        self.write(f":SYST:LFR {code}")

    def preset(self):
        """Put the instrument back to its SYSTem:PRESet defaults."""
        self.write(":SYST:PRES")

    @property
    def contact_check(self):
        """Returns whether contact check is enabled, if this unit has the option."""
        return self.query_boolean(":SYST:CCH?")

    @contact_check.setter
    def contact_check(self, value):
        state = check_boolean(value, "contact check")
        self.write(f":SYST:CCH {int(state)}")

    @property
    def contact_check_resistance(self):
        """Returns the contact check threshold resistance, in ohms."""
        return self.query_float(":SYST:CCH:RES?")

    @contact_check_resistance.setter
    def contact_check_resistance(self, value):
        check_range(value, 0, 60, "contact check threshold resistance", " ohms")
        self.write(f":SYST:CCH:RES {value}")

    # Common procedures

    def configure_source(self, function, level=0.0, compliance=None, measure=None):
        """Set the instrument up as a source, in one call.

        :param function: 'voltage' or 'current' to source.
        :param level: Level to start at.
        :param compliance: Limit on the quantity not being sourced. A voltage
                           source is limited in current, and the other way
                           round.
        :param measure: What to measure, defaulting to both voltage and
                        current.
        """
        # Only the two that have a level to set. Memory mode is a source
        # function the instrument has, and naming it here would otherwise set up
        # a voltage source without saying so.
        code = check_choice(
            function, {"voltage": "VOLT", "current": "CURR"}, "source function"
        )
        name = "current" if code == "CURR" else "voltage"

        self.source_function = name
        if compliance is not None:
            if name == "current":
                self.voltage_compliance = compliance
            else:
                self.current_compliance = compliance
        self.measure_functions = ["voltage", "current"] if measure is None else measure
        self.source_value = level

    def sweep_source(
        self,
        start,
        stop,
        points=None,
        step=None,
        settle=0.0,
        spacing="linear",
        return_to_start=False,
    ):
        """Step the source through a range, reading at each point.

        Runs the sweep from Python rather than using the instrument's own
        staircase. That is slower, but the readings arrive one at a time, so a
        measurement can be plotted, logged or stopped while it runs.

            for level, voltage, current in source.sweep_source(0, 1, points=101):
                print(level, voltage, current)

        :param start: First source level.
        :param stop: Last source level.
        :param points: Number of levels, including both ends.
        :param step: Spacing between levels, as an alternative to points.
        :param settle: Seconds to wait after setting each level before reading.
        :param spacing: 'linear' or 'logarithmic'.
        :param return_to_start: Sweep back down again afterwards, for
                                hysteresis.
        :yield: A tuple of (source level, voltage, current) at each point.
        """
        levels = sweep_values(start, stop, points=points, step=step, spacing=spacing)
        if return_to_start:
            levels = round_trip(levels)

        self.data_elements = ["voltage", "current"]
        for level in levels:
            self.source_value = level
            if settle:
                time.sleep(settle)
            voltage, current = self.read()[:2]
            yield level, voltage, current

    def safe_shutdown(self, steps=50, delay=0.02):
        """Walk the source down to zero, then switch the output off.

        Opening the output while it is driving leaves whatever charge is on the
        device to find its own way out. Ramping down first does not.
        """
        if self.output:
            self.ramp_to(0.0, steps=steps, delay=delay)
        self.output = False

    def __repr__(self):
        return f"Keithley{self._model}({self._transport!r})"
