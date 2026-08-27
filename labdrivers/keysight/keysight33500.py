"""Driver for the Keysight 33500B and 33600A Trueform waveform generators.

Covers the one- and two-channel models of both families, which differ in how
fast they can go: 20 MHz for a 33509B, 30 MHz for a 33521B, 80 MHz for a
33612A and 120 MHz for a 33622A. The model is read from ``*IDN?`` and sets the
frequency limit.

Amplitude and offset limits depend on what the output is terminated into. Into
50 ohms the generator manages 10 Vpp and 5 V of offset, but into an open
circuit the same settings produce twice that, so the limits follow the
configured output load.

Commands and ranges are transcribed from the *33500 Series Operating and
Service Guide*.
"""

import time

from ..core import (
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import RangeError
from ..core.sweep import round_trip, sweep_values

# Channel count and maximum output frequency in hertz, per model.
MODELS = {
    "33509B": (1, 20e6),
    "33510B": (2, 20e6),
    "33511B": (1, 20e6),
    "33512B": (2, 20e6),
    "33519B": (1, 30e6),
    "33520B": (2, 30e6),
    "33521B": (1, 30e6),
    "33522B": (2, 30e6),
    "33611A": (1, 80e6),
    "33612A": (2, 80e6),
    "33621A": (1, 120e6),
    "33622A": (2, 120e6),
}

DEFAULT_MODEL = "33511B"

MINIMUM_FREQUENCY = 1e-6

# Into 50 ohms. An open circuit doubles both, since the generator no longer
# loses half its output across the source impedance.
MAXIMUM_AMPLITUDE_INTO_50 = 10.0
MINIMUM_AMPLITUDE_INTO_50 = 1e-3
MAXIMUM_OFFSET_INTO_50 = 5.0

WAVEFORMS = {
    "sine": "SIN",
    "square": "SQU",
    "ramp": "RAMP",
    "pulse": "PULS",
    "noise": "NOIS",
    "dc": "DC",
    "prbs": "PRBS",
    "arbitrary": "ARB",
    "triangle": "TRI",
}
VOLTAGE_UNITS = {"vpp": "VPP", "vrms": "VRMS", "dbm": "DBM"}
POLARITIES = {"normal": "NORM", "inverted": "INV"}
BURST_MODES = {"triggered": "TRIG", "gated": "GAT"}
SWEEP_SPACINGS = {"linear": "LIN", "logarithmic": "LOG"}
TRIGGER_SOURCES = {
    "immediate": "IMM",
    "external": "EXT",
    "timer": "TIM",
    "bus": "BUS",
}
MODULATION_SHAPES = {
    "sine": "SIN",
    "square": "SQU",
    "ramp": "RAMP",
    "negative ramp": "NRAM",
    "triangle": "TRI",
    "noise": "NOIS",
    "prbs": "PRBS",
    "arbitrary": "ARB",
}


class Keysight33500(ScpiInstrument):
    """Interface to a Keysight 33500B or 33600A waveform generator.

        source = Keysight33500(resource_name="TCPIP0::192.168.0.20::INSTR")
        source.apply("sine", frequency=1e3, amplitude=0.5)
        source.output = True

    Two-channel models take a channel number on every setting::

        source.channel = 2
        source.frequency = 2e3

    :param model: Which model this is, e.g. '33622A'. Read from *IDN? when not
                  given.
    :param channel: Which output channel the properties act on.
    """

    IDENTIFIER = "335"

    def __init__(self, *args, model=None, channel=1, **kwargs):
        super().__init__(*args, **kwargs)
        self._model = None
        self.model = model if model is not None else self._detect_model()
        self.channel = channel

    def _detect_model(self):
        try:
            identity = self.identify().upper()
        except Exception:
            return DEFAULT_MODEL
        for name in MODELS:
            if name in identity:
                return name
        return DEFAULT_MODEL

    @property
    def model(self):
        """Returns the model number.

        The model sets the channel count and the frequency limit.
        """
        return self._model

    @model.setter
    def model(self, value):
        name = str(value).upper().strip()
        if name not in MODELS:
            raise RangeError(
                f"'{value}' is not a 33500B or 33600A series model. Known models "
                f"are {', '.join(sorted(MODELS))}."
            )
        self._model = name
        self._channel_count, self._maximum_frequency = MODELS[name]

    @property
    def channel_count(self):
        """Returns how many output channels this model has."""
        return self._channel_count

    @property
    def maximum_frequency(self):
        """Returns the highest frequency this model will generate, in hertz."""
        return self._maximum_frequency

    @property
    def channel(self):
        """Returns which output channel the properties act on."""
        return self._channel

    @channel.setter
    def channel(self, value):
        self._channel = check_integer_range(
            value, 1, self._channel_count, "output channel"
        )

    def _source(self, tail):
        return f"SOUR{self._channel}:{tail}"

    def _amplitude_limits(self):
        """Amplitude limits for the load the output is set up for.

        Into an open circuit the generator delivers twice the amplitude it
        does into 50 ohms, so the usable range doubles with it.
        """
        scale = 2.0 if self.load_is_high_impedance() else 1.0
        return MINIMUM_AMPLITUDE_INTO_50 * scale, MAXIMUM_AMPLITUDE_INTO_50 * scale

    # Waveform

    @property
    def waveform(self):
        """Returns the shape being generated, e.g. 'sine' or 'square'."""
        reply = self.query(self._source("FUNC?")).strip().upper()
        for name, code in WAVEFORMS.items():
            if reply == code or reply.startswith(code):
                return name
        return reply.lower()

    @waveform.setter
    def waveform(self, value):
        code = check_choice(value, WAVEFORMS, "waveform")
        self.write(self._source(f"FUNC {code}"))

    @property
    def frequency(self):
        """Returns the output frequency, in hertz."""
        return self.query_float(self._source("FREQ?"))

    @frequency.setter
    def frequency(self, value):
        check_range(
            value, MINIMUM_FREQUENCY, self._maximum_frequency, "frequency", " Hz"
        )
        self.write(self._source(f"FREQ {value}"))

    @property
    def amplitude(self):
        """Returns the output amplitude, in the configured voltage unit."""
        return self.query_float(self._source("VOLT?"))

    @amplitude.setter
    def amplitude(self, value):
        smallest, largest = self._amplitude_limits()
        check_range(value, smallest, largest, "amplitude", " Vpp")
        self.write(self._source(f"VOLT {value}"))

    @property
    def offset(self):
        """Returns the dC offset, in volts."""
        return self.query_float(self._source("VOLT:OFFS?"))

    @offset.setter
    def offset(self, value):
        limit = MAXIMUM_OFFSET_INTO_50 * (2.0 if self.load_is_high_impedance() else 1.0)
        check_range(value, -limit, limit, "offset", " V")
        self.write(self._source(f"VOLT:OFFS {value}"))

    @property
    def high_level(self):
        """Returns the upper level of the waveform, in volts."""
        return self.query_float(self._source("VOLT:HIGH?"))

    @high_level.setter
    def high_level(self, value):
        self.write(self._source(f"VOLT:HIGH {value}"))

    @property
    def low_level(self):
        """Returns the lower level of the waveform, in volts."""
        return self.query_float(self._source("VOLT:LOW?"))

    @low_level.setter
    def low_level(self, value):
        self.write(self._source(f"VOLT:LOW {value}"))

    @property
    def voltage_unit(self):
        """Returns the unit amplitude is expressed in: 'vpp', 'vrms' or 'dbm'."""
        reply = self.query(self._source("VOLT:UNIT?")).strip().upper()
        for name, code in VOLTAGE_UNITS.items():
            if reply.startswith(code):
                return name
        return reply.lower()

    @voltage_unit.setter
    def voltage_unit(self, value):
        code = check_choice(value, VOLTAGE_UNITS, "voltage unit")
        self.write(self._source(f"VOLT:UNIT {code}"))

    @property
    def phase(self):
        """Returns the output phase, in degrees."""
        return self.query_float(self._source("PHAS?"))

    @phase.setter
    def phase(self, value):
        check_range(value, -360, 360, "phase", " degrees")
        self.write(self._source(f"PHAS {value}"))

    def synchronise_phase(self):
        """Align the phase of both channels."""
        self.write(self._source("PHAS:SYNC"))

    @property
    def duty_cycle(self):
        """Returns the duty cycle of a square wave, as a percentage."""
        return self.query_float(self._source("FUNC:SQU:DCYC?"))

    @duty_cycle.setter
    def duty_cycle(self, value):
        check_range(value, 0.01, 99.99, "duty cycle", " percent")
        self.write(self._source(f"FUNC:SQU:DCYC {value}"))

    @property
    def ramp_symmetry(self):
        """Returns the fraction of a ramp cycle spent rising, as a percentage."""
        return self.query_float(self._source("FUNC:RAMP:SYMM?"))

    @ramp_symmetry.setter
    def ramp_symmetry(self, value):
        check_range(value, 0, 100, "ramp symmetry", " percent")
        self.write(self._source(f"FUNC:RAMP:SYMM {value}"))

    @property
    def pulse_width(self):
        """Returns the width of a pulse, in seconds."""
        return self.query_float(self._source("FUNC:PULS:WIDT?"))

    @pulse_width.setter
    def pulse_width(self, value):
        check_range(value, 0, 1e6, "pulse width", " s")
        self.write(self._source(f"FUNC:PULS:WIDT {value}"))

    @property
    def pulse_edge_time(self):
        """Returns the rise and fall time of a pulse, in seconds."""
        return self.query_float(self._source("FUNC:PULS:TRAN:LEAD?"))

    @pulse_edge_time.setter
    def pulse_edge_time(self, value):
        check_range(value, 0, 1.0, "pulse edge time", " s")
        self.write(self._source(f"FUNC:PULS:TRAN:LEAD {value}"))
        self.write(self._source(f"FUNC:PULS:TRAN:TRA {value}"))

    def apply(self, waveform, frequency=None, amplitude=None, offset=None):
        """Set the shape, frequency, amplitude and offset in one command.

        Also cancels any modulation, sweep or burst and turns the output on,
        which is what makes it a quick way back to a known state.
        """
        code = check_choice(waveform, WAVEFORMS, "waveform")
        if frequency is not None:
            check_range(
                frequency,
                MINIMUM_FREQUENCY,
                self._maximum_frequency,
                "frequency",
                " Hz",
            )
        parts = [
            "DEF" if value is None else str(value)
            for value in (frequency, amplitude, offset)
        ]
        self.write(self._source(f"APPL:{code} " + ",".join(parts)))

    # Output

    @property
    def output(self):
        """Returns whether the output is on."""
        return self.query_boolean(f"OUTP{self._channel}?")

    @output.setter
    def output(self, value):
        state = check_boolean(value, "output")
        self.write(f"OUTP{self._channel} {int(state)}")

    @property
    def load(self):
        """Returns the impedance the output is terminated into, in ohms.

        Returns ``float('inf')`` for a high-impedance load. This is what the
        generator uses to work out the amplitude it must produce, so it has to
        match reality or every level will be out by a factor of two.
        """
        reply = self.query(f"OUTP{self._channel}:LOAD?").strip()
        try:
            value = float(reply)
        except ValueError:
            return float("inf")
        return float("inf") if value >= 9.9e37 else value

    @load.setter
    def load(self, value):
        if isinstance(value, str) or value == float("inf"):
            word = str(value).strip().lower()
            if word in ("inf", "infinity", "high", "high impedance", "highz", "high z"):
                self.write(f"OUTP{self._channel}:LOAD INF")
                return
            raise RangeError(
                "The output load must be a resistance in ohms, or 'infinity' "
                f"for a high-impedance load, but got {value!r}."
            )
        check_range(value, 1, 10000, "output load", " ohms")
        self.write(f"OUTP{self._channel}:LOAD {value}")

    def load_is_high_impedance(self):
        """Whether the output is set up to drive a high-impedance load."""
        return self.load == float("inf")

    @property
    def polarity(self):
        """Returns whether the output is 'normal' or 'inverted'."""
        reply = self.query(f"OUTP{self._channel}:POL?").strip().upper()
        return "inverted" if reply.startswith("INV") else "normal"

    @polarity.setter
    def polarity(self, value):
        code = check_choice(value, POLARITIES, "output polarity")
        self.write(f"OUTP{self._channel}:POL {code}")

    @property
    def sync_output(self):
        """Returns whether the sync output is on."""
        return self.query_boolean("OUTP:SYNC?")

    @sync_output.setter
    def sync_output(self, value):
        state = check_boolean(value, "sync output")
        self.write(f"OUTP:SYNC {int(state)}")

    # Burst

    def configure_burst(
        self, cycles=1, mode="triggered", phase=0.0, period=None, trigger="immediate"
    ):
        """Set up burst mode and enable it.

        :param cycles: Cycles per burst, or 'infinite'.
        :param mode: 'triggered' fires a set number of cycles per trigger, and
                     'gated' runs for as long as the gate signal is true.
        :param phase: Phase each burst starts at, in degrees.
        :param period: Interval between internally triggered bursts, in
                       seconds.
        :param trigger: What starts each burst.
        """
        code = check_choice(mode, BURST_MODES, "burst mode")
        self.write(self._source(f"BURS:MODE {code}"))

        if str(cycles).strip().lower() in ("inf", "infinite", "infinity"):
            self.write(self._source("BURS:NCYC INF"))
        else:
            count = check_integer_range(cycles, 1, 100000000, "burst cycles")
            self.write(self._source(f"BURS:NCYC {count}"))

        check_range(phase, -360, 360, "burst phase", " degrees")
        self.write(self._source(f"BURS:PHAS {phase}"))

        if period is not None:
            check_range(period, 1e-6, 8000, "burst period", " s")
            self.write(self._source(f"BURS:INT:PER {period}"))

        self.trigger_source = trigger
        self.write(self._source("BURS:STAT ON"))

    @property
    def burst_enabled(self):
        """Returns whether burst mode is on."""
        return self.query_boolean(self._source("BURS:STAT?"))

    @burst_enabled.setter
    def burst_enabled(self, value):
        state = check_boolean(value, "burst mode")
        self.write(self._source(f"BURS:STAT {int(state)}"))

    # Sweep

    def configure_sweep(
        self, start, stop, duration=1.0, spacing="linear", hold=0.0, return_time=0.0
    ):
        """Set up a frequency sweep and enable it.

        :param start: Frequency to start from, in hertz.
        :param stop: Frequency to finish at, in hertz.
        :param duration: Seconds to take getting there.
        :param spacing: 'linear' or 'logarithmic'.
        :param hold: Seconds to dwell at the stop frequency.
        :param return_time: Seconds to take returning to the start.
        """
        for value, name in ((start, "sweep start"), (stop, "sweep stop")):
            check_range(value, MINIMUM_FREQUENCY, self._maximum_frequency, name, " Hz")
        check_range(duration, 1e-3, 250000, "sweep time", " s")
        code = check_choice(spacing, SWEEP_SPACINGS, "sweep spacing")

        self.write(self._source(f"FREQ:STAR {start}"))
        self.write(self._source(f"FREQ:STOP {stop}"))
        self.write(self._source(f"SWE:SPAC {code}"))
        self.write(self._source(f"SWE:TIME {duration}"))
        self.write(self._source(f"SWE:HTIM {hold}"))
        self.write(self._source(f"SWE:RTIM {return_time}"))
        self.write(self._source("SWE:STAT ON"))

    @property
    def sweep_enabled(self):
        """Returns whether frequency sweeping is on."""
        return self.query_boolean(self._source("SWE:STAT?"))

    @sweep_enabled.setter
    def sweep_enabled(self, value):
        state = check_boolean(value, "frequency sweep")
        self.write(self._source(f"SWE:STAT {int(state)}"))

    # Modulation

    def configure_amplitude_modulation(
        self, depth=100.0, frequency=100.0, shape="sine", source="internal"
    ):
        """Set up amplitude modulation and enable it.

        :param depth: Modulation depth, as a percentage.
        :param frequency: Modulating frequency, in hertz.
        :param shape: Shape of the modulating waveform.
        """
        check_range(depth, 0, 120, "modulation depth", " percent")
        check_range(frequency, MINIMUM_FREQUENCY, 1e6, "modulating frequency", " Hz")
        code = check_choice(shape, MODULATION_SHAPES, "modulating waveform")
        internal = check_choice(
            source, {"internal": "INT", "external": "EXT"}, "modulation source"
        )

        self.write(self._source(f"AM:SOUR {internal}"))
        self.write(self._source(f"AM:INT:FUNC {code}"))
        self.write(self._source(f"AM:INT:FREQ {frequency}"))
        self.write(self._source(f"AM:DEPT {depth}"))
        self.write(self._source("AM:STAT ON"))

    def configure_frequency_modulation(
        self, deviation, frequency=100.0, shape="sine", source="internal"
    ):
        """Set up frequency modulation and enable it.

        :param deviation: Peak frequency deviation, in hertz.
        :param frequency: Modulating frequency, in hertz.
        :param shape: Shape of the modulating waveform.
        """
        check_range(deviation, 0, self._maximum_frequency, "frequency deviation", " Hz")
        check_range(frequency, MINIMUM_FREQUENCY, 1e6, "modulating frequency", " Hz")
        code = check_choice(shape, MODULATION_SHAPES, "modulating waveform")
        internal = check_choice(
            source, {"internal": "INT", "external": "EXT"}, "modulation source"
        )

        self.write(self._source(f"FM:SOUR {internal}"))
        self.write(self._source(f"FM:INT:FUNC {code}"))
        self.write(self._source(f"FM:INT:FREQ {frequency}"))
        self.write(self._source(f"FM:DEV {deviation}"))
        self.write(self._source("FM:STAT ON"))

    def disable_modulation(self):
        """Turn off amplitude, frequency, phase and pulse-width modulation."""
        for subsystem in ("AM", "FM", "PM", "PWM"):
            self.write(self._source(f"{subsystem}:STAT OFF"))

    # Triggering

    @property
    def trigger_source(self):
        """Returns what triggers a burst or a sweep."""
        reply = self.query(f"TRIG{self._channel}:SOUR?").strip().upper()
        for name, code in TRIGGER_SOURCES.items():
            if reply.startswith(code):
                return name
        return reply.lower()

    @trigger_source.setter
    def trigger_source(self, value):
        code = check_choice(value, TRIGGER_SOURCES, "trigger source")
        self.write(f"TRIG{self._channel}:SOUR {code}")

    def trigger(self):
        """Send a software trigger."""
        self.write("*TRG")

    # Common procedures

    def sweep_frequency(
        self,
        start,
        stop,
        points=None,
        step=None,
        settle=0.0,
        spacing="linear",
        return_to_start=False,
    ):
        """Step the output frequency, yielding at each point.

        Distinct from configure_sweep(), which hands the whole sweep to the
        instrument. Stepping from Python lets a measurement be taken at each
        frequency.

        :yield: The frequency actually set, in hertz.
        """
        frequencies = sweep_values(
            start, stop, points=points, step=step, spacing=spacing
        )
        if return_to_start:
            frequencies = round_trip(frequencies)

        for frequency in frequencies:
            self.frequency = frequency
            if settle:
                time.sleep(settle)
            yield frequency

    def safe_shutdown(self):
        """Turn the output off and cancel any modulation, burst or sweep."""
        self.output = False
        self.disable_modulation()
        self.burst_enabled = False
        self.sweep_enabled = False

    def __repr__(self):
        return f"Keysight{self._model}({self._transport!r})"
