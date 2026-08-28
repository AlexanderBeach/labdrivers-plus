"""Driver for Keysight InfiniiVision X-Series oscilloscopes.

Covers the 1000, 2000, 3000, 4000 and 6000 X-Series, whose command set is the
same across the range. They differ in bandwidth, channel count and memory. The
channel count is read from ``*IDN?`` where the model name gives it away, and
can be set explicitly for anything unusual.

Reading a trace off a scope means more than asking for the samples. The
instrument sends raw integers, and the preamble that goes with them says how to
turn those into volts and seconds. :meth:`InfiniiVision.read_waveform` does
both and hands back real units.

Commands and ranges are transcribed from the *InfiniiVision 3000 X-Series
Oscilloscopes Programmer's Guide*.
"""

import re

from ..core import (
    ScpiInstrument,
    check_boolean,
    check_choice,
    check_integer_range,
    check_range,
)
from ..core.errors import InstrumentError, RangeError

DEFAULT_CHANNEL_COUNT = 4

# Preamble fields, in the order :WAVeform:PREamble? returns them.
PREAMBLE_FIELDS = (
    "format",
    "type",
    "points",
    "count",
    "x_increment",
    "x_origin",
    "x_reference",
    "y_increment",
    "y_origin",
    "y_reference",
)

# Values the preamble reports for the transfer format and acquisition type.
PREAMBLE_FORMATS = {0: "byte", 1: "word", 2: "ascii"}
PREAMBLE_TYPES = {0: "normal", 1: "peak", 2: "average", 3: "high resolution"}

WAVEFORM_FORMATS = {"byte": "BYTE", "word": "WORD", "ascii": "ASCii"}

COUPLINGS = {"ac": "AC", "dc": "DC"}
IMPEDANCES = {"1m": "ONEMeg", "50": "FIFTy"}
ACQUISITION_TYPES = {
    "normal": "NORMal",
    "average": "AVERage",
    "high resolution": "HRESolution",
    "peak": "PEAK",
}
TIMEBASE_MODES = {"main": "MAIN", "window": "WINDow", "xy": "XY", "roll": "ROLL"}
TRIGGER_SWEEPS = {"auto": "AUTO", "normal": "NORMal"}
TRIGGER_SLOPES = {
    "negative": "NEGative",
    "positive": "POSitive",
    "either": "EITHer",
    "alternate": "ALTernate",
}

# Measurements the scope can make on the displayed trace.
MEASUREMENTS = {
    "amplitude": "VAMPlitude",
    "average": "VAVerage",
    "duty cycle": "DUTYcycle",
    "fall time": "FALLtime",
    "frequency": "FREQuency",
    "maximum": "VMAX",
    "minimum": "VMIN",
    "peak to peak": "VPP",
    "period": "PERiod",
    "rise time": "RISetime",
    "rms": "VRMS",
    "pulse width": "PWIDth",
}

# The scope reports a measurement it could not make as this value rather than
# raising, so a reading close to it means "no signal", not 9.9e37 volts.
MEASUREMENT_FAILED = 9.9e37


def parse_ascii_block(reply):
    """Returns the numbers in an ASCii waveform reply.

    Some models wrap even the ASCii format in the definite-length block that
    the binary formats use, so a reply can begin #800000060 before the first
    value. Stripping it when it is there costs nothing when it is not.

    :raises InstrumentError: If the reply holds something other than numbers.
    """
    text = reply.strip()
    if text.startswith("#") and len(text) > 2 and text[1].isdigit():
        digits = int(text[1])
        text = text[2 + digits :]
    try:
        return [float(piece) for piece in text.split(",") if piece.strip()]
    except ValueError:
        raise InstrumentError(
            f"Expected comma-separated numbers from the waveform, but got "
            f"{reply[:60]!r}."
        )


class InfiniiVision(ScpiInstrument):
    """Interface to a Keysight InfiniiVision X-Series oscilloscope.

        scope = InfiniiVision(resource_name="USB0::0x2A8D::0x1797::...::INSTR")
        scope.set_channel(1, scale=0.5, coupling="dc")
        scope.set_edge_trigger(source=1, level=0.2)
        scope.single()
        times, volts = scope.read_waveform(1)

    :param channels: How many input channels this scope has. Worked out from
                     *IDN? when not given.
    """

    IDENTIFIER = "MSO-X"

    def __init__(self, *args, channels=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.channel_count = (
            self._detect_channel_count() if channels is None else channels
        )

    def _detect_channel_count(self):
        """Work out the channel count from the model name.

        An InfiniiVision model number carries the channel count as its last
        digit: an MSO-X 3054A has four channels, an MSO-X 3012T has two. The
        prefix is sometimes written with a hyphen and a space, so the model
        number is not always its own word in the identification string.
        """
        try:
            identity = self.identify().upper()
        except Exception:
            return DEFAULT_CHANNEL_COUNT
        match = re.search(r"[MD]SO-?X\s*(\d{4})", identity)
        if not match:
            return DEFAULT_CHANNEL_COUNT
        channels = int(match.group(1)[-1])
        return channels if channels in (2, 4) else DEFAULT_CHANNEL_COUNT

    @property
    def channel_count(self):
        """Returns how many input channels this scope has."""
        return self._channel_count

    @channel_count.setter
    def channel_count(self, value):
        self._channel_count = check_integer_range(value, 1, 4, "channel count")

    def _check_channel(self, channel):
        return check_integer_range(channel, 1, self._channel_count, "input channel")

    # Run control

    def run(self):
        """Acquire continuously."""
        self.write(":RUN")

    def stop(self):
        """Stop acquiring."""
        self.write(":STOP")

    def single(self):
        """Arm for one acquisition and stop when it triggers."""
        self.write(":SINGle")

    def digitize(self, channel=None):
        """Acquire once and stop, leaving the data ready to read.

        Unlike single(), this blocks until the acquisition is complete, so a
        read that follows it is guaranteed to see the new trace rather than
        the previous one.
        """
        if channel is None:
            self.write(":DIGitize")
        else:
            self.write(f":DIGitize CHANnel{self._check_channel(channel)}")

    def autoscale(self):
        """Let the scope choose its own vertical, timebase and trigger."""
        self.write(":AUToscale")

    def clear_display(self):
        """Clear the display and any accumulated persistence."""
        self.write(":CDISplay")

    # Vertical

    def channel_scale(self, channel):
        """Vertical scale of one channel, in volts per division."""
        return self.query_float(f":CHANnel{self._check_channel(channel)}:SCALe?")

    def set_channel_scale(self, channel, volts_per_division):
        """Set one channel's vertical scale, in volts per division."""
        number = self._check_channel(channel)
        # The scale a channel can reach moves with what is plugged into it, so
        # a 10x probe puts 20 V/div well inside the range while a bound fixed
        # at the 1:1 figure turns an ordinary setting away.
        attenuation = self.channel_probe(number)
        check_range(
            volts_per_division,
            1e-3 * attenuation,
            10 * attenuation,
            "vertical scale",
            " V/div",
        )
        self.write(f":CHANnel{number}:SCALe {volts_per_division}")

    def channel_offset(self, channel):
        """Vertical offset of one channel, in volts."""
        return self.query_float(f":CHANnel{self._check_channel(channel)}:OFFSet?")

    def set_channel_offset(self, channel, volts):
        """Set one channel's vertical offset, in volts."""
        number = self._check_channel(channel)
        self.write(f":CHANnel{number}:OFFSet {volts}")

    def channel_coupling(self, channel):
        """Input coupling of one channel: 'ac' or 'dc'."""
        reply = self.query(f":CHANnel{self._check_channel(channel)}:COUPling?")
        return "ac" if reply.strip().upper().startswith("AC") else "dc"

    def set_channel_coupling(self, channel, coupling):
        """Set one channel's input coupling."""
        number = self._check_channel(channel)
        code = check_choice(coupling, COUPLINGS, "input coupling")
        self.write(f":CHANnel{number}:COUPling {code}")

    def channel_impedance(self, channel):
        """Input impedance of one channel: '1m' or '50'."""
        reply = self.query(f":CHANnel{self._check_channel(channel)}:IMPedance?")
        return "50" if reply.strip().upper().startswith("FIFT") else "1m"

    def set_channel_impedance(self, channel, impedance):
        """Set one channel's input impedance.

        Choosing 50 ohms puts a terminator across the input, so it must match
        what is actually connected or the signal will be halved or reflected.
        """
        number = self._check_channel(channel)
        code = check_choice(impedance, IMPEDANCES, "input impedance")
        self.write(f":CHANnel{number}:IMPedance {code}")

    def channel_probe(self, channel):
        """Probe attenuation of one channel, as a ratio."""
        return self.query_float(f":CHANnel{self._check_channel(channel)}:PROBe?")

    def set_channel_probe(self, channel, attenuation):
        """Set one channel's probe attenuation, e.g. 10 for a 10x probe."""
        number = self._check_channel(channel)
        check_range(attenuation, 0.1, 10000, "probe attenuation", "x")
        self.write(f":CHANnel{number}:PROBe {attenuation}")

    def channel_enabled(self, channel):
        """Whether one channel is displayed."""
        return self.query_boolean(f":CHANnel{self._check_channel(channel)}:DISPlay?")

    def set_channel_enabled(self, channel, enabled=True):
        """Show or hide one channel."""
        number = self._check_channel(channel)
        state = check_boolean(enabled, "channel display")
        self.write(f":CHANnel{number}:DISPlay {int(state)}")

    def set_channel_bandwidth_limit(self, channel, enabled=True):
        """Turn one channel's bandwidth limit on or off.

        The limit is around 20 MHz and is the quickest way to quieten a trace
        when the signal of interest is slow.
        """
        number = self._check_channel(channel)
        state = check_boolean(enabled, "bandwidth limit")
        self.write(f":CHANnel{number}:BWLimit {int(state)}")

    def set_channel_inverted(self, channel, inverted=True):
        """Invert one channel's displayed trace."""
        number = self._check_channel(channel)
        state = check_boolean(inverted, "channel inversion")
        self.write(f":CHANnel{number}:INVert {int(state)}")

    def set_channel(
        self,
        channel,
        scale=None,
        offset=None,
        coupling=None,
        impedance=None,
        probe=None,
        enabled=True,
    ):
        """Configure one channel in a single call."""
        number = self._check_channel(channel)
        if probe is not None:
            self.set_channel_probe(number, probe)
        if scale is not None:
            self.set_channel_scale(number, scale)
        if offset is not None:
            self.set_channel_offset(number, offset)
        if coupling is not None:
            self.set_channel_coupling(number, coupling)
        if impedance is not None:
            self.set_channel_impedance(number, impedance)
        self.set_channel_enabled(number, enabled)

    # Horizontal

    @property
    def timebase_scale(self):
        """Returns the timebase, in seconds per division."""
        return self.query_float(":TIMebase:SCALe?")

    @timebase_scale.setter
    def timebase_scale(self, seconds_per_division):
        check_range(seconds_per_division, 1e-12, 500, "timebase", " s/div")
        self.write(f":TIMebase:SCALe {seconds_per_division}")

    @property
    def timebase_position(self):
        """Returns the horizontal position of the trigger, in seconds."""
        return self.query_float(":TIMebase:POSition?")

    @timebase_position.setter
    def timebase_position(self, seconds):
        self.write(f":TIMebase:POSition {seconds}")

    @property
    def timebase_mode(self):
        """Returns the timebase mode: 'main', 'window', 'xy' or 'roll'."""
        reply = self.query(":TIMebase:MODE?").strip().upper()
        for name, code in TIMEBASE_MODES.items():
            if reply.startswith(code.upper()[:4]):
                return name
        return reply.lower()

    @timebase_mode.setter
    def timebase_mode(self, value):
        code = check_choice(value, TIMEBASE_MODES, "timebase mode")
        self.write(f":TIMebase:MODE {code}")

    # Trigger

    def set_edge_trigger(self, source=1, level=0.0, slope="positive", sweep="auto"):
        """Set up an edge trigger.

        :param source: Channel number, or 'external' or 'line'.
        :param level: Trigger level, in volts.
        :param slope: 'positive', 'negative', 'either' or 'alternate'.
        :param sweep: 'auto' free-runs when nothing triggers, and 'normal' waits.
        """
        if isinstance(source, str):
            word = source.strip().lower()
            if word in ("external", "ext"):
                name = "EXTernal"
            elif word == "line":
                name = "LINE"
            else:
                raise RangeError(
                    "The trigger source can be a channel number, 'external' "
                    f"or 'line', but got {source!r}."
                )
        else:
            name = f"CHANnel{self._check_channel(source)}"

        slope_code = check_choice(slope, TRIGGER_SLOPES, "trigger slope")
        sweep_code = check_choice(sweep, TRIGGER_SWEEPS, "trigger sweep")

        self.write(":TRIGger:MODE EDGE")
        self.write(f":TRIGger:EDGE:SOURce {name}")
        self.write(f":TRIGger:EDGE:LEVel {level}")
        self.write(f":TRIGger:EDGE:SLOPe {slope_code}")
        self.write(f":TRIGger:SWEep {sweep_code}")

    @property
    def trigger_level(self):
        """Returns the edge trigger level, in volts."""
        return self.query_float(":TRIGger:EDGE:LEVel?")

    @trigger_level.setter
    def trigger_level(self, volts):
        self.write(f":TRIGger:EDGE:LEVel {volts}")

    @property
    def trigger_sweep(self):
        """Returns whether the trigger is 'auto' or 'normal'."""
        reply = self.query(":TRIGger:SWEep?").strip().upper()
        return "normal" if reply.startswith("NORM") else "auto"

    @trigger_sweep.setter
    def trigger_sweep(self, value):
        code = check_choice(value, TRIGGER_SWEEPS, "trigger sweep")
        self.write(f":TRIGger:SWEep {code}")

    def triggered(self):
        """Whether the scope has triggered since this was last asked.

        Reading the trigger event register clears it, so a second call
        immediately afterwards reports False.
        """
        return bool(self.query_integer(":TER?"))

    def force_trigger(self):
        """Trigger once regardless of the input."""
        self.write(":TRIGger:FORCe")

    # Acquisition

    @property
    def acquisition_type(self):
        """Returns how samples are combined: 'normal', 'average', 'high resolution'
        or 'peak'."""
        reply = self.query(":ACQuire:TYPE?").strip().upper()
        for name, code in ACQUISITION_TYPES.items():
            if reply.startswith(code.upper()[:4]):
                return name
        return reply.lower()

    @acquisition_type.setter
    def acquisition_type(self, value):
        code = check_choice(value, ACQUISITION_TYPES, "acquisition type")
        self.write(f":ACQuire:TYPE {code}")

    @property
    def average_count(self):
        """Returns how many acquisitions are averaged together."""
        return self.query_integer(":ACQuire:COUNt?")

    @average_count.setter
    def average_count(self, value):
        count = check_integer_range(value, 2, 65536, "average count")
        self.write(f":ACQuire:COUNt {count}")

    @property
    def sample_rate(self):
        """Returns the sample rate of the last acquisition, in samples per second."""
        return self.query_float(":ACQuire:SRATe?")

    @property
    def acquisition_points(self):
        """Returns how many points the last acquisition captured."""
        return self.query_integer(":ACQuire:POINts?")

    def average(self, count=16):
        """Average a number of acquisitions together.

        The quickest way to pull a small repetitive signal out of the noise,
        provided the signal is stable and the trigger is reliable.
        """
        self.acquisition_type = "average"
        self.average_count = count

    # Waveform transfer

    def preamble(self):
        """Read the waveform preamble as a dictionary.

        The preamble is what makes the raw samples meaningful: it carries the
        scale and offset needed to turn them into volts and seconds.
        """
        values = self.query_floats(":WAVeform:PREamble?")
        if len(values) != len(PREAMBLE_FIELDS):
            raise InstrumentError(
                f"Expected {len(PREAMBLE_FIELDS)} preamble fields, got "
                f"{len(values)}.",
                instrument=self,
            )
        preamble = dict(zip(PREAMBLE_FIELDS, values))
        preamble["format"] = PREAMBLE_FORMATS.get(
            int(preamble["format"]), preamble["format"]
        )
        preamble["type"] = PREAMBLE_TYPES.get(int(preamble["type"]), preamble["type"])
        preamble["points"] = int(preamble["points"])
        preamble["count"] = int(preamble["count"])
        return preamble

    def read_waveform(self, channel=1, points=None, waveform_format="word"):
        """Read one channel's trace, converted into seconds and volts.

        :param channel: Which channel to read.
        :param points: How many points to transfer. Defaults to whatever the
                       scope is set to return.
        :param waveform_format: 'word' for 16-bit samples, 'byte' for 8-bit,
                                or 'ascii' for text. Word is the default: it
                                is as fast as byte to transfer per point and
                                keeps the full vertical resolution.
        :return: A tuple of (times, voltages), both lists of floats.
        """
        number = self._check_channel(channel)
        code = check_choice(waveform_format, WAVEFORM_FORMATS, "waveform format")

        self.write(f":WAVeform:SOURce CHANnel{number}")
        self.write(f":WAVeform:FORMat {code}")
        if points is not None:
            count = check_integer_range(points, 1, 8000000, "waveform points")
            self.write(f":WAVeform:POINts {count}")

        if waveform_format != "ascii":
            # Parsed as unsigned, so the scope is told to send unsigned
            # rather than trusting whatever it was last set to. A signed record
            # read as unsigned puts every sample below mid-screen 65536 counts
            # too high, which looks like a plausible trace at the wrong offset.
            # This is set before the preamble is asked for, because the
            # reference level the preamble reports is the one for the sign
            # convention in force at the moment of the question, and scaling
            # against the earlier one shifts the whole trace by half of scale.
            self.write(":WAVeform:UNSigned 1")
            if waveform_format == "word":
                self.write(":WAVeform:BYTeorder MSBFirst")

        preamble = self.preamble()

        if waveform_format == "ascii":
            raw = parse_ascii_block(self.query(":WAVeform:DATA?"))
        else:
            datatype = "H" if waveform_format == "word" else "B"
            raw = self.query_binary(
                ":WAVeform:DATA?",
                datatype=datatype,
                is_big_endian=(waveform_format == "word"),
            )

        return self.scale_waveform(
            raw, preamble, already_scaled=waveform_format == "ascii"
        )

    @staticmethod
    def scale_waveform(raw, preamble, already_scaled=False):
        """Turn raw samples and a preamble into seconds and volts.

        The scope sends integers spanning its digitizer range. The preamble
        gives the increment and origin for each axis, and the reference point
        those are measured from.

        :param already_scaled: True when the samples are already in volts,
                               which is what the ASCII format returns.
        :return: A tuple of (times, voltages).
        """
        times = [
            (index - preamble["x_reference"]) * preamble["x_increment"]
            + preamble["x_origin"]
            for index in range(len(raw))
        ]
        if already_scaled:
            return times, [float(value) for value in raw]

        voltages = [
            (value - preamble["y_reference"]) * preamble["y_increment"]
            + preamble["y_origin"]
            for value in raw
        ]
        return times, voltages

    def read_waveforms(self, channels=None, **kwargs):
        """Read several channels, sharing one time axis.

        :param channels: Which channels to read. Defaults to every channel
                         that is currently displayed.
        :return: A tuple of (times, {channel: voltages}).
        """
        if channels is None:
            channels = [
                number
                for number in range(1, self._channel_count + 1)
                if self.channel_enabled(number)
            ]
        if not channels:
            raise RangeError(
                "No channels are displayed, so there is nothing to read. Turn "
                "one on with set_channel_enabled()."
            )

        traces = {}
        times = []
        for number in channels:
            times, voltages = self.read_waveform(number, **kwargs)
            traces[number] = voltages
        return times, traces

    # Measurements

    def measure(self, quantity, channel=1):
        """Ask the scope for a measurement of the displayed trace.

        :param quantity: What to measure, e.g. 'peak to peak' or 'frequency'.
        :return: The value, or None if the scope could not make it. That
                 usually means there is no signal, or too little of one
                 period on screen to measure.
        """
        number = self._check_channel(channel)
        code = check_choice(quantity, MEASUREMENTS, "measurement")
        value = self.query_float(f":MEASure:{code}? CHANnel{number}")
        return None if value >= MEASUREMENT_FAILED else value

    def measure_all(self, channel=1, quantities=None):
        """Take several measurements of one channel.

        :return: A dict of measurement name to value, with None for any the
                 scope could not make.
        """
        names = sorted(MEASUREMENTS) if quantities is None else quantities
        return {name: self.measure(name, channel) for name in names}

    # Display

    @property
    def display_text(self):
        """Returns the message shown on the scope's display."""
        return self.query(":DISPlay:ANNotation:TEXT?").strip().strip('"')

    @display_text.setter
    def display_text(self, value):
        self.write(f':DISPlay:ANNotation:TEXT "{value}"')
        self.write(":DISPlay:ANNotation ON")

    def clear_display_text(self):
        """Stop showing a message."""
        self.write(":DISPlay:ANNotation OFF")

    def screenshot(self, image_format="png"):
        """Capture the screen as image data.

        :return: The raw bytes of the image, ready to write to a file.
        """
        code = check_choice(
            image_format, {"png": "PNG", "bmp": "BMP24bit"}, "image format"
        )
        self.write(":HARDcopy:INKSaver OFF")
        data = self.query_binary(f":DISPlay:DATA? {code},COLor", datatype="B")
        return bytes(bytearray(data))

    def __repr__(self):
        return f"InfiniiVision({self._transport!r})"
