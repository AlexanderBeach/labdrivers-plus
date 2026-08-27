"""Driver for Copper Mountain and Keysight ENA vector network analyzers.

Copper Mountain built their SCPI set to match the Keysight ENA, so one driver
covers both: the S2VNA and S4VNA software for Copper Mountain hardware, and the
E5061B and E5071C ENA analyzers. Anything that differs between them is a
frequency limit rather than a command, and that is a constructor argument.

A VNA is organized as channels holding traces. A channel owns the stimulus,
meaning the frequency range, the power, the IF bandwidth and the sweep type,
and each trace within that channel displays one S-parameter in one format.
Trace numbering restarts within each channel, so both numbers are needed to
address a measurement.

Two kinds of data come back. Formatted data is what the display shows, one
number per point, in whatever format the trace is set to. Complex data is the
underlying S-parameter, real and imaginary parts interleaved, and is what any
further analysis should use.

Commands and ranges are transcribed from the *CMT VNA Programming Manual*.
"""

from ..core import (
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
    nearest_allowed,
)
from ..core.errors import RangeError

MAXIMUM_CHANNELS = 16
MAXIMUM_TRACES = 16

# S-parameters and receivers a trace can be assigned to.
PARAMETERS = {
    "s11": "S11",
    "s12": "S12",
    "s21": "S21",
    "s22": "S22",
    "a": "A",
    "b": "B",
    "r1": "R1",
    "r2": "R2",
}

# Trace display formats.
FORMATS = {
    "log magnitude": "MLOGarithmic",
    "linear magnitude": "MLINear",
    "phase": "PHASe",
    "expanded phase": "UPHase",
    "group delay": "GDELay",
    "real": "REAL",
    "imaginary": "IMAGinary",
    "swr": "SWR",
    "smith": "SMITh",
    "smith linear": "SLINear",
    "smith log": "SLOGarithmic",
    "smith complex": "SCOMplex",
    "smith admittance": "SADMittance",
    "polar": "POLar",
    "polar linear": "PLINear",
    "polar log": "PLOGarithmic",
}

SWEEP_TYPES = {
    "linear": "LINear",
    "logarithmic": "LOGarithmic",
    "segment": "SEGMent",
    "power": "POWer",
}

TRIGGER_SOURCES = {
    "internal": "INTernal",
    "external": "EXTernal",
    "manual": "MANual",
    "bus": "BUS",
}

# IF bandwidth steps the analyzer accepts, in hertz.
IF_BANDWIDTHS = [
    1,
    1.5,
    2,
    3,
    5,
    7,
    10,
    15,
    20,
    30,
    50,
    70,
    100,
    150,
    200,
    300,
    500,
    700,
    1e3,
    1.5e3,
    2e3,
    3e3,
    5e3,
    7e3,
    10e3,
    15e3,
    20e3,
    30e3,
]

DEFAULT_MINIMUM_FREQUENCY = 9e3
DEFAULT_MAXIMUM_FREQUENCY = 8.5e9


class Vna(ScpiInstrument):
    """Interface to a Copper Mountain or Keysight ENA network analyzer.

        vna = Vna(resource_name="TCPIP0::127.0.0.1::5025::SOCKET")
        vna.configure_sweep(start=1e9, stop=2e9, points=401, power=-10)
        vna.set_trace(1, "s21", "log magnitude")
        frequencies, values = vna.read_trace(1)

    :param channel: Which measurement channel the settings act on.
    :param minimum_frequency: Lowest frequency this analyzer reaches, in hertz.
    :param maximum_frequency: Highest frequency this analyzer reaches.
    """

    IDENTIFIER = None

    def __init__(
        self,
        *args,
        channel=1,
        minimum_frequency=DEFAULT_MINIMUM_FREQUENCY,
        maximum_frequency=DEFAULT_MAXIMUM_FREQUENCY,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.channel = channel
        if float(minimum_frequency) >= float(maximum_frequency):
            raise RangeError(
                f"The minimum frequency ({minimum_frequency} Hz) must be below "
                f"the maximum ({maximum_frequency} Hz)."
            )
        self.minimum_frequency = float(minimum_frequency)
        self.maximum_frequency = float(maximum_frequency)

    @property
    def channel(self):
        """Returns which measurement channel the settings act on."""
        return self._channel

    @channel.setter
    def channel(self, value):
        self._channel = check_integer_range(
            value, 1, MAXIMUM_CHANNELS, "measurement channel"
        )

    def _check_trace(self, trace):
        return check_integer_range(trace, 1, MAXIMUM_TRACES, "trace number")

    def _check_frequency(self, value, name):
        return check_range(
            value, self.minimum_frequency, self.maximum_frequency, name, " Hz"
        )

    # Stimulus

    @property
    def start_frequency(self):
        """Returns the first frequency of the sweep, in hertz."""
        return self.query_float(f":SENS{self._channel}:FREQ:STAR?")

    @start_frequency.setter
    def start_frequency(self, value):
        self._check_frequency(value, "start frequency")
        self.write(f":SENS{self._channel}:FREQ:STAR {value}")

    @property
    def stop_frequency(self):
        """Returns the last frequency of the sweep, in hertz."""
        return self.query_float(f":SENS{self._channel}:FREQ:STOP?")

    @stop_frequency.setter
    def stop_frequency(self, value):
        self._check_frequency(value, "stop frequency")
        self.write(f":SENS{self._channel}:FREQ:STOP {value}")

    @property
    def centre_frequency(self):
        """Returns the center of the sweep, in hertz."""
        return self.query_float(f":SENS{self._channel}:FREQ:CENT?")

    @centre_frequency.setter
    def centre_frequency(self, value):
        self._check_frequency(value, "center frequency")
        self.write(f":SENS{self._channel}:FREQ:CENT {value}")

    @property
    def span(self):
        """Returns the width of the sweep, in hertz."""
        return self.query_float(f":SENS{self._channel}:FREQ:SPAN?")

    @span.setter
    def span(self, value):
        check_range(
            value, 0, self.maximum_frequency - self.minimum_frequency, "span", " Hz"
        )
        self.write(f":SENS{self._channel}:FREQ:SPAN {value}")

    @property
    def points(self):
        """Returns how many points the sweep measures."""
        return self.query_integer(f":SENS{self._channel}:SWE:POIN?")

    @points.setter
    def points(self, value):
        count = check_integer_range(value, 2, 500001, "sweep points")
        self.write(f":SENS{self._channel}:SWE:POIN {count}")

    @property
    def sweep_type(self):
        """Returns the sweep type: 'linear', 'logarithmic', 'segment' or 'power'."""
        reply = self.query(f":SENS{self._channel}:SWE:TYPE?").strip().upper()
        for name, code in SWEEP_TYPES.items():
            if reply.startswith(code.upper()[:3]):
                return name
        return reply.lower()

    @sweep_type.setter
    def sweep_type(self, value):
        code = check_choice(value, SWEEP_TYPES, "sweep type")
        self.write(f":SENS{self._channel}:SWE:TYPE {code}")

    @property
    def if_bandwidth(self):
        """Returns the iF bandwidth, in hertz.

        Narrowing this is the main lever on the noise floor: each factor of ten
        buys about 10 dB, and costs the same factor in sweep time.
        """
        return self.query_float(f":SENS{self._channel}:BAND?")

    @if_bandwidth.setter
    def if_bandwidth(self, value):
        check_range(value, IF_BANDWIDTHS[0], IF_BANDWIDTHS[-1], "IF bandwidth", " Hz")
        # The analyzer only offers a 1/1.5/2/3/5/7 ladder, so an arbitrary
        # value would be silently rounded to one of these anyway.
        _, bandwidth = nearest_allowed(value, IF_BANDWIDTHS, "IF bandwidth", " Hz")
        self.write(f":SENS{self._channel}:BAND {bandwidth}")

    @property
    def power(self):
        """Returns the stimulus power, in dBm."""
        return self.query_float(f":SOUR{self._channel}:POW?")

    @power.setter
    def power(self, value):
        check_range(value, -60, 20, "stimulus power", " dBm")
        self.write(f":SOUR{self._channel}:POW {value}")

    @property
    def averaging(self):
        """Returns whether sweep averaging is on."""
        return self.query_boolean(f":SENS{self._channel}:AVER?")

    @averaging.setter
    def averaging(self, value):
        state = check_boolean(value, "averaging")
        self.write(f":SENS{self._channel}:AVER {int(state)}")

    @property
    def average_count(self):
        """Returns how many sweeps are averaged together."""
        return self.query_integer(f":SENS{self._channel}:AVER:COUN?")

    @average_count.setter
    def average_count(self, value):
        count = check_integer_range(value, 1, 999, "average count")
        self.write(f":SENS{self._channel}:AVER:COUN {count}")

    def clear_averaging(self):
        """Discard the accumulated averages and start again."""
        self.write(f":SENS{self._channel}:AVER:CLE")

    def configure_sweep(
        self,
        start=None,
        stop=None,
        center=None,
        span=None,
        points=None,
        power=None,
        if_bandwidth=None,
        sweep_type=None,
    ):
        """Set up the stimulus in one call.

        Give either start and stop, or center and span, not both pairs.
        """
        if (start is not None or stop is not None) and (
            center is not None or span is not None
        ):
            raise RangeError(
                "A sweep is specified either by start and stop, or by center "
                "and span, but not by both."
            )

        if sweep_type is not None:
            self.sweep_type = sweep_type
        if start is not None:
            self.start_frequency = start
        if stop is not None:
            self.stop_frequency = stop
        if center is not None:
            self.centre_frequency = center
        if span is not None:
            self.span = span
        if points is not None:
            self.points = points
        if if_bandwidth is not None:
            self.if_bandwidth = if_bandwidth
        if power is not None:
            self.power = power

    # Traces

    @property
    def trace_count(self):
        """Returns how many traces the channel holds."""
        return self.query_integer(f":CALC{self._channel}:PAR:COUN?")

    @trace_count.setter
    def trace_count(self, value):
        count = check_integer_range(value, 1, MAXIMUM_TRACES, "trace count")
        self.write(f":CALC{self._channel}:PAR:COUN {count}")

    def set_trace(self, trace, parameter, trace_format=None):
        """Assign an S-parameter to a trace and choose how it is displayed.

        Raises the trace count first if the trace does not exist yet, since
        assigning to a trace the channel does not hold has no effect.

        :param trace: Which trace, counting from 1.
        :param parameter: 's11', 's21', 's12', 's22', or a receiver name.
        :param trace_format: How to display it, e.g. 'log magnitude'.
        """
        number = self._check_trace(trace)
        code = check_choice(parameter, PARAMETERS, "measurement parameter")

        if self.trace_count < number:
            self.trace_count = number

        self.write(f":CALC{self._channel}:PAR{number}:DEF {code}")
        self.write(f":CALC{self._channel}:PAR{number}:SEL")
        if trace_format is not None:
            self.set_trace_format(number, trace_format)

    def trace_parameter(self, trace=1):
        """Which parameter a trace is measuring."""
        number = self._check_trace(trace)
        reply = self.query(f":CALC{self._channel}:PAR{number}:DEF?").strip().upper()
        for name, code in PARAMETERS.items():
            if reply == code:
                return name
        return reply.lower()

    def set_trace_format(self, trace, trace_format):
        """Choose how a trace is displayed."""
        number = self._check_trace(trace)
        code = check_choice(trace_format, FORMATS, "trace format")
        self.write(f":CALC{self._channel}:TRAC{number}:FORM {code}")

    def trace_format(self, trace=1):
        """How a trace is displayed."""
        number = self._check_trace(trace)
        reply = self.query(f":CALC{self._channel}:TRAC{number}:FORM?").strip().upper()
        for name, code in FORMATS.items():
            if reply == code.upper()[: len(reply)]:
                return name
        return reply.lower()

    def select_trace(self, trace):
        """Make a trace the active one, which the display commands act on."""
        number = self._check_trace(trace)
        self.write(f":CALC{self._channel}:PAR{number}:SEL")

    def autoscale(self, trace=None):
        """Scale a trace to fit the display, or every trace if none is given."""
        if trace is None:
            for number in range(1, self.trace_count + 1):
                self.select_trace(number)
                self.write(f":DISP:WIND{self._channel}:TRAC{number}:Y:AUTO")
            return
        number = self._check_trace(trace)
        self.select_trace(number)
        self.write(f":DISP:WIND{self._channel}:TRAC{number}:Y:AUTO")

    # Reading data

    def frequencies(self):
        """Returns the frequency of every point in the sweep, in hertz."""
        return self.query_floats(f":SENS{self._channel}:FREQ:DATA?")

    def read_trace(self, trace=1):
        """Read one trace as it is displayed.

        Formatted data is one number per point, in whatever format the trace is
        set to. For a log magnitude trace that is decibels.

        :return: A tuple of (frequencies, values).
        """
        number = self._check_trace(trace)
        self.select_trace(number)
        values = self.query_floats(f":CALC{self._channel}:TRAC{number}:DATA:FDAT?")
        frequencies = self.frequencies()

        # Formatted data carries two numbers per point, the second of which is
        # zero for every format that has only one axis.
        if len(values) == 2 * len(frequencies):
            values = values[0::2]
        return frequencies, values

    def read_complex_trace(self, trace=1):
        """Read one trace as complex S-parameter values.

        This is the underlying measurement rather than the displayed version of
        it, and is what any further analysis should start from.

        :return: A tuple of (frequencies, complex values).
        """
        number = self._check_trace(trace)
        self.select_trace(number)
        raw = self.query_floats(f":CALC{self._channel}:TRAC{number}:DATA:SDAT?")
        frequencies = self.frequencies()

        values = [
            complex(raw[index], raw[index + 1]) for index in range(0, len(raw) - 1, 2)
        ]
        return frequencies, values

    def read_all_traces(self):
        """Read every trace in the channel as complex values.

        :return: A tuple of (frequencies, {parameter name: complex values}).
        """
        frequencies = self.frequencies()
        traces = {}
        for number in range(1, self.trace_count + 1):
            name = self.trace_parameter(number)
            _, values = self.read_complex_trace(number)
            traces[name] = values
        return frequencies, traces

    # Sweep control

    @property
    def continuous(self):
        """Returns whether the analyzer re-arms itself after each sweep."""
        return self.query_boolean(f":INIT{self._channel}:CONT?")

    @continuous.setter
    def continuous(self, value):
        state = check_boolean(value, "continuous sweeping")
        self.write(f":INIT{self._channel}:CONT {int(state)}")

    @property
    def trigger_source(self):
        """Returns what starts a sweep."""
        reply = self.query(":TRIG:SOUR?").strip().upper()
        for name, code in TRIGGER_SOURCES.items():
            if reply.startswith(code.upper()[:3]):
                return name
        return reply.lower()

    @trigger_source.setter
    def trigger_source(self, value):
        code = check_choice(value, TRIGGER_SOURCES, "trigger source")
        self.write(f":TRIG:SOUR {code}")

    def single_sweep(self, wait=True, timeout=600.0):
        """Run one sweep and wait for it to finish.

        Reading a trace mid-sweep returns a mixture of the new sweep and the
        previous one, so a measurement should complete a sweep first.

        :param wait: Block until the sweep is complete.
        """
        self.continuous = False
        self.write(f":INIT{self._channel}:IMM")
        if wait:
            self.wait_for_sweep(timeout=timeout)

    def wait_for_sweep(self, timeout=600.0):
        """Block until the analyzer reports the sweep is complete."""
        check_range(timeout, 0, 86400, "timeout", " s")
        self.write("*OPC")
        self.wait_until(
            lambda: bool(self.query_integer("*ESR?") & 0b1),
            timeout=timeout,
            interval=0.2,
            description="the sweep to finish",
        )

    def measure(self, trace=1, complex_values=False):
        """Run one sweep and read a trace from it.

        :param complex_values: Return complex S-parameters instead of the
                               displayed format.
        :return: A tuple of (frequencies, values).
        """
        self.single_sweep(wait=True)
        if complex_values:
            return self.read_complex_trace(trace)
        return self.read_trace(trace)

    # Markers

    def set_marker(self, number, frequency, trace=1):
        """Place a marker at a frequency."""
        marker = check_integer_range(number, 1, 10, "marker number")
        self._check_frequency(frequency, "marker frequency")
        self.select_trace(trace)
        self.write(f":CALC{self._channel}:MARK{marker} ON")
        self.write(f":CALC{self._channel}:MARK{marker}:X {frequency}")

    def marker_value(self, number, trace=1):
        """Read what a marker is sitting on.

        :return: A tuple of (frequency, value).
        """
        marker = check_integer_range(number, 1, 10, "marker number")
        self.select_trace(trace)
        frequency = self.query_float(f":CALC{self._channel}:MARK{marker}:X?")
        values = self.query_floats(f":CALC{self._channel}:MARK{marker}:Y?")
        return frequency, values[0] if values else None

    def marker_search(self, number, target="maximum", trace=1):
        """Move a marker to a feature of the trace.

        :param target: 'maximum', 'minimum', 'peak', 'left peak' or
                       'right peak'.
        """
        marker = check_integer_range(number, 1, 10, "marker number")
        code = check_choice(
            target,
            {
                "maximum": "MAXimum",
                "minimum": "MINimum",
                "peak": "PEAK",
                "left peak": "LPEak",
                "right peak": "RPEak",
            },
            "marker search target",
        )
        self.select_trace(trace)
        self.write(f":CALC{self._channel}:MARK{marker}:FUNC:TYPE {code}")
        self.write(f":CALC{self._channel}:MARK{marker}:FUNC:EXEC")
        return self.marker_value(marker, trace)

    def __repr__(self):
        return f"Vna({self._transport!r})"
