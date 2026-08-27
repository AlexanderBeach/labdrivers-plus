"""Driver for the Stanford Research Systems SR830 DSP lock-in amplifier.

The SR830 can be reached over GPIB or RS-232. Which one is a constructor
argument::

    Sr830(gpib_address=8)
    Sr830(resource_name="ASRL3::INSTR", baud_rate=9600, interface="rs232")

Most SR830 settings are selected by index into a fixed ladder rather than by
value, so the ladders are given here and the properties accept and return real
physical values, snapping to the nearest available setting.

Commands and ranges are transcribed from the *SR830 DSP Lock-In Amplifier*
manual, Chapter 5 (Remote Programming).
"""

import math
import statistics
import time

from ..core import (
    Instrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
    nearest_allowed,
)
from ..core.errors import RangeError

# Sensitivity ladder for SENS, index 0 to 26, in volts (or microamps for the
# current inputs) rms full scale.
SENSITIVITIES = [
    2e-9,
    5e-9,
    10e-9,
    20e-9,
    50e-9,
    100e-9,
    200e-9,
    500e-9,
    1e-6,
    2e-6,
    5e-6,
    10e-6,
    20e-6,
    50e-6,
    100e-6,
    200e-6,
    500e-6,
    1e-3,
    2e-3,
    5e-3,
    10e-3,
    20e-3,
    50e-3,
    100e-3,
    200e-3,
    500e-3,
    1.0,
]

# Time constant ladder for OFLT, index 0 to 19, in seconds.
TIME_CONSTANTS = [
    10e-6,
    30e-6,
    100e-6,
    300e-6,
    1e-3,
    3e-3,
    10e-3,
    30e-3,
    100e-3,
    300e-3,
    1.0,
    3.0,
    10.0,
    30.0,
    100.0,
    300.0,
    1e3,
    3e3,
    10e3,
    30e3,
]

# Data sample rate ladder for SRAT, index 0 to 13, in hertz. Index 14 selects
# external trigger instead of a fixed rate.
SAMPLE_RATES = [
    0.0625,
    0.125,
    0.25,
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    16.0,
    32.0,
    64.0,
    128.0,
    256.0,
    512.0,
]
TRIGGERED_SAMPLE_RATE = 14

FILTER_SLOPES = [6, 12, 18, 24]
RESERVE_MODES = {"high reserve": 0, "normal": 1, "low noise": 2}
INPUT_CONFIGURATIONS = {"a": 0, "a-b": 1, "i1m": 2, "i100m": 3}
INPUT_GROUNDINGS = {"float": 0, "ground": 1}
INPUT_COUPLINGS = {"ac": 0, "dc": 1}
LINE_FILTERS = {"none": 0, "line": 1, "2x line": 2, "both": 3}
REFERENCE_SOURCES = {"external": 0, "internal": 1}
REFERENCE_SLOPES = {"sine": 0, "ttl rising": 1, "ttl falling": 2}
SCAN_MODES = {"one shot": 0, "loop": 1}
INTERFACES = {"rs232": 0, "gpib": 1}
EXPANSIONS = {1: 0, 10: 1, 100: 2}

# Channel display options for DDEF, per channel.
CHANNEL1_DISPLAYS = {"x": 0, "r": 1, "x noise": 2, "aux1": 3, "aux2": 4}
CHANNEL2_DISPLAYS = {"y": 0, "theta": 1, "y noise": 2, "aux3": 3, "aux4": 4}
DISPLAY_RATIOS = {"none": 0, "aux1": 1, "aux2": 2}

# Parameters SNAP? can read together, and what OUTP? indexes mean.
SNAP_PARAMETERS = {
    "x": 1,
    "y": 2,
    "r": 3,
    "theta": 4,
    "aux1": 5,
    "aux2": 6,
    "aux3": 7,
    "aux4": 8,
    "frequency": 9,
    "channel1": 10,
    "channel2": 11,
}
OUTPUT_PARAMETERS = {"x": 1, "y": 2, "r": 3, "theta": 4}
OFFSET_PARAMETERS = {"x": 1, "y": 2, "r": 3}

MINIMUM_FREQUENCY = 0.001
MAXIMUM_FREQUENCY = 102000.0
MAXIMUM_HARMONIC = 19999
BUFFER_POINTS = 16383


class Sr830(Instrument):
    """Interface to an SR830 lock-in amplifier.

        lockin = Sr830(gpib_address=8)
        lockin.time_constant = 0.1
        lockin.sensitivity = 1e-6
        x, y = lockin.snapshot("x", "y")

    :param interface: Which port the instrument should answer on, 'gpib' or
                      'rs232'. Sent as OUTX at construction. The SR830 will
                      otherwise reply on whichever port it was last told to
                      use, which is the usual cause of a lock-in that connects
                      but never answers. Pass None to leave it alone.
    """

    def __init__(self, *args, interface="gpib", **kwargs):
        super().__init__(*args, **kwargs)
        if interface is not None:
            self.interface = interface

    def identify(self):
        """Return the instrument's identification string (``*IDN?``)."""
        return self.query("*IDN?")

    def reset(self):
        """Return the instrument to its default settings (``*RST``)."""
        self.write("*RST")

    def clear_status(self):
        """Clear the status registers (``*CLS``)."""
        self.write("*CLS")

    # Interface

    @property
    def interface(self):
        """Returns which port the instrument replies on: 'gpib' or 'rs232'."""
        return "gpib" if self.query_integer("OUTX?") == 1 else "rs232"

    @interface.setter
    def interface(self, value):
        code = check_choice(value, INTERFACES, "output interface")
        self.write(f"OUTX {code}")

    @property
    def remote_override(self):
        """Returns whether the front panel stays live while under GPIB control."""
        return self.query_boolean("OVRM?")

    @remote_override.setter
    def remote_override(self, value):
        state = check_boolean(value, "remote override")
        self.write(f"OVRM {int(state)}")

    def go_to_local(self):
        """Return the instrument to front-panel control."""
        self.write("LOCL 0")

    def go_to_remote(self):
        """Put the instrument under remote control."""
        self.write("LOCL 1")

    def lock_front_panel(self):
        """Put the instrument in remote with the front panel locked out."""
        self.write("LOCL 2")

    # Reference and phase

    @property
    def phase(self):
        """Returns the reference phase shift, in degrees."""
        return self.query_float("PHAS?")

    @phase.setter
    def phase(self, value):
        check_range(value, -360.0, 729.99, "phase shift", " degrees")
        self.write(f"PHAS {value}")

    @property
    def reference_source(self):
        """Returns whether the reference is 'internal' or 'external'."""
        return "internal" if self.query_integer("FMOD?") == 1 else "external"

    @reference_source.setter
    def reference_source(self, value):
        code = check_choice(value, REFERENCE_SOURCES, "reference source")
        self.write(f"FMOD {code}")

    @property
    def frequency(self):
        """Returns the reference frequency, in hertz."""
        return self.query_float("FREQ?")

    @frequency.setter
    def frequency(self, value):
        check_range(
            value, MINIMUM_FREQUENCY, MAXIMUM_FREQUENCY, "reference frequency", " Hz"
        )
        self.write(f"FREQ {value}")

    @property
    def reference_slope(self):
        """Returns the external reference trigger.

        One of 'sine', 'ttl rising' or 'ttl falling'.
        """
        codes = {code: name for name, code in REFERENCE_SLOPES.items()}
        return codes.get(self.query_integer("RSLP?"), "sine")

    @reference_slope.setter
    def reference_slope(self, value):
        code = check_choice(value, REFERENCE_SLOPES, "external reference slope")
        self.write(f"RSLP {code}")

    @property
    def harmonic(self):
        """Returns which harmonic of the reference is detected."""
        return self.query_integer("HARM?")

    @harmonic.setter
    def harmonic(self, value):
        harmonic = check_integer_range(value, 1, MAXIMUM_HARMONIC, "detection harmonic")
        self.write(f"HARM {harmonic}")

    @property
    def amplitude(self):
        """Returns the sine output amplitude, in volts rms."""
        return self.query_float("SLVL?")

    @amplitude.setter
    def amplitude(self, value):
        check_range(value, 0.004, 5.0, "sine output amplitude", " V rms")
        self.write(f"SLVL {value}")

    # Input and filtering

    @property
    def input_configuration(self):
        """Returns the input wiring: 'a', 'a-b', 'i1m' or 'i100m'."""
        codes = {code: name for name, code in INPUT_CONFIGURATIONS.items()}
        return codes.get(self.query_integer("ISRC?"), "a")

    @input_configuration.setter
    def input_configuration(self, value):
        code = check_choice(value, INPUT_CONFIGURATIONS, "input configuration")
        self.write(f"ISRC {code}")

    @property
    def input_grounding(self):
        """Returns whether the input shield is 'float' or 'ground'."""
        return "ground" if self.query_integer("IGND?") == 1 else "float"

    @input_grounding.setter
    def input_grounding(self, value):
        code = check_choice(value, INPUT_GROUNDINGS, "input shield grounding")
        self.write(f"IGND {code}")

    @property
    def input_coupling(self):
        """Returns whether the input is 'ac' or 'dc' coupled."""
        return "dc" if self.query_integer("ICPL?") == 1 else "ac"

    @input_coupling.setter
    def input_coupling(self, value):
        code = check_choice(value, INPUT_COUPLINGS, "input coupling")
        self.write(f"ICPL {code}")

    @property
    def line_filter(self):
        """Returns the notch filters in use: 'none', 'line', '2x line' or 'both'."""
        codes = {code: name for name, code in LINE_FILTERS.items()}
        return codes.get(self.query_integer("ILIN?"), "none")

    @line_filter.setter
    def line_filter(self, value):
        code = check_choice(value, LINE_FILTERS, "line notch filter")
        self.write(f"ILIN {code}")

    @property
    def sensitivity(self):
        """Returns the full-scale sensitivity, in volts (or microamps) rms."""
        return SENSITIVITIES[self.query_integer("SENS?")]

    @sensitivity.setter
    def sensitivity(self, value):
        index, _ = nearest_allowed(value, SENSITIVITIES, "sensitivity", " V")
        self.write(f"SENS {index}")

    @property
    def time_constant(self):
        """Returns the output filter time constant, in seconds."""
        return TIME_CONSTANTS[self.query_integer("OFLT?")]

    @time_constant.setter
    def time_constant(self, value):
        index, _ = nearest_allowed(value, TIME_CONSTANTS, "time constant", " s")
        self.write(f"OFLT {index}")

    @property
    def filter_slope(self):
        """Returns the low-pass filter roll-off, in dB per octave: 6, 12, 18 or 24."""
        return FILTER_SLOPES[self.query_integer("OFSL?")]

    @filter_slope.setter
    def filter_slope(self, value):
        slope = check_choice(
            int(value),
            {s: i for i, s in enumerate(FILTER_SLOPES)},
            "low pass filter slope",
        )
        self.write(f"OFSL {slope}")

    @property
    def synchronous_filter(self):
        """Returns whether synchronous filtering is on (it applies below 200 Hz)."""
        return self.query_boolean("SYNC?")

    @synchronous_filter.setter
    def synchronous_filter(self, value):
        state = check_boolean(value, "synchronous filter")
        self.write(f"SYNC {int(state)}")

    @property
    def reserve(self):
        """Returns the dynamic reserve: 'high reserve', 'normal' or 'low noise'."""
        codes = {code: name for name, code in RESERVE_MODES.items()}
        return codes.get(self.query_integer("RMOD?"), "normal")

    @reserve.setter
    def reserve(self, value):
        code = check_choice(value, RESERVE_MODES, "dynamic reserve")
        self.write(f"RMOD {code}")

    # Display and output routing

    def set_display(self, channel, display, ratio="none"):
        """Choose what a front-panel channel shows.

        :param channel: 1 or 2.
        :param display: For channel 1, one of 'x', 'r', 'x noise', 'aux1',
                        'aux2'. For channel 2, 'y', 'theta', 'y noise',
                        'aux3', 'aux4'.
        :param ratio: Divide the display by 'none', 'aux1' or 'aux2'.
        """
        number = check_integer_range(channel, 1, 2, "display channel")
        options = CHANNEL1_DISPLAYS if number == 1 else CHANNEL2_DISPLAYS
        code = check_choice(display, options, f"channel {number} display")
        ratio_code = check_choice(ratio, DISPLAY_RATIOS, "display ratio")
        self.write(f"DDEF {number},{code},{ratio_code}")

    def get_display(self, channel):
        """Return what a channel is showing, as (display, ratio)."""
        number = check_integer_range(channel, 1, 2, "display channel")
        reply = self.query(f"DDEF? {number}")
        code, _, ratio_code = reply.partition(",")
        options = CHANNEL1_DISPLAYS if number == 1 else CHANNEL2_DISPLAYS
        names = {value: name for name, value in options.items()}
        ratios = {value: name for name, value in DISPLAY_RATIOS.items()}
        return (
            names.get(int(float(code)), code),
            ratios.get(int(float(ratio_code or 0)), ratio_code),
        )

    def set_output_source(self, channel, source):
        """Choose whether a rear output follows the display or X/Y directly.

        :param channel: 1 or 2.
        :param source: 'display' or 'xy'.
        """
        number = check_integer_range(channel, 1, 2, "output channel")
        code = check_choice(source, {"display": 0, "xy": 1}, "output source")
        self.write(f"FPOP {number},{code}")

    def set_offset_and_expand(self, parameter, offset=0.0, expand=1):
        """Offset and expand one of X, Y or R.

        :param parameter: 'x', 'y' or 'r'.
        :param offset: Offset as a percentage of full scale, -105 to 105.
        :param expand: Gain applied after the offset: 1, 10 or 100.
        """
        index = check_choice(parameter, OFFSET_PARAMETERS, "offset parameter")
        check_range(offset, -105.0, 105.0, "offset", " percent")
        code = check_choice(int(expand), EXPANSIONS, "expand")
        self.write(f"OEXP {index},{offset},{code}")

    def get_offset_and_expand(self, parameter):
        """Return the offset percentage and expansion of X, Y or R."""
        index = check_choice(parameter, OFFSET_PARAMETERS, "offset parameter")
        reply = self.query(f"OEXP? {index}")
        offset, _, code = reply.partition(",")
        expansions = {value: name for name, value in EXPANSIONS.items()}
        return float(offset), expansions.get(int(float(code or 0)), 1)

    def auto_offset(self, parameter):
        """Offset X, Y or R so it reads zero now."""
        index = check_choice(parameter, OFFSET_PARAMETERS, "offset parameter")
        self.write(f"AOFF {index}")

    def auto_gain(self):
        """Choose the sensitivity automatically, as the AUTO GAIN key does."""
        self.write("AGAN")

    def auto_reserve(self):
        """Choose the dynamic reserve automatically."""
        self.write("ARSV")

    def auto_phase(self):
        """Adjust the reference phase so that Y reads zero."""
        self.write("APHS")

    # Auxiliary inputs and outputs

    def auxiliary_input(self, channel):
        """Read one of the four auxiliary inputs, in volts."""
        number = check_integer_range(channel, 1, 4, "auxiliary input channel")
        return self.query_float(f"OAUX? {number}")

    def auxiliary_output(self, channel):
        """Read back the setting of one of the four auxiliary outputs."""
        number = check_integer_range(channel, 1, 4, "auxiliary output channel")
        return self.query_float(f"AUXV? {number}")

    def set_auxiliary_output(self, channel, voltage):
        """Set one of the four auxiliary outputs, in volts."""
        number = check_integer_range(channel, 1, 4, "auxiliary output channel")
        check_range(voltage, -10.5, 10.5, "auxiliary output voltage", " V")
        self.write(f"AUXV {number},{voltage}")

    # Reading values

    def output(self, parameter):
        """Read one of X, Y, R or theta.

        Reading these one at a time gives values from different instants. Use
        snapshot() when the values have to be consistent with each other.
        """
        index = check_choice(parameter, OUTPUT_PARAMETERS, "output parameter")
        return self.query_float(f"OUTP? {index}")

    def display_value(self, channel):
        """Read the value shown on channel 1 or 2."""
        number = check_integer_range(channel, 1, 2, "display channel")
        return self.query_float(f"OUTR? {number}")

    def snapshot(self, *parameters):
        """Read two to six values captured at the same instant.

        X and Y read separately come from different moments, which matters when
        the signal is moving. SNAP? samples them together.

        :param parameters: Two to six of 'x', 'y', 'r', 'theta', 'aux1'..
                           'aux4', 'frequency', 'channel1', 'channel2'.
        :return: A list of floats, in the order asked for.
        """
        if not 2 <= len(parameters) <= 6:
            raise RangeError(
                "A snapshot reads between 2 and 6 parameters at once, but "
                f"{len(parameters)} were given."
            )
        indexes = [
            check_choice(parameter, SNAP_PARAMETERS, "snapshot parameter")
            for parameter in parameters
        ]
        return self.query_floats("SNAP? " + ",".join(str(i) for i in indexes))

    @property
    def x(self):
        """Returns the in-phase component, in volts."""
        return self.output("x")

    @property
    def y(self):
        """Returns the quadrature component, in volts."""
        return self.output("y")

    @property
    def magnitude(self):
        """Returns the signal magnitude R, in volts."""
        return self.output("r")

    @property
    def theta(self):
        """Returns the signal phase, in degrees."""
        return self.output("theta")

    # Data buffer

    @property
    def sample_rate(self):
        """Returns the buffer sample rate in hertz.

        Returns 'trigger' when the buffer is clocked externally.
        """
        index = self.query_integer("SRAT?")
        return "trigger" if index == TRIGGERED_SAMPLE_RATE else SAMPLE_RATES[index]

    @sample_rate.setter
    def sample_rate(self, value):
        if str(value).strip().lower() == "trigger":
            self.write(f"SRAT {TRIGGERED_SAMPLE_RATE}")
            return
        index, _ = nearest_allowed(value, SAMPLE_RATES, "sample rate", " Hz")
        self.write(f"SRAT {index}")

    @property
    def scan_mode(self):
        """Returns whether the buffer stops when full ('one shot') or wraps ('loop')."""
        return "loop" if self.query_integer("SEND?") == 1 else "one shot"

    @scan_mode.setter
    def scan_mode(self, value):
        code = check_choice(value, SCAN_MODES, "scan mode")
        self.write(f"SEND {code}")

    @property
    def trigger_starts_scan(self):
        """Returns whether a trigger starts the scan as well as clocking it."""
        return self.query_boolean("TSTR?")

    @trigger_starts_scan.setter
    def trigger_starts_scan(self, value):
        state = check_boolean(value, "trigger starts scan")
        self.write(f"TSTR {int(state)}")

    def trigger(self):
        """Send a software trigger, equivalent to the rear trigger input."""
        self.write("TRIG")

    def start_scan(self):
        """Start or resume filling the buffer."""
        self.write("STRT")

    def pause_scan(self):
        """Pause the scan, keeping what has been stored."""
        self.write("PAUS")

    def reset_scan(self):
        """Stop the scan and discard everything stored."""
        self.write("REST")

    @property
    def buffer_count(self):
        """Returns how many points are stored in the buffer."""
        return self.query_integer("SPTS?")

    def read_buffer(self, channel, start=0, count=None):
        """Read stored points out of a display buffer.

        :param channel: Which display buffer, 1 or 2.
        :param start: First bin to read, counting from 0.
        :param count: How many points to read. Defaults to all of them from
                      ``start`` onwards.
        :return: A list of floats.
        """
        number = check_integer_range(channel, 1, 2, "display channel")
        first = check_integer_range(start, 0, BUFFER_POINTS, "starting bin")
        if count is None:
            count = max(self.buffer_count - first, 0)
        points = check_integer_range(count, 1, BUFFER_POINTS, "number of points")
        return self.query_floats(f"TRCA? {number},{first},{points}")

    # Front panel and setups

    @property
    def key_click(self):
        """Returns whether the front-panel keys click."""
        return self.query_boolean("KCLK?")

    @key_click.setter
    def key_click(self, value):
        state = check_boolean(value, "key click")
        self.write(f"KCLK {int(state)}")

    @property
    def alarms(self):
        """Returns whether the audible alarms sound."""
        return self.query_boolean("ALRM?")

    @alarms.setter
    def alarms(self, value):
        state = check_boolean(value, "alarms")
        self.write(f"ALRM {int(state)}")

    def save_setup(self, buffer):
        """Store the current settings in one of nine setup buffers."""
        number = check_integer_range(buffer, 1, 9, "setup buffer")
        self.write(f"SSET {number}")

    def recall_setup(self, buffer):
        """Restore settings from one of nine setup buffers."""
        number = check_integer_range(buffer, 1, 9, "setup buffer")
        self.write(f"RSET {number}")

    # Status

    @property
    def status_byte(self):
        """Returns the serial poll status byte (``*STB?``)."""
        return self.query_integer("*STB?")

    @property
    def lockin_status(self):
        """Returns the lock-in status byte.

        The byte reports overloads and whether the reference is locked.
        """
        return self.query_integer("LIAS?")

    @property
    def error_status(self):
        """Returns the error status byte."""
        return self.query_integer("ERRS?")

    def input_overload(self):
        """Whether the input or amplifier is overloaded.

        Bit 0 of the lock-in status byte. A reading taken while overloaded is
        not trustworthy.
        """
        return bool(self.lockin_status & 0b1)

    def filter_overload(self):
        """Whether the time-constant filter is overloaded (bit 1)."""
        return bool(self.lockin_status & 0b10)

    def output_overload(self):
        """Whether an output is railed (bit 2)."""
        return bool(self.lockin_status & 0b100)

    def reference_unlocked(self):
        """Whether the reference is unlocked (bit 3).

        An unlocked reference means the instrument is not measuring at the
        frequency you think it is.
        """
        return bool(self.lockin_status & 0b1000)

    def overloaded(self):
        """Whether any overload is present."""
        return bool(self.lockin_status & 0b111)

    # Common procedures

    def settling_time(self, time_constants=5):
        """How long to wait for the output filter to settle after a change.

        A single-pole filter is within 1% of a step after five time constants.
        Each extra 6 dB/octave of roll-off is another pole, and each pole adds
        its own settling, so the wait is scaled by the filter order.

        :param time_constants: How many time constants to allow per pole.
        :return: The wait, in seconds.
        """
        poles = self.filter_slope / 6
        return self.time_constant * float(time_constants) * poles

    def wait_to_settle(self, time_constants=5):
        """Sleep long enough for the output filter to settle.

        Reading immediately after changing the sensitivity, the time constant
        or anything about the sample gives the filter's old contents, not the
        new signal.

        :return: How long was waited, in seconds.
        """
        delay = self.settling_time(time_constants)
        time.sleep(delay)
        return delay

    def measure(self, settle=True, time_constants=5):
        """Read X and Y together, after letting the filter settle.

        :param settle: Wait for the output filter first.
        :return: A tuple of (X, Y) in volts, captured at the same instant.
        """
        if settle:
            self.wait_to_settle(time_constants)
        x, y = self.snapshot("x", "y")
        return x, y

    def measure_average(self, count=10, interval=None, settle=True):
        """Average several X and Y readings and report the scatter.

        :param count: How many readings to take.
        :param interval: Seconds between readings, defaulting to one time
                         constant so successive readings are not correlated.
        :param settle: Wait for the filter to settle before starting.
        :return: A tuple of (mean X, mean Y, standard error of X, standard
                 error of Y).
        """
        number = check_integer_range(count, 1, 100000, "number of readings")
        if settle:
            self.wait_to_settle()
        if interval is None:
            interval = self.time_constant

        xs, ys = [], []
        for index in range(number):
            if index:
                time.sleep(interval)
            x, y = self.snapshot("x", "y")
            xs.append(x)
            ys.append(y)

        if number < 2:
            return xs[0], ys[0], 0.0, 0.0
        root = math.sqrt(number)
        return (
            statistics.fmean(xs),
            statistics.fmean(ys),
            statistics.stdev(xs) / root,
            statistics.stdev(ys) / root,
        )

    def ramp_amplitude(self, target, steps=50, delay=0.05):
        """Walk the drive amplitude to a new value instead of stepping to it.

        A sudden change in drive puts a transient through the sample and takes
        the lock-in several time constants to recover from.

        :param target: Amplitude to finish at, in volts rms.
        :param steps: How many intermediate levels to pass through.
        :param delay: Seconds to wait at each step.
        """
        target = check_range(target, 0.004, 5.0, "sine output amplitude", " V rms")
        number = check_integer_range(steps, 1, 100000, "number of steps")
        check_range(delay, 0, 3600, "step delay", " s")

        start = self.amplitude
        for step in range(1, number + 1):
            self.amplitude = start + (target - start) * step / number
            time.sleep(delay)

    def __repr__(self):
        return f"Sr830({self._transport!r})"
