"""Driver for the Rigol DG1000Z series function and arbitrary waveform generators.

Every model in the family has two channels. The DG1032Z reaches 30 MHz and
the DG1062Z reaches 60 MHz. The model is read from ``*IDN?``, and
``max_frequency`` covers any variant not listed here.

As on most generators, amplitude and offset limits depend on what the output is
terminated into: an open circuit gives twice the amplitude that 50 ohms does,
so the limits follow the configured output impedance.

Commands and ranges are transcribed from the *DG1000Z Series Programming
Guide*.
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

# Maximum output frequency in hertz, per model. Every DG1000Z has 2 channels.
MODELS = {"DG1032Z": 30e6, "DG1062Z": 60e6}

DEFAULT_MODEL = "DG1062Z"
CHANNEL_COUNT = 2

MINIMUM_FREQUENCY = 1e-6

# Into 50 ohms. An open circuit doubles both.
MINIMUM_AMPLITUDE_INTO_50 = 1e-3
MAXIMUM_AMPLITUDE_INTO_50 = 10.0
MAXIMUM_OFFSET_INTO_50 = 5.0

WAVEFORMS = {
    "sine": "SINusoid",
    "square": "SQUare",
    "ramp": "RAMP",
    "pulse": "PULSe",
    "noise": "NOISe",
    "dc": "DC",
    "triangle": "TRIangle",
    "user": "USER",
    "arbitrary": "ARBitrary",
    "harmonic": "HARMonic",
}
VOLTAGE_UNITS = {"vpp": "VPP", "vrms": "VRMS", "dbm": "DBM"}
POLARITIES = {"normal": "NORMal", "inverted": "INVerted"}
BURST_MODES = {"triggered": "TRIGgered", "gated": "GATed", "infinite": "INFinity"}
SWEEP_SPACINGS = {"linear": "LINear", "logarithmic": "LOGarithmic", "step": "STEp"}
TRIGGER_SOURCES = {"internal": "INTernal", "external": "EXTernal", "manual": "MANual"}


class RigolDG1000Z(ScpiInstrument):
    """Interface to a Rigol DG1000Z waveform generator.

        source = RigolDG1000Z(resource_name="USB0::0x1AB1::0x0642::...::INSTR")
        source.apply("sine", frequency=1e3, amplitude=1.0)
        source.output = True

    :param model: Which model this is, e.g. 'DG1062Z'. Read from *IDN? when
                  not given.
    :param channel: Which output channel the properties act on.
    :param max_frequency: Frequency limit in hertz, for a variant not listed
                          in MODELS.
    """

    IDENTIFIER = "DG10"

    def __init__(self, *args, model=None, channel=1, max_frequency=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._model = None
        self.model = model if model is not None else self._detect_model()
        if max_frequency is not None:
            self._maximum_frequency = check_range(
                max_frequency, 1.0, 1e9, "maximum frequency", " Hz"
            )
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
        """Returns the model number, which sets the frequency limit."""
        return self._model

    @model.setter
    def model(self, value):
        name = str(value).upper().strip()
        if name not in MODELS:
            raise RangeError(
                f"'{value}' is not a DG1000Z series model. Known models are "
                f"{', '.join(sorted(MODELS))}. Pass max_frequency= for another."
            )
        self._model = name
        self._maximum_frequency = MODELS[name]

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
        self._channel = check_integer_range(value, 1, CHANNEL_COUNT, "output channel")

    def _source(self, tail):
        return f":SOUR{self._channel}:{tail}"

    def _amplitude_limits(self):
        scale = 2.0 if self.load_is_high_impedance() else 1.0
        return MINIMUM_AMPLITUDE_INTO_50 * scale, MAXIMUM_AMPLITUDE_INTO_50 * scale

    # Waveform

    @property
    def waveform(self):
        """Returns the shape being generated, e.g. 'sine' or 'square'."""
        reply = self.query(self._source("FUNC?")).strip().upper()
        for name, code in WAVEFORMS.items():
            # A DG1000Z answers with the short form of the mnemonic, so SIN
            # comes back for SINusoid. The capital letters of the mnemonic are
            # that short form, which is how SCPI writes them.
            short = "".join(letter for letter in code if letter.isupper())
            if reply == short or reply.startswith(short):
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
        # These limits are peak-to-peak volts. The same numbers are not the
        # right bounds in volts rms or in dBm, where the conversion depends on
        # the load and on the shape being generated, and applying them anyway
        # would refuse ordinary levels. In those units the generator judges its
        # own range.
        if self.voltage_unit == "vpp":
            smallest, largest = self._amplitude_limits()
            check_range(value, smallest, largest, "amplitude", " Vpp")
        self.write(self._source(f"VOLT {value}"))

    @property
    def offset(self):
        """Returns the DC offset, in volts."""
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
        check_range(value, 0, 360, "phase", " degrees")
        self.write(self._source(f"PHAS {value}"))

    def align_phase(self):
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

    def apply(self, waveform, frequency=None, amplitude=None, offset=None, phase=None):
        """Set shape, frequency, amplitude, offset and phase in one command."""
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
            for value in (frequency, amplitude, offset, phase)
        ]
        # Trailing defaults add nothing, so send only as many as are needed.
        while parts and parts[-1] == "DEF":
            parts.pop()
        arguments = (" " + ",".join(parts)) if parts else ""
        self.write(self._source(f"APPL:{code}{arguments}"))

    # Output

    @property
    def output(self):
        """Returns whether the output is on."""
        return self.query_boolean(f":OUTP{self._channel}?")

    @output.setter
    def output(self, value):
        state = check_boolean(value, "output")
        self.write(f":OUTP{self._channel} {'ON' if state else 'OFF'}")

    @property
    def load(self):
        """Returns the impedance the output is terminated into, in ohms.

        Returns ``float('inf')`` for a high-impedance load. The generator uses
        this to work out the amplitude it must produce, so it has to match how
        the output is really terminated.
        """
        reply = self.query(f":OUTP{self._channel}:IMP?").strip()
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
                self.write(f":OUTP{self._channel}:IMP INF")
                return
            raise RangeError(
                "The output load must be a resistance in ohms, or 'infinity' "
                f"for a high-impedance load, but got {value!r}."
            )
        check_range(value, 1, 10000, "output load", " ohms")
        self.write(f":OUTP{self._channel}:IMP {value}")

    def load_is_high_impedance(self):
        """Whether the output is set up to drive a high-impedance load."""
        return self.load == float("inf")

    @property
    def polarity(self):
        """Returns whether the output is 'normal' or 'inverted'."""
        reply = self.query(f":OUTP{self._channel}:POL?").strip().upper()
        return "inverted" if reply.startswith("INV") else "normal"

    @polarity.setter
    def polarity(self, value):
        code = check_choice(value, POLARITIES, "output polarity")
        self.write(f":OUTP{self._channel}:POL {code}")

    @property
    def sync_output(self):
        """Returns whether the sync output is on."""
        return self.query_boolean(f":OUTP{self._channel}:SYNC?")

    @sync_output.setter
    def sync_output(self, value):
        state = check_boolean(value, "sync output")
        self.write(f":OUTP{self._channel}:SYNC {'ON' if state else 'OFF'}")

    # Burst

    def configure_burst(
        self, cycles=1, mode="triggered", phase=0.0, period=None, trigger="internal"
    ):
        """Set up burst mode and enable it.

        :param cycles: Cycles per burst.
        :param mode: 'triggered', 'gated' or 'infinite'.
        :param phase: Phase each burst starts at, in degrees.
        :param period: Interval between internally triggered bursts, in
                       seconds.
        :param trigger: What starts each burst.
        """
        code = check_choice(mode, BURST_MODES, "burst mode")
        self.write(self._source(f"BURS:MODE {code}"))

        count = check_integer_range(cycles, 1, 1000000, "burst cycles")
        self.write(self._source(f"BURS:NCYC {count}"))

        check_range(phase, 0, 360, "burst phase", " degrees")
        self.write(self._source(f"BURS:PHAS {phase}"))

        if period is not None:
            check_range(period, 1e-6, 500, "burst period", " s")
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
        self.write(self._source(f"BURS:STAT {'ON' if state else 'OFF'}"))

    # Sweep

    def configure_sweep(
        self, start, stop, duration=1.0, spacing="linear", hold=0.0, return_time=0.0
    ):
        """Set up a frequency sweep and enable it.

        :param start: Frequency to start from, in hertz.
        :param stop: Frequency to finish at, in hertz.
        :param duration: Seconds to take getting there.
        :param spacing: 'linear', 'logarithmic' or 'step'.
        :param hold: Seconds to dwell at the stop frequency.
        :param return_time: Seconds to take returning to the start.
        """
        for value, name in ((start, "sweep start"), (stop, "sweep stop")):
            check_range(value, MINIMUM_FREQUENCY, self._maximum_frequency, name, " Hz")
        check_range(duration, 1e-3, 500, "sweep time", " s")
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
        self.write(self._source(f"SWE:STAT {'ON' if state else 'OFF'}"))

    # Modulation

    def configure_amplitude_modulation(
        self, depth=100.0, frequency=100.0, shape="sine", source="internal"
    ):
        """Set up amplitude modulation and enable it."""
        check_range(depth, 0, 120, "modulation depth", " percent")
        check_range(frequency, 2e-3, 1e6, "modulating frequency", " Hz")
        code = check_choice(shape, WAVEFORMS, "modulating waveform")
        internal = check_choice(
            source, {"internal": "INT", "external": "EXT"}, "modulation source"
        )

        self.write(self._source(f"MOD:AM:SOUR {internal}"))
        self.write(self._source(f"MOD:AM:INT:FUNC {code}"))
        self.write(self._source(f"MOD:AM:INT:FREQ {frequency}"))
        self.write(self._source(f"MOD:AM:DEPT {depth}"))
        self.write(self._source("MOD:AM:STAT ON"))
        self.write(self._source("MOD:STAT ON"))

    def configure_frequency_modulation(
        self, deviation, frequency=100.0, shape="sine", source="internal"
    ):
        """Set up frequency modulation and enable it."""
        check_range(deviation, 0, self._maximum_frequency, "frequency deviation", " Hz")
        check_range(frequency, 2e-3, 1e6, "modulating frequency", " Hz")
        code = check_choice(shape, WAVEFORMS, "modulating waveform")
        internal = check_choice(
            source, {"internal": "INT", "external": "EXT"}, "modulation source"
        )

        self.write(self._source(f"MOD:FM:SOUR {internal}"))
        self.write(self._source(f"MOD:FM:INT:FUNC {code}"))
        self.write(self._source(f"MOD:FM:INT:FREQ {frequency}"))
        self.write(self._source(f"MOD:FM:DEV {deviation}"))
        self.write(self._source("MOD:FM:STAT ON"))
        self.write(self._source("MOD:STAT ON"))

    def disable_modulation(self):
        """Turn modulation off."""
        self.write(self._source("MOD:STAT OFF"))

    # Triggering

    @property
    def trigger_source(self):
        """Returns what triggers a burst or a sweep."""
        reply = self.query(self._source("BURS:TRIG:SOUR?")).strip().upper()
        for name, code in TRIGGER_SOURCES.items():
            if reply.startswith(code.upper()[:3]):
                return name
        return reply.lower()

    @trigger_source.setter
    def trigger_source(self, value):
        code = check_choice(value, TRIGGER_SOURCES, "trigger source")
        self.write(self._source(f"BURS:TRIG:SOUR {code}"))

    def trigger(self):
        """Send a software trigger."""
        self.write(self._source("BURS:TRIG"))

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
        return f"Rigol{self._model}({self._transport!r})"
