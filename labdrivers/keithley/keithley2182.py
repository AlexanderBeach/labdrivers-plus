"""Driver for the Keithley 2182 and 2182A nanovoltmeter.

The 2182 measures only: it has two voltage channels and no source of any kind.
Channel 1 reads up to 120 V, channel 2 up to 12 V, and channel 0 is the internal
temperature sensor. It is most often paired with a 6221 current source over the
trigger link, where the 6221 drives the current and the 2182 reads the voltage.

Commands and ranges are transcribed from the *Model 2182/2182A Nanovoltmeter
User's Manual*, Tables 2-3, 3-3 and the SCPI reference in the appendix.
"""

import math
import statistics
import time

from ..core import (
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import RangeError

# Full-scale reading limits per channel, in volts.
CHANNEL_VOLTAGE_LIMITS = {1: 120.0, 2: 12.0}

# Integration rate limits depend on the power line frequency.
NPLC_LIMITS = {50: (0.01, 50.0), 60: (0.01, 60.0)}

FUNCTIONS = {"voltage": "VOLTage", "temperature": "TEMPerature"}
FILTER_TYPES = {"moving": "MOVing", "repeating": "REPeat"}
TRIGGER_SOURCES = {
    "immediate": "IMM",
    "timer": "TIM",
    "manual": "MAN",
    "bus": "BUS",
    "external": "EXT",
}
THERMOCOUPLE_TYPES = ("J", "K", "T", "E", "R", "S", "B", "N")
TEMPERATURE_UNITS = {"celsius": "C", "fahrenheit": "F", "kelvin": "K"}
STATISTICS = {
    "mean": "MEAN",
    "standard deviation": "SDEViation",
    "maximum": "MAXimum",
    "minimum": "MINimum",
    "peak to peak": "PKPK",
}

MAXIMUM_BUFFER_POINTS = 1024


class Keithley2182(ScpiInstrument):
    """Interface to a Keithley 2182 or 2182A nanovoltmeter.

    meter = Keithley2182(gpib_address=7)
    meter.channel = 1
    meter.integration_time = 5
    meter.analog_filter = True
    voltage = meter.read()
    """

    IDENTIFIER = "2182"

    line_frequency = None

    def __init__(self, *args, line_frequency=60, **kwargs):
        super().__init__(*args, **kwargs)
        if int(line_frequency) not in NPLC_LIMITS:
            raise RangeError(
                "The power line frequency can be 50 or 60 Hz, but got "
                f"{line_frequency}."
            )
        self.line_frequency = int(line_frequency)

    def _check_channel(self, channel, allow_internal=True):
        """Validate a channel number, optionally allowing the internal sensor."""
        lowest = 0 if allow_internal else 1
        number = check_integer_range(channel, lowest, 2, "measurement channel")
        return number

    def _channel_path(self, channel):
        """SCPI path fragment for a channel. Channel 1 is the default path."""
        return "" if int(channel) == 1 else f":CHAN{int(channel)}"

    # Function and channel

    @property
    def function(self):
        """Returns what is being measured: 'voltage' or 'temperature'."""
        reply = self.query(":SENS:FUNC?").strip().strip('"').strip("'").upper()
        return "temperature" if reply.startswith("TEMP") else "voltage"

    @function.setter
    def function(self, value):
        code = check_choice(value, FUNCTIONS, "measurement function")
        self.write(f":SENS:FUNC '{code}'")

    @property
    def channel(self):
        """Returns which input is measured: 1, 2, or 0 for the internal sensor."""
        return self.query_integer(":SENS:CHAN?")

    @channel.setter
    def channel(self, value):
        number = self._check_channel(value)
        self.write(f":SENS:CHAN {number}")

    # Range

    def voltage_range(self, channel=1):
        """Measurement range of one channel, in volts."""
        number = self._check_channel(channel, allow_internal=False)
        return self.query_float(f":SENS:VOLT{self._channel_path(number)}:RANG?")

    def set_voltage_range(self, value, channel=1):
        """Set one channel's range, which turns its autorange off.

        Channel 1 reads to 120 V and channel 2 to 12 V.
        """
        number = self._check_channel(channel, allow_internal=False)
        limit = CHANNEL_VOLTAGE_LIMITS[number]
        check_range(value, -limit, limit, f"channel {number} voltage range", " V")
        self.write(f":SENS:VOLT{self._channel_path(number)}:RANG {value}")

    def voltage_auto_range(self, channel=1):
        """Whether one channel picks its own range."""
        number = self._check_channel(channel, allow_internal=False)
        return self.query_boolean(f":SENS:VOLT{self._channel_path(number)}:RANG:AUTO?")

    def set_voltage_auto_range(self, value, channel=1):
        """Turn autoranging on or off for one channel."""
        number = self._check_channel(channel, allow_internal=False)
        state = check_boolean(value, "voltage autorange")
        self.write(f":SENS:VOLT{self._channel_path(number)}:RANG:AUTO {int(state)}")

    # Integration rate

    @property
    def integration_time(self):
        """Returns the integration time, in power line cycles."""
        return self.query_float(":SENS:VOLT:NPLC?")

    @integration_time.setter
    def integration_time(self, value):
        lowest, highest = NPLC_LIMITS[self.line_frequency]
        check_range(
            value,
            lowest,
            highest,
            f"integration time at {self.line_frequency} Hz",
            " power line cycles",
        )
        self.write(f":SENS:VOLT:NPLC {value}")

    @property
    def aperture(self):
        """Returns the integration time expressed in seconds instead of line cycles."""
        return self.query_float(":SENS:VOLT:APER?")

    @aperture.setter
    def aperture(self, value):
        shortest = 166.67e-6 if self.line_frequency == 60 else 200e-6
        check_range(value, shortest, 1.0, "aperture", " s")
        self.write(f":SENS:VOLT:APER {value}")

    # Filtering
    #
    # The 2182 has two filters: an analog low-pass ahead of the A/D, and a
    # digital averaging filter after it. Nanovolt work usually wants both.

    def analog_filter(self, channel=1):
        """Whether the analog low-pass filter is on for one channel."""
        number = self._check_channel(channel, allow_internal=False)
        return self.query_boolean(f":SENS:VOLT{self._channel_path(number)}:LPAS?")

    def set_analog_filter(self, value, channel=1):
        """Turn the analog low-pass filter on or off for one channel."""
        number = self._check_channel(channel, allow_internal=False)
        state = check_boolean(value, "analog filter")
        self.write(f":SENS:VOLT{self._channel_path(number)}:LPAS {int(state)}")

    def digital_filter(self, channel=1):
        """Whether the digital averaging filter is on for one channel."""
        number = self._check_channel(channel, allow_internal=False)
        return self.query_boolean(f":SENS:VOLT{self._channel_path(number)}:DFIL:STAT?")

    def set_digital_filter(
        self, enabled=True, count=10, filter_type="moving", window=0.01, channel=1
    ):
        """Configure and enable the digital averaging filter.

        :param count: How many readings to average, 1 to 100.
        :param filter_type: 'moving' or 'repeating'.
        :param window: Noise window as a percentage of range, 0 to 10. Readings
                       outside the window restart the filter, so a step change
                       is not smeared across the average.
        :param channel: Which channel the setting applies to.
        """
        number = self._check_channel(channel, allow_internal=False)
        path = f":SENS:VOLT{self._channel_path(number)}:DFIL"
        readings = check_integer_range(count, 1, 100, "filter count")
        code = check_choice(filter_type, FILTER_TYPES, "filter type")
        check_range(window, 0, 10, "filter window", " percent")
        state = check_boolean(enabled, "digital filter")

        self.write(f"{path}:COUN {readings}")
        self.write(f"{path}:TCON {code}")
        self.write(f"{path}:WIND {window}")
        self.write(f"{path}:STAT {int(state)}")

    # Relative (nulling)

    def relative(self, channel=1):
        """Whether the relative offset is applied on one channel."""
        number = self._check_channel(channel, allow_internal=False)
        return self.query_boolean(f":SENS:VOLT{self._channel_path(number)}:REF:STAT?")

    def set_relative(self, value, channel=1):
        """Set the relative offset subtracted from readings, in volts.

        Channel 1 accepts -120 to 120 V, channel 2 -12 to 12 V.
        """
        number = self._check_channel(channel, allow_internal=False)
        limit = CHANNEL_VOLTAGE_LIMITS[number]
        check_range(value, -limit, limit, f"channel {number} relative value", " V")
        self.write(f":SENS:VOLT{self._channel_path(number)}:REF {value}")
        self.write(f":SENS:VOLT{self._channel_path(number)}:REF:STAT 1")

    def acquire_relative(self, channel=1):
        """Take the present reading as the relative offset.

        This is how thermal EMFs are nulled: short the input, acquire, and
        every later reading has that offset removed.
        """
        number = self._check_channel(channel, allow_internal=False)
        self.write(f":SENS:VOLT{self._channel_path(number)}:REF:ACQ")
        self.write(f":SENS:VOLT{self._channel_path(number)}:REF:STAT 1")

    def clear_relative(self, channel=1):
        """Stop subtracting the relative offset."""
        number = self._check_channel(channel, allow_internal=False)
        self.write(f":SENS:VOLT{self._channel_path(number)}:REF:STAT 0")

    # Ratio and delta

    @property
    def ratio(self):
        """Returns whether the instrument reports channel 1 divided by channel 2."""
        return self.query_boolean(":SENS:VOLT:RAT?")

    @ratio.setter
    def ratio(self, value):
        state = check_boolean(value, "ratio")
        self.write(f":SENS:VOLT:RAT {int(state)}")

    @property
    def delta(self):
        """Returns whether the instrument reports the delta of the two channels."""
        return self.query_boolean(":SENS:VOLT:DELT?")

    @delta.setter
    def delta(self, value):
        state = check_boolean(value, "delta")
        self.write(f":SENS:VOLT:DELT {int(state)}")

    # Temperature

    @property
    def temperature_unit(self):
        """Returns the units temperature is reported in.

        One of celsius, fahrenheit or kelvin.
        """
        reply = self.query(":UNIT:TEMP?").strip().upper()
        for name, code in TEMPERATURE_UNITS.items():
            if reply.startswith(code):
                return name
        return reply

    @temperature_unit.setter
    def temperature_unit(self, value):
        code = check_choice(value, TEMPERATURE_UNITS, "temperature unit")
        self.write(f":UNIT:TEMP {code}")

    @property
    def thermocouple(self):
        """Returns which thermocouple type is fitted."""
        return self.query(":SENS:TEMP:TC?").strip().upper()

    @thermocouple.setter
    def thermocouple(self, value):
        letter = str(value).strip().upper()
        if letter not in THERMOCOUPLE_TYPES:
            raise RangeError(
                f"The thermocouple type can be {', '.join(THERMOCOUPLE_TYPES)}, "
                f"but got {value!r}."
            )
        self.write(f":SENS:TEMP:TC {letter}")

    @property
    def reference_junction_temperature(self):
        """Returns the simulated reference junction temperature, in degrees Celsius."""
        return self.query_float(":SENS:TEMP:RJUN1:SIM?")

    @reference_junction_temperature.setter
    def reference_junction_temperature(self, value):
        check_range(value, 0, 60, "reference junction temperature", " C")
        self.write(f":SENS:TEMP:RJUN1:SIM {value}")

    # Taking readings

    def read(self):
        """Trigger a fresh reading and return it."""
        return self.query_float(":READ?")

    def fetch(self):
        """Returns the last reading again, without triggering a new one."""
        return self.query_float(":FETC?")

    def latest(self):
        """Returns the most recent reading, whenever it was taken."""
        return self.query_float(":SENS:DATA:LAT?")

    def fresh(self):
        """Returns the first reading taken after this call.

        Blocks until one is available, which is what separates it from
        latest(), since that will hand back the same reading twice.
        """
        return self.query_float(":SENS:DATA:FRES?")

    def initiate(self):
        """Start the configured measurement."""
        self.write(":INIT")

    def abort(self):
        """Stop the measurement and return to idle."""
        self.write(":ABOR")

    @property
    def continuous_initiation(self):
        """Returns whether the instrument re-arms itself after each measurement."""
        return self.query_boolean(":INIT:CONT?")

    @continuous_initiation.setter
    def continuous_initiation(self, value):
        state = check_boolean(value, "continuous initiation")
        self.write(f":INIT:CONT {int(state)}")

    # Triggering

    @property
    def trigger_source(self):
        """Returns what triggers a reading."""
        reply = self.query(":TRIG:SOUR?").strip().upper()
        for name, code in TRIGGER_SOURCES.items():
            if reply.startswith(code):
                return name
        return reply

    @trigger_source.setter
    def trigger_source(self, value):
        code = check_choice(value, TRIGGER_SOURCES, "trigger source")
        self.write(f":TRIG:SOUR {code}")

    @property
    def trigger_count(self):
        """Returns how many readings one trigger sequence takes."""
        return self.query_integer(":TRIG:COUN?")

    @trigger_count.setter
    def trigger_count(self, value):
        if str(value).strip().lower() in ("inf", "infinite"):
            self.write(":TRIG:COUN INF")
            return
        count = check_integer_range(value, 1, 9999, "trigger count")
        self.write(f":TRIG:COUN {count}")

    @property
    def trigger_delay(self):
        """Returns the delay between the trigger and the reading, in seconds."""
        return self.query_float(":TRIG:DEL?")

    @trigger_delay.setter
    def trigger_delay(self, value):
        check_range(value, 0, 999999.999, "trigger delay", " s")
        self.write(f":TRIG:DEL {value}")

    @property
    def trigger_auto_delay(self):
        """Returns whether the instrument chooses its own trigger delay."""
        return self.query_boolean(":TRIG:DEL:AUTO?")

    @trigger_auto_delay.setter
    def trigger_auto_delay(self, value):
        state = check_boolean(value, "auto trigger delay")
        self.write(f":TRIG:DEL:AUTO {int(state)}")

    @property
    def trigger_timer(self):
        """Returns the interval of the timer trigger source, in seconds."""
        return self.query_float(":TRIG:TIM?")

    @trigger_timer.setter
    def trigger_timer(self, value):
        check_range(value, 0, 999999.999, "trigger timer interval", " s")
        self.write(f":TRIG:TIM {value}")

    @property
    def sample_count(self):
        """Returns how many readings each trigger produces."""
        return self.query_integer(":SAMP:COUN?")

    @sample_count.setter
    def sample_count(self, value):
        count = check_integer_range(value, 1, MAXIMUM_BUFFER_POINTS, "sample count")
        self.write(f":SAMP:COUN {count}")

    # Buffer

    @property
    def buffer_size(self):
        """Returns how many readings the buffer will hold."""
        return self.query_integer(":TRAC:POIN?")

    @buffer_size.setter
    def buffer_size(self, value):
        points = check_integer_range(
            value, 2, MAXIMUM_BUFFER_POINTS, "buffer size", " readings"
        )
        self.write(f":TRAC:POIN {points}")

    def read_buffer(self):
        """Returns everything stored in the buffer, as a list of floats."""
        return self.query_floats(":TRAC:DATA?")

    def clear_buffer(self):
        """Discard the buffer contents."""
        self.write(":TRAC:CLE")

    def start_buffer(self, size=None):
        """Clear the buffer, size it, and arm it to fill."""
        self.clear_buffer()
        if size is not None:
            self.buffer_size = size
        self.write(":TRAC:FEED:CONT NEXT")

    def statistic(self, name):
        """Return a statistic over the readings in the buffer."""
        code = check_choice(name, STATISTICS, "statistic")
        self.write(f":CALC2:FORM {code}")
        self.write(":CALC2:STAT ON")
        return self.query_float(":CALC2:DATA?")

    # System and front panel

    @property
    def auto_zero(self):
        """Returns whether the instrument re-zeros its A/D before each reading."""
        return self.query_boolean(":SYST:AZER:STAT?")

    @auto_zero.setter
    def auto_zero(self, value):
        state = check_boolean(value, "auto zero")
        self.write(f":SYST:AZER:STAT {int(state)}")

    @property
    def front_autozero(self):
        """Returns whether front-end auto zero is on.

        Turning this off roughly doubles the reading rate, at the cost of drift.
        """
        return self.query_boolean(":SYST:FAZ:STAT?")

    @front_autozero.setter
    def front_autozero(self, value):
        state = check_boolean(value, "front autozero")
        self.write(f":SYST:FAZ:STAT {int(state)}")

    @property
    def line_synchronization(self):
        """Returns whether readings are synchronized to the power line."""
        return self.query_boolean(":SYST:LSYN:STAT?")

    @line_synchronization.setter
    def line_synchronization(self, value):
        state = check_boolean(value, "line synchronization")
        self.write(f":SYST:LSYN:STAT {int(state)}")

    def preset(self):
        """Put the instrument back to its SYSTem:PRESet defaults."""
        self.write(":SYST:PRES")

    @property
    def display_enabled(self):
        """Returns whether the front-panel display is on."""
        return self.query_boolean(":DISP:ENAB?")

    @display_enabled.setter
    def display_enabled(self, value):
        state = check_boolean(value, "display")
        self.write(f":DISP:ENAB {int(state)}")

    @property
    def display_text(self):
        """Returns the message shown on the display."""
        return self.query(":DISP:TEXT:DATA?").strip().strip('"')

    @display_text.setter
    def display_text(self, value):
        text = str(value)
        if len(text) > 12:
            raise RangeError(
                f"Display text is at most 12 characters, but got {len(text)}."
            )
        self.write(f':DISP:TEXT:DATA "{text}"')
        self.write(":DISP:TEXT:STAT 1")

    def clear_display_text(self):
        """Stop showing a message and return the display to readings."""
        self.write(":DISP:TEXT:STAT 0")

    def go_to_local(self):
        """Hand the instrument back to front-panel control."""
        self.write(":SYST:LOC")

    def go_to_remote(self):
        """Put the instrument under remote control."""
        self.write(":SYST:REM")

    # Common procedures

    def read_average(self, count=10, settle=0.0):
        """Take several readings and report the mean and its uncertainty.

        Nanovolt measurements are almost always averaged, and the scatter is
        worth having: it says whether the number is limited by noise or by
        something that is drifting.

        :param count: How many readings to take.
        :param settle: Seconds to wait between readings.
        :return: A tuple of (mean, standard error of the mean). The error is
                 zero for a single reading.
        """
        number = check_integer_range(count, 1, 100000, "number of readings")
        check_range(settle, 0, 3600, "settling time", " s")

        readings = []
        for index in range(number):
            if settle and index:
                time.sleep(settle)
            readings.append(self.read())

        average = statistics.fmean(readings)
        if number < 2:
            return average, 0.0
        return average, statistics.stdev(readings) / math.sqrt(number)

    def __repr__(self):
        return f"Keithley2182({self._transport!r})"
