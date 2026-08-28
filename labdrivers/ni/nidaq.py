"""Driver for National Instruments DAQ devices.

Built on NI's ``nidaqmx`` package, which needs the NI-DAQmx driver installed
separately from ni.com.

Every read and write opens a task, uses it and closes it again. That suits
occasional measurements taken between other operations. For continuous or
hardware-timed acquisition, take a task from :meth:`Nidaq.task` and drive
nidaqmx directly.
"""

import math
import statistics
import time

from ..core import (
    Settings,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import ConnectionFailure, RangeError

# NI's analog input terminal configurations.
TERMINAL_CONFIGURATIONS = (
    "default",
    "differential",
    "rse",
    "nrse",
    "pseudodifferential",
)

DEFAULT_DEVICE = "Dev1"


def _nidaqmx():
    """Import nidaqmx, with an error that says what to install if it is absent."""
    try:
        import nidaqmx
    except ImportError:
        raise ConnectionFailure(
            "The nidaqmx package is not installed, so NI DAQ devices cannot be "
            "reached. Install it with 'pip install nidaqmx'. It also needs the "
            "NI-DAQmx driver from ni.com, which is a separate download."
        )
    return nidaqmx


class Nidaq(Settings):
    """Interface to a National Instruments DAQ device.

        daq = Nidaq("Dev1")
        daq.write_voltage("ao0", 1.5)
        reading = daq.read_voltage("ai0")

    :param device: Device name as it appears in NI MAX, e.g. 'Dev1'.
    """

    device = None

    def __init__(self, device=DEFAULT_DEVICE):
        self.device = str(device)
        self._nidaqmx = _nidaqmx()
        # A DAQ analog output cannot be read back, so remember what was
        # last written to each one for ramp_voltage to start from.
        self._last_written = {}

    def _channel(self, channel):
        """Qualify a bare channel name with the device name."""
        name = str(channel).strip()
        return name if "/" in name else f"{self.device}/{name}"

    def task(self):
        """Return a new nidaqmx Task for this device, for advanced use.

        Use as a context manager::

            with daq.task() as task:
                task.ai_channels.add_ai_voltage_chan("Dev1/ai0")
                ...
        """
        return self._nidaqmx.Task()

    def _terminal_configuration(self, name):
        code = check_choice(name, TERMINAL_CONFIGURATIONS, "terminal configuration")
        constants = self._nidaqmx.constants.TerminalConfiguration
        return {
            "default": constants.DEFAULT,
            "differential": constants.DIFF,
            "rse": constants.RSE,
            "nrse": constants.NRSE,
            "pseudodifferential": constants.PSEUDO_DIFF,
        }[code]

    # Analog input

    def read_voltage(
        self,
        channel,
        minimum=-10.0,
        maximum=10.0,
        samples=1,
        terminal_configuration="default",
    ):
        """Read an analog input channel, in volts.

        :param channel: Channel name, e.g. 'ai0'.
        :param minimum: Smallest voltage expected, which sets the input range.
        :param maximum: Largest voltage expected.
        :param samples: How many samples to take. One returns a float. More
                        returns a list.
        :param terminal_configuration: How the input is wired: 'default',
                                       'differential', 'rse', 'nrse' or
                                       'pseudodifferential'.
        """
        count = check_integer_range(samples, 1, 1000000, "number of samples")
        if float(minimum) >= float(maximum):
            raise RangeError(
                f"The minimum voltage ({minimum} V) must be below the maximum "
                f"({maximum} V)."
            )
        with self.task() as task:
            task.ai_channels.add_ai_voltage_chan(
                self._channel(channel),
                min_val=float(minimum),
                max_val=float(maximum),
                terminal_config=self._terminal_configuration(terminal_configuration),
            )
            return task.read(number_of_samples_per_channel=count)

    def read_current(self, channel, minimum=-0.02, maximum=0.02, samples=1):
        """Read an analog input channel wired for current, in amps.

        Defaults span the usual 4-20 mA instrumentation range.
        """
        count = check_integer_range(samples, 1, 1000000, "number of samples")
        if float(minimum) >= float(maximum):
            raise RangeError(
                f"The minimum current ({minimum} A) must be below the maximum "
                f"({maximum} A)."
            )
        with self.task() as task:
            task.ai_channels.add_ai_current_chan(
                self._channel(channel),
                min_val=float(minimum),
                max_val=float(maximum),
            )
            return task.read(number_of_samples_per_channel=count)

    def read_channels(self, channels, minimum=-10.0, maximum=10.0):
        """Read several analog inputs in one task, sampled together.

        :param channels: A sequence of channel names.
        :return: A list of readings, in the order given.
        """
        names = [self._channel(name) for name in channels]
        if not names:
            raise RangeError("At least one channel is needed.")
        with self.task() as task:
            for name in names:
                task.ai_channels.add_ai_voltage_chan(
                    name, min_val=float(minimum), max_val=float(maximum)
                )
            return task.read()

    # Analog output

    def write_voltage(self, channel, voltage, minimum=-10.0, maximum=10.0):
        """Set an analog output channel, in volts.

        :param channel: Channel name, e.g. 'ao0'.
        :param voltage: Voltage to output.
        """
        check_range(voltage, minimum, maximum, "output voltage", " V")
        with self.task() as task:
            task.ao_channels.add_ao_voltage_chan(
                self._channel(channel),
                min_val=float(minimum),
                max_val=float(maximum),
            )
            task.write(float(voltage), auto_start=True)
        self._last_written[self._channel(channel)] = float(voltage)

    def write_current(self, channel, current, minimum=0.0, maximum=0.02):
        """Set an analog output channel wired for current, in amps."""
        check_range(current, minimum, maximum, "output current", " A")
        with self.task() as task:
            task.ao_channels.add_ao_current_chan(
                self._channel(channel),
                min_val=float(minimum),
                max_val=float(maximum),
            )
            task.write(float(current), auto_start=True)

    # Digital

    def read_digital(self, line):
        """Read one digital input line.

        :param line: Line name, e.g. 'port0/line0'.
        """
        with self.task() as task:
            task.di_channels.add_di_chan(self._channel(line))
            return bool(task.read())

    def write_digital(self, line, value):
        """Set one digital output line."""
        state = check_boolean(value, "digital output")
        with self.task() as task:
            task.do_channels.add_do_chan(self._channel(line))
            task.write(state, auto_start=True)

    # Counters

    def count_edges(self, counter, duration=1.0):
        """Count edges on a counter input for a fixed time.

        :param counter: Counter name, e.g. 'ctr0'.
        :param duration: Seconds to count for.
        :return: The number of edges counted.
        """
        check_range(duration, 0, 3600, "count duration", " s")
        with self.task() as task:
            task.ci_channels.add_ci_count_edges_chan(self._channel(counter))
            task.start()
            time.sleep(float(duration))
            count = task.read()
            task.stop()
        return count

    # Device

    def reset(self):
        """Reset the device, as NI MAX's right-click Reset does."""
        self._nidaqmx.system.Device(self.device).reset_device()

    def channel_names(self):
        """List the device's analog input, analog output and digital lines."""
        device = self._nidaqmx.system.Device(self.device)
        return {
            "analog_input": list(device.ai_physical_chans.channel_names),
            "analog_output": list(device.ao_physical_chans.channel_names),
            "digital_input": list(device.di_lines.channel_names),
            "digital_output": list(device.do_lines.channel_names),
        }

    @staticmethod
    def available_devices():
        """List the DAQ devices this machine can see."""
        system = _nidaqmx().system.System.local()
        return [device.name for device in system.devices]

    # Common procedures

    def ramp_voltage(self, channel, target, steps=100, delay=0.01, start=None):
        """Walk an analog output to a voltage instead of stepping to it.

        A DAQ output changes in one conversion, which puts a step through
        whatever it drives. This walks it there.

        :param channel: Channel name, e.g. 'ao0'.
        :param target: Voltage to finish at.
        :param steps: How many intermediate levels to pass through.
        :param delay: Seconds to wait at each step.
        :param start: Voltage to start from. A DAQ output cannot be read back,
                      so this defaults to the last value this driver wrote to
                      the channel, or zero if it has not written one.
        """
        check_range(target, -10.0, 10.0, "target voltage", " V")
        number = check_integer_range(steps, 1, 1000000, "number of steps")
        check_range(delay, 0, 3600, "step delay", " s")

        name = self._channel(channel)
        first = self._last_written.get(name, 0.0) if start is None else float(start)

        for step in range(1, number + 1):
            self.write_voltage(channel, first + (float(target) - first) * step / number)
            time.sleep(delay)

    def read_average(self, channel, samples=100, minimum=-10.0, maximum=10.0):
        """Average a burst of readings from one channel.

        :return: A tuple of (mean, standard error of the mean).
        """
        readings = self.read_voltage(
            channel, minimum=minimum, maximum=maximum, samples=samples
        )
        if not isinstance(readings, list):
            return float(readings), 0.0
        if len(readings) < 2:
            return float(readings[0]), 0.0
        return (
            statistics.fmean(readings),
            statistics.stdev(readings) / math.sqrt(len(readings)),
        )

    def __repr__(self):
        return f"Nidaq({self.device!r})"
