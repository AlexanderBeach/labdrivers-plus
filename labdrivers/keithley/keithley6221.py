"""Driver for the Keithley 6221 AC and DC current source.

The 6221 is a current source only: it has no voltage source and no voltmeter of
its own. Its differential-conductance, delta and pulse-delta modes work by
driving a 2182A nanovoltmeter over the trigger link and reading the voltage back
through it, so those modes need both instruments cabled together.

Commands and ranges are transcribed from the *Model 6220/6221 Reference
Manual*.
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

# The 6221 sources up to 105 mA and holds off up to 105 V of compliance.
MAXIMUM_CURRENT = 0.105
MINIMUM_CURRENT = 2e-12
MINIMUM_COMPLIANCE = 0.1
MAXIMUM_COMPLIANCE = 105.0

MINIMUM_WAVE_FREQUENCY = 1e-3
MAXIMUM_WAVE_FREQUENCY = 1e5

WAVE_FUNCTIONS = {
    "sine": "SIN",
    "ramp": "RAMP",
    "square": "SQU",
    "arbitrary1": "ARB1",
    "arbitrary2": "ARB2",
    "arbitrary3": "ARB3",
    "arbitrary4": "ARB4",
}
RANGING = {"best": "BEST", "fixed": "FIX"}
FILTER_TYPES = {"moving": "MOV", "repeating": "REP"}
SWEEP_SPACINGS = {"linear": "LIN", "logarithmic": "LOG"}

# Units the delta modes can report the measured value in.
DELTA_UNITS = {"volts": "V", "ohms": "OHMS", "watts": "W", "siemens": "SIEM"}

# Trigger link lines usable for the waveform phase marker and for the
# 6221-to-2182A connection.
TRIGGER_LINES = (1, 2, 3, 4, 5, 6)

MAXIMUM_BUFFER_POINTS = 65536


class Keithley6221(ScpiInstrument):
    """Interface to a Keithley 6221 current source.

    source = Keithley6221(gpib_address=12)
    source.compliance = 10
    source.wave_function = "sine"
    source.wave_amplitude = 1e-6
    source.wave_frequency = 17.777
    source.arm_waveform()
    source.start_waveform()
    """

    IDENTIFIER = "622"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Report the status registers in ASCII rather than binary, so the
        # status queries return something parseable.
        self.write("FORM:SREG ASC")

    # Output and source

    @property
    def output(self):
        """Returns whether the output terminals are live."""
        return self.query_boolean("OUTP:STAT?")

    @output.setter
    def output(self, value):
        state = check_boolean(value, "output")
        self.write(f"OUTP:STAT {int(state)}")

    @property
    def output_low_grounded(self):
        """Returns whether output low is tied to earth."""
        return self.query_boolean("OUTP:LTE?")

    @output_low_grounded.setter
    def output_low_grounded(self, value):
        state = check_boolean(value, "output low earth connection")
        self.write(f"OUTP:LTE {int(state)}")

    @property
    def source_current(self):
        """Returns the DC output current, in amps."""
        return self.query_float("SOUR:CURR?")

    @source_current.setter
    def source_current(self, value):
        check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "source current", " A")
        self.write(f"SOUR:CURR {value}")

    @property
    def compliance(self):
        """Returns the voltage the source will not exceed, in volts."""
        return self.query_float("SOUR:CURR:COMP?")

    @compliance.setter
    def compliance(self, value):
        check_range(
            value,
            MINIMUM_COMPLIANCE,
            MAXIMUM_COMPLIANCE,
            "compliance voltage",
            " V",
        )
        self.write(f"SOUR:CURR:COMP {value}")

    def in_compliance(self):
        """Whether the source is currently clamped by its compliance limit.

        A measurement taken in compliance is not the one that was asked for.
        """
        return self.query_boolean("SOUR:CURR:COMP:TRIP?")

    @property
    def source_range(self):
        """Returns the output current range, in amps."""
        return self.query_float("SOUR:CURR:RANG?")

    @source_range.setter
    def source_range(self, value):
        check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "source range", " A")
        self.write(f"SOUR:CURR:RANG {value}")

    @property
    def source_auto_range(self):
        """Returns whether the source picks its own range."""
        return self.query_boolean("SOUR:CURR:RANG:AUTO?")

    @source_auto_range.setter
    def source_auto_range(self, value):
        state = check_boolean(value, "source autorange")
        self.write(f"SOUR:CURR:RANG:AUTO {int(state)}")

    @property
    def source_delay(self):
        """Returns the settling delay after each source change, in seconds."""
        return self.query_float("SOUR:DEL?")

    @source_delay.setter
    def source_delay(self, value):
        check_range(value, 1e-3, 999999.999, "source delay", " s")
        self.write(f"SOUR:DEL {value}")

    def ramp_source_current(self, target, steps=1000, delay=0.01):
        """Walk the DC output to a target current in steps.

        :param target: Current to finish at, in amps.
        :param steps: How many intermediate levels to pass through.
        :param delay: Seconds to wait at each step.
        """
        target = check_range(
            target, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "source current", " A"
        )
        steps = check_integer_range(steps, 1, 1000000, "number of steps")
        check_range(delay, 0, 3600, "step delay", " s")
        start = self.source_current
        for step in range(1, steps + 1):
            self.source_current = start + (target - start) * step / steps
            time.sleep(delay)

    # Waveform generation

    @property
    def wave_function(self):
        """Returns the waveform shape: 'sine', 'ramp', 'square' or 'arbitrary1'..4."""
        reply = self.query("SOUR:WAVE:FUNC?").strip().upper()
        for name, code in WAVE_FUNCTIONS.items():
            if reply.startswith(code):
                return name
        return reply

    @wave_function.setter
    def wave_function(self, value):
        code = check_choice(value, WAVE_FUNCTIONS, "waveform function")
        self.write(f"SOUR:WAVE:FUNC {code}")

    @property
    def wave_amplitude(self):
        """Returns the waveform amplitude, in amps peak."""
        return self.query_float("SOUR:WAVE:AMPL?")

    @wave_amplitude.setter
    def wave_amplitude(self, value):
        check_range(value, MINIMUM_CURRENT, MAXIMUM_CURRENT, "waveform amplitude", " A")
        self.write(f"SOUR:WAVE:AMPL {value}")

    @property
    def wave_offset(self):
        """Returns the waveform DC offset, in amps."""
        return self.query_float("SOUR:WAVE:OFFS?")

    @wave_offset.setter
    def wave_offset(self, value):
        check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "waveform offset", " A")
        self.write(f"SOUR:WAVE:OFFS {value}")

    def ramp_wave_offset(self, target, steps=1000, delay=0.01):
        """Walk the waveform offset to a target in steps.

        Changing the offset in one jump puts a step through the device under
        test, so this walks it there instead.
        """
        target = check_range(
            target, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "waveform offset", " A"
        )
        steps = check_integer_range(steps, 1, 1000000, "number of steps")
        check_range(delay, 0, 3600, "step delay", " s")
        start = self.wave_offset
        for step in range(1, steps + 1):
            self.wave_offset = start + (target - start) * step / steps
            time.sleep(delay)

    @property
    def wave_frequency(self):
        """Returns the waveform frequency, in hertz."""
        return self.query_float("SOUR:WAVE:FREQ?")

    @wave_frequency.setter
    def wave_frequency(self, value):
        check_range(
            value,
            MINIMUM_WAVE_FREQUENCY,
            MAXIMUM_WAVE_FREQUENCY,
            "waveform frequency",
            " Hz",
        )
        self.write(f"SOUR:WAVE:FREQ {value}")

    @property
    def wave_duty_cycle(self):
        """Returns the duty cycle of a square or ramp waveform, as a percentage."""
        return self.query_float("SOUR:WAVE:DCYC?")

    @wave_duty_cycle.setter
    def wave_duty_cycle(self, value):
        check_range(value, 0, 100, "waveform duty cycle", " percent")
        self.write(f"SOUR:WAVE:DCYC {value}")

    @property
    def wave_ranging(self):
        """Returns how the source ranges during a waveform: 'best' or 'fixed'."""
        reply = self.query("SOUR:WAVE:RANG?").strip().upper()
        return "fixed" if reply.startswith("FIX") else "best"

    @wave_ranging.setter
    def wave_ranging(self, value):
        code = check_choice(value, RANGING, "waveform ranging")
        self.write(f"SOUR:WAVE:RANG {code}")

    @property
    def wave_duration_cycles(self):
        """Returns how many cycles the waveform runs for."""
        return self.query_float("SOUR:WAVE:DUR:CYCL?")

    @wave_duration_cycles.setter
    def wave_duration_cycles(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write("SOUR:WAVE:DUR:CYCL INF")
            return
        check_range(value, 1e-3, 99999999900, "waveform duration", " cycles")
        self.write(f"SOUR:WAVE:DUR:CYCL {value}")

    @property
    def wave_duration_time(self):
        """Returns how long the waveform runs for, in seconds."""
        return self.query_float("SOUR:WAVE:DUR:TIME?")

    @wave_duration_time.setter
    def wave_duration_time(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write("SOUR:WAVE:DUR:TIME INF")
            return
        check_range(value, 100e-9, 999999.999, "waveform duration", " s")
        self.write(f"SOUR:WAVE:DUR:TIME {value}")

    @property
    def wave_armed(self):
        """Returns whether the waveform generator is armed and ready to start."""
        return self.query_boolean("SOUR:WAVE:ARM?")

    def arm_waveform(self):
        """Arm the waveform generator."""
        self.write("SOUR:WAVE:ARM")

    def start_waveform(self):
        """Start the armed waveform."""
        self.write("SOUR:WAVE:INIT")

    def abort_waveform(self):
        """Stop the waveform and disarm the generator."""
        self.write("SOUR:WAVE:ABOR")

    def set_phase_marker(self, enabled=True, level=180, line=None):
        """Emit a trigger pulse at a chosen phase of the waveform.

        Used to tell a lock-in or a nanovoltmeter where in the cycle it is.

        :param level: Phase at which to mark, 0 to 360 degrees.
        :param line: Trigger link line to output on, 1 to 6.
        """
        state = check_boolean(enabled, "phase marker")
        check_range(level, 0, 360, "phase marker level", " degrees")
        self.write(f"SOUR:WAVE:PMAR:STAT {int(state)}")
        self.write(f"SOUR:WAVE:PMAR:LEV {level}")
        if line is not None:
            if line not in TRIGGER_LINES:
                raise RangeError(
                    "The phase marker can only be output on trigger link lines "
                    f"{', '.join(str(n) for n in TRIGGER_LINES)}, but got "
                    f"{line}."
                )
            self.write(f"SOUR:WAVE:PMAR:OLIN {line}")

    # Averaging filter

    @property
    def filter_enabled(self):
        """Returns whether the averaging filter is on."""
        return self.query_boolean("SENS:AVER?")

    @filter_enabled.setter
    def filter_enabled(self, value):
        state = check_boolean(value, "filter")
        self.write(f"SENS:AVER {int(state)}")

    @property
    def filter_count(self):
        """Returns how many readings the averaging filter combines."""
        return self.query_integer("SENS:AVER:COUN?")

    @filter_count.setter
    def filter_count(self, value):
        count = check_integer_range(value, 2, 300, "filter count")
        self.write(f"SENS:AVER:COUN {count}")

    @property
    def filter_type(self):
        """Returns the averaging filter type: 'moving' or 'repeating'."""
        reply = self.query("SENS:AVER:TCON?").strip().upper()
        return "moving" if reply.startswith("MOV") else "repeating"

    @filter_type.setter
    def filter_type(self, value):
        code = check_choice(value, FILTER_TYPES, "filter type")
        self.write(f"SENS:AVER:TCON {code}")

    def enable_filter(self, count=10, filter_type="repeating"):
        """Turn on averaging with a given count and type, in one call."""
        self.filter_type = filter_type
        self.filter_count = count
        self.filter_enabled = True

    def disable_filter(self):
        """Turn the averaging filter off."""
        self.filter_enabled = False

    # Delta mode
    #
    # Delta reverses the current between a high and a low level and takes the
    # difference of the voltages the 2182A reads, which cancels thermoelectric
    # offsets. All three of these modes require a 2182A on the trigger link.

    def _require_nanovoltmeter(self, query, mode):
        """Check a 2182A is present before arming a mode that needs one."""
        if not self.query_boolean(query):
            raise RangeError(
                f"{mode} needs a Keithley 2182A connected to the 6221 over the "
                "trigger link, and none was detected. Check the RS-232 and "
                "trigger link cables between the two instruments."
            )

    @property
    def delta_high_current(self):
        """Returns the upper current level of the delta cycle, in amps."""
        return self.query_float("SOUR:DELT:HIGH?")

    @delta_high_current.setter
    def delta_high_current(self, value):
        check_range(value, 0, MAXIMUM_CURRENT, "delta high current", " A")
        self.write(f"SOUR:DELT:HIGH {value}")

    @property
    def delta_low_current(self):
        """Returns the lower current level of the delta cycle, in amps."""
        return self.query_float("SOUR:DELT:LOW?")

    @delta_low_current.setter
    def delta_low_current(self, value):
        check_range(value, -MAXIMUM_CURRENT, 0, "delta low current", " A")
        self.write(f"SOUR:DELT:LOW {value}")

    @property
    def delta_delay(self):
        """Returns the settling time between the current step and the reading.

        In seconds.
        """
        return self.query_float("SOUR:DELT:DEL?")

    @delta_delay.setter
    def delta_delay(self, value):
        check_range(value, 0, 9999.999, "delta delay", " s")
        self.write(f"SOUR:DELT:DEL {value}")

    @property
    def delta_count(self):
        """Returns how many delta cycles to run."""
        return self.query_integer("SOUR:DELT:COUN?")

    @delta_count.setter
    def delta_count(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write("SOUR:DELT:COUN INF")
            return
        count = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "delta count")
        self.write(f"SOUR:DELT:COUN {count}")

    @property
    def delta_compliance_abort(self):
        """Returns whether the delta run stops if the source goes into compliance."""
        return self.query_boolean("SOUR:DELT:CAB?")

    @delta_compliance_abort.setter
    def delta_compliance_abort(self, value):
        state = check_boolean(value, "compliance abort")
        self.write(f"SOUR:DELT:CAB {'ON' if state else 'OFF'}")

    def configure_delta(
        self, high, low=None, delay=0.002, count="infinite", compliance_abort=True
    ):
        """Set up a delta measurement.

        :param high: Upper current level, in amps.
        :param low: Lower current level. Defaults to -high, the symmetric
                    reversal that cancels thermoelectric offsets.
        :param delay: Settling time between step and reading, in seconds.
        :param count: How many cycles to run, or 'infinite'.
        """
        self._require_nanovoltmeter("SOUR:DELT:NVPR?", "Delta mode")
        self.delta_high_current = high
        self.delta_low_current = -abs(float(high)) if low is None else low
        self.delta_delay = delay
        self.delta_count = count
        self.delta_compliance_abort = compliance_abort

    def arm_delta(self):
        """Arm the delta measurement."""
        self.write("SOUR:DELT:ARM")

    def abort_delta(self):
        """Stop the delta measurement."""
        self.write("SOUR:SWE:ABOR")

    # Pulse delta mode

    @property
    def pulse_delta_high_current(self):
        """Returns the pulse current level, in amps."""
        return self.query_float("SOUR:PDEL:HIGH?")

    @pulse_delta_high_current.setter
    def pulse_delta_high_current(self, value):
        check_range(
            value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "pulse high current", " A"
        )
        self.write(f"SOUR:PDEL:HIGH {value}")

    @property
    def pulse_delta_low_current(self):
        """Returns the baseline current level between pulses, in amps."""
        return self.query_float("SOUR:PDEL:LOW?")

    @pulse_delta_low_current.setter
    def pulse_delta_low_current(self, value):
        check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, "pulse low current", " A")
        self.write(f"SOUR:PDEL:LOW {value}")

    @property
    def pulse_width(self):
        """Returns the pulse width, in seconds."""
        return self.query_float("SOUR:PDEL:WIDT?")

    @pulse_width.setter
    def pulse_width(self, value):
        check_range(value, 50e-6, 12e-3, "pulse width", " s")
        self.write(f"SOUR:PDEL:WIDT {value}")

    @property
    def pulse_measurement_delay(self):
        """Returns the delay from the start of the pulse to the reading, in seconds."""
        return self.query_float("SOUR:PDEL:SDEL?")

    @pulse_measurement_delay.setter
    def pulse_measurement_delay(self, value):
        check_range(value, 16e-6, 11.966e-3, "pulse measurement delay", " s")
        self.write(f"SOUR:PDEL:SDEL {value}")

    @property
    def pulse_interval(self):
        """Returns the pulse repetition period, in power line cycles."""
        return self.query_integer("SOUR:PDEL:INT?")

    @pulse_interval.setter
    def pulse_interval(self, value):
        interval = check_integer_range(
            value, 5, 999999, "pulse interval", " power line cycles"
        )
        self.write(f"SOUR:PDEL:INT {interval}")

    @property
    def pulse_count(self):
        """Returns how many pulses to deliver."""
        return self.query_integer("SOUR:PDEL:COUN?")

    @pulse_count.setter
    def pulse_count(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write("SOUR:PDEL:COUN INF")
            return
        count = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "pulse count")
        self.write(f"SOUR:PDEL:COUN {count}")

    def configure_pulse_delta(
        self,
        high,
        low=0.0,
        width=100e-6,
        measurement_delay=16e-6,
        interval=5,
        count=100,
        sweep=False,
    ):
        """Set up a pulse-delta measurement.

        Pulsing keeps the average power in the sample low, which is what makes
        this usable on anything that self-heats.

        :param high: Pulse current, in amps.
        :param low: Baseline current between pulses, in amps.
        :param width: Pulse width, in seconds.
        :param measurement_delay: Delay from pulse start to reading, in seconds.
        :param interval: Repetition period, in power line cycles.
        :param count: How many pulses, or 'infinite'.
        :param sweep: Whether the pulse amplitude sweeps rather than staying
                      fixed.
        """
        self._require_nanovoltmeter("SOUR:PDEL:NVPR?", "Pulse delta mode")
        if float(measurement_delay) > float(width):
            raise RangeError(
                f"The pulse measurement delay ({measurement_delay} s) must not "
                f"be longer than the pulse itself ({width} s), or the reading "
                "would be taken after the pulse has ended."
            )
        self.pulse_delta_high_current = high
        self.pulse_delta_low_current = low
        self.pulse_width = width
        self.pulse_measurement_delay = measurement_delay
        self.pulse_interval = interval
        self.pulse_count = count
        self.write(f"SOUR:PDEL:SWE {'ON' if check_boolean(sweep, 'sweep') else 'OFF'}")

    def arm_pulse_delta(self):
        """Arm the pulse-delta measurement."""
        self.write("SOUR:PDEL:ARM")

    def abort_pulse_delta(self):
        """Stop the pulse-delta measurement."""
        self.write("SOUR:SWE:ABOR")

    # Differential conductance

    def configure_differential_conductance(
        self, start, stop, step, delta, delay=0.002, compliance_abort=True
    ):
        """Set up a differential conductance sweep.

        The source steps through a staircase while dithering by +/- delta at
        each step, so dV/dI comes straight out of the instrument rather than
        from differentiating a measured curve afterwards.

        :param start: First current level, in amps.
        :param stop: Last current level, in amps.
        :param step: Staircase step size, in amps.
        :param delta: Dither amplitude at each step, in amps.
        :param delay: Settling time at each level, in seconds.
        """
        self._require_nanovoltmeter("SOUR:DCON:NVPR?", "Differential conductance")
        for value, name in (
            (start, "start current"),
            (stop, "stop current"),
            (step, "step size"),
            (delta, "delta"),
        ):
            check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, name, " A")
        if float(step) == 0:
            raise RangeError("The step size cannot be zero.")
        check_range(delay, 0, 9999.999, "delay", " s")

        self.write(f"SOUR:DCON:STAR {start}")
        self.write(f"SOUR:DCON:STOP {stop}")
        self.write(f"SOUR:DCON:STEP {step}")
        self.write(f"SOUR:DCON:DELT {delta}")
        self.write(f"SOUR:DCON:DEL {delay}")
        state = check_boolean(compliance_abort, "compliance abort")
        self.write(f"SOUR:DCON:CAB {'ON' if state else 'OFF'}")

    def arm_differential_conductance(self):
        """Arm the differential conductance sweep."""
        self.write("SOUR:DCON:ARM")

    def abort_differential_conductance(self):
        """Stop the differential conductance sweep."""
        self.write("SOUR:SWE:ABOR")

    # Sweeps

    def configure_sweep(
        self, start, stop, step, spacing="linear", delay=0.001, count=1
    ):
        """Set up a staircase current sweep.

        :param start: First current level, in amps.
        :param stop: Last current level, in amps.
        :param step: Step size, in amps.
        :param spacing: 'linear' or 'logarithmic'.
        :param delay: Settling time at each level, in seconds.
        :param count: How many times to repeat the sweep.
        """
        for value, name in (
            (start, "sweep start current"),
            (stop, "sweep stop current"),
            (step, "sweep step size"),
        ):
            check_range(value, -MAXIMUM_CURRENT, MAXIMUM_CURRENT, name, " A")
        if float(step) == 0:
            raise RangeError("The sweep step size cannot be zero.")
        code = check_choice(spacing, SWEEP_SPACINGS, "sweep spacing")
        check_range(delay, 0, 999999.999, "sweep delay", " s")
        repeats = check_integer_range(count, 1, MAXIMUM_BUFFER_POINTS, "sweep count")

        self.write(f"SOUR:CURR:STAR {start}")
        self.write(f"SOUR:CURR:STOP {stop}")
        self.write(f"SOUR:CURR:STEP {step}")
        self.write(f"SOUR:SWE:SPAC {code}")
        self.write(f"SOUR:DEL {delay}")
        self.write(f"SOUR:SWE:COUN {repeats}")
        self.write("SOUR:SWE:ARM")

    def abort_sweep(self):
        """Stop a running sweep."""
        self.write("SOUR:SWE:ABOR")

    # Running and reading

    def start(self):
        """Start whichever measurement is armed."""
        self.write("INIT:IMM")

    def abort(self):
        """Stop the running measurement."""
        self.write("SOUR:SWE:ABOR")

    @property
    def measurement_unit(self):
        """Returns the unit the delta modes report in: volts, ohms, watts or siemens."""
        reply = self.query("UNIT?").strip().upper()
        for name, code in DELTA_UNITS.items():
            if reply.startswith(code):
                return name
        return reply

    @measurement_unit.setter
    def measurement_unit(self, value):
        code = check_choice(value, DELTA_UNITS, "measurement unit")
        self.write(f"UNIT {code}")

    @property
    def buffer_size(self):
        """Returns how many readings the buffer will hold."""
        return self.query_integer("TRAC:POIN?")

    @buffer_size.setter
    def buffer_size(self, value):
        points = check_integer_range(
            value, 1, MAXIMUM_BUFFER_POINTS, "buffer size", " readings"
        )
        self.write(f"TRAC:POIN {points}")

    def read_buffer(self):
        """Returns everything stored in the buffer, as a list of floats."""
        return self.query_floats("TRAC:DATA?")

    def clear_buffer(self):
        """Discard the buffer contents."""
        self.write("TRAC:CLE")

    # Front panel

    @property
    def display_enabled(self):
        """Returns whether the front-panel display is on."""
        return self.query_boolean("DISP:ENAB?")

    @display_enabled.setter
    def display_enabled(self, value):
        state = check_boolean(value, "display")
        self.write(f"DISP:ENAB {int(state)}")

    @property
    def display_text(self):
        """Returns the message shown on the top line of the display."""
        return self.query("DISP:WIND1:TEXT:DATA?").strip().strip('"')

    @display_text.setter
    def display_text(self, value):
        text = str(value)
        if len(text) > 20:
            raise RangeError(
                f"Display text is at most 20 characters, but got {len(text)}."
            )
        self.write(f'DISP:WIND1:TEXT:DATA "{text}"')
        self.write("DISP:WIND1:TEXT:STAT 1")

    def clear_display_text(self):
        """Stop showing a message and return the display to readings."""
        self.write("DISP:WIND1:TEXT:STAT 0")

    def press_key(self, code):
        """Simulate a front-panel key press by its code."""
        key = check_integer_range(code, 1, 31, "front-panel key code")
        self.write(f"SYST:KEY {key}")

    @property
    def gpib_address(self):
        """Returns the GPIB address the instrument reports for itself."""
        return self.query_integer("SYST:COMM:GPIB:ADDR?")

    # Common procedures

    def safe_shutdown(self, steps=100, delay=0.01):
        """Stop any waveform, walk the current down to zero and switch off.

        Opening the output of a current source while it is driving forces the
        current to find another path, which is worth avoiding with anything
        delicate connected.
        """
        self.abort_waveform()
        if self.output:
            self.ramp_source_current(0.0, steps=steps, delay=delay)
        self.output = False

    def __repr__(self):
        return f"Keithley6221({self._transport!r})"
