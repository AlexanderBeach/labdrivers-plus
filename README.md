# labdrivers-plus

A laboratory tends to accumulate instruments faster than it accumulates the
code to drive them, and the code that does get written is usually written once,
by whoever needed it that week, for the one instrument in front of them. The
result is a folder of scripts that each speak a slightly different dialect, and
none of which say anything useful when an instrument refuses a setting.

This package is a set of drivers for the instruments common to a low
temperature transport laboratory, along with the oscilloscopes, waveform
generators and network analyzers that tend to sit alongside them. Each
instrument is presented as an object with properties rather than as a list
of command strings. Every setting is checked in Python before it is sent,
against what that particular instrument will accept:

```python
from labdrivers.keithley import Keithley2400

source = Keithley2400(gpib_address=24)
source.configure_source("voltage", compliance=1e-3)
source.output = True

for level, voltage, current in source.sweep_source(0, 1, points=101):
    print(level, voltage, current)

source.safe_shutdown()
```

Asking for something the instrument cannot do raises before anything reaches
the wire, and the message names the range rather than the command:

```python
>>> source.voltage_compliance = 500
RangeError: The voltage compliance must be between -210.0 V and 210.0 V, but got 500 V.
```

That limit is not a constant in the driver. It comes from the model the
instrument reported when the connection opened, so the same line of code is
accepted on a 2410, which really does reach 1100 V, and refused on a 2400,
which does not.

## Installing

```bash
pip install labdrivers-plus
```

The package needs Python 3.12 or newer. Installing it also installs `pyvisa`,
which needs a VISA implementation underneath it, and on Windows that is
normally [NI-VISA](https://www.ni.com/visa/). Without one, opening any GPIB,
USB or serial instrument will raise `ConnectionFailure` with a message saying
the VISA library could not be loaded.

Two of the drivers wrap a library supplied by the instrument vendor rather
than speaking to the instrument directly. Those libraries are separate
installs:

```bash
pip install "labdrivers-plus[ni]"             # National Instruments DAQ
pip install "labdrivers-plus[quantumdesign]"  # PPMS, DynaCool, MPMS
```

The Quantum Design and rotation probe drivers also each load a vendor DLL at
runtime, `QDInstrument.dll` and `WJ_API.dll`. Both ship with this package and
are found automatically, but a copy from elsewhere can be used by passing
`dll_path=` to the constructor. Two things go wrong with them often enough to
be worth stating in advance. `QDInstrument.dll` is a 32 bit assembly, so it
will not load into a 64 bit Python. Windows also marks any DLL that arrived
in a download as blocked, but this shows up only as a load failure, and the
file's Properties dialog has an Unblock button that clears it.

## Connecting

Every driver takes the same connection arguments. The interface an instrument
happens to be wired with is chosen when the object is constructed, rather than
by importing a different class:

```python
Sr830(gpib_address=8)                                     # GPIB
Sr830(resource_name="ASRL3::INSTR", baud_rate=9600)       # RS-232
Sr830(resource_name="TCPIP0::192.168.0.20::INSTR")        # LAN
Triton200(ip_address="192.168.0.12")                      # raw socket
```

Instruments can be used as context managers, which closes the connection even
if the measurement raises partway through:

```python
with Keithley2400(gpib_address=24) as source:
    ...
```

## Instruments

| Vendor | Driver | Instrument |
| --- | --- | --- |
| Keithley | `Keithley2400` | 2400 series SourceMeter (2400, 2400-LV, 2401, 2410, 2420, 2425, 2430, 2440) |
| Keithley | `Keithley6221` | 6221 AC and DC current source |
| Keithley | `Keithley2182` | 2182 and 2182A nanovoltmeter |
| SRS | `Sr830` | SR830 DSP lock-in amplifier |
| Oxford | `Triton200` | Triton 200 dilution refrigerator |
| Oxford | `MercuryIps` | Mercury iPS magnet power supply |
| Oxford | `MercuryIpsTeslatron` | Mercury iPS as fitted to a TeslatronPT |
| Oxford | `MercuryItc` | Mercury iTC temperature controller |
| Oxford | `MercuryItcHeliox` | Mercury iTC as fitted to a HelioxVT insert |
| Oxford | `Ips120` | IPS 120-10 magnet power supply |
| Oxford | `Itc503` | ITC 503 temperature controller |
| Lake Shore | `Ls332` | Model 332 temperature controller |
| Quantum Design | `Ppms`, `Dynacool`, `VersaLab`, `Svsm`, `Mpms` | cryostats, through MultiVu |
| National Instruments | `Nidaq` | DAQ analog and digital input and output |
| Keysight | `Keysight33500` | 33500B and 33600A waveform generators |
| Keysight | `InfiniiVision` | InfiniiVision X-Series oscilloscopes |
| Rigol | `RigolDG1000Z` | DG1000Z waveform generators |
| Copper Mountain | `Vna` | Copper Mountain and Keysight ENA network analyzers |
| Fangcun Keyi | `Rotator` | rotation probe, through its vendor DLL |

## Sweeps and other common procedures

A measurement is almost always a loop over values of something, so every driver
that has something worth sweeping exposes that sweep as a generator. The
readings arrive one at a time as they are taken, so a run can be plotted as it
happens, written to disk as it goes, or stopped partway through without losing
what has already been measured:

```python
for field in supply.z.sweep_field(-1, 1, points=41):
    x, y = lockin.measure()
    print(field, x, y)
```

The same shape appears throughout. `sweep_source` steps the Keithley 2400,
`sweep_field` steps a magnet, `sweep_temperature` steps the cryostats and the
Lake Shore controller, `sweep_angles` steps the rotation probe, and
`sweep_frequency` steps a waveform generator. Each of them takes either a
number of points or a step size, accepts linear or logarithmic spacing, and can
be asked to return to its starting value so that a hysteresis loop comes out of
one call.

Alongside the sweeps there are a handful of procedures that otherwise get
written out by hand every time somebody needs them:

- `safe_shutdown()` walks a source down to zero before opening its output,
  because opening a live output leaves whatever charge is on the device to find
  its own way out.
- `Sr830.wait_to_settle()` waits according to the lock-in's actual filter
  order rather than assuming a single pole. Five time constants is the usual
  rule, but that holds for one pole, and a filter rolling off at 24 dB per
  octave has four of them. A reading taken too early returns the filter's old
  contents rather than the new signal.
- `read_average()` on the nanovoltmeter and on the DAQ returns a mean together
  with its standard error, because the scatter is what says whether a reading
  is limited by noise or by something that is drifting.
- `InfiniiVision.read_waveform()` applies the oscilloscope's preamble and
  returns seconds and volts. The instrument sends raw integers spanning its
  digitiser range. A trace read without applying the preamble looks entirely
  plausible, but it is wrong by whatever the vertical scale and offset
  happened to be.
- `Vna.read_complex_trace()` returns complex S-parameters rather than the
  formatted values the display happens to be showing, which is what any
  subsequent fitting or de-embedding needs.

## Configuring for a particular laboratory

A good deal of what an instrument driver appears to know is really a property
of the laboratory rather than of the instrument. The field a magnet can reach
belongs to the magnet, and the board a temperature sensor is wired to belongs
to the cryostat. The heater current a mixing chamber wants at a given
temperature belongs to that particular fridge and its heater resistance. All
of these are constructor arguments with defaults, rather than constants in
the driver:

```python
MercuryIps(ip_address="...", field_limits={"GRPZ": 14.0})
MercuryItc(ip_address="...", sensors={"sample": "DB3.T1"})
Triton200(ip_address="...", heater_ranges=("1", "10", "100"),
          heater_thresholds=(0.1, 1.0))
Rotator(pulses_per_revolution=50000)
```

## Errors

Everything raised by this package derives from `RuntimeError`, so a script
written against an older driver that catches `RuntimeError` keeps working. The
type still says what went wrong:

- `RangeError` is a setting outside what the instrument accepts. It also
  derives from `ValueError`, since that is what code validating user input
  would naturally catch.
- `ConnectionFailure` is an instrument that could not be reached or opened.
- `InstrumentError` is a fault the instrument reported about itself.
- `InstrumentTimeoutError` is an instrument that did not reply, or a wait that
  never settled.

Every command sent and every reply received can be logged. This is usually
the fastest way to find out why an instrument is behaving unexpectedly:

```python
from labdrivers.core import enable_logging
enable_logging()
```

## Adding a driver

An instrument that speaks SCPI should subclass `ScpiInstrument`, and one that
speaks a protocol of its own should subclass `Instrument`. Either way the
connection handling, the reply parsing and the argument validation come with
the base class. The driver itself only has to carry what is specific to the
instrument:

```python
from labdrivers.core import ScpiInstrument, check_range

class MyInstrument(ScpiInstrument):
    IDENTIFIER = "MODEL 1234"

    @property
    def level(self):
        """Output level, in volts."""
        return self.query_float("SOUR:LEV?")

    @level.setter
    def level(self, value):
        check_range(value, -10, 10, "output level", " V")
        self.write(f"SOUR:LEV {value}")
```

The driver can then be tested against `RecordingTransport`, which stands in for
the instrument and records the exact bytes the driver sends. No hardware is
involved, so the tests run anywhere:

```python
from labdrivers.core import RecordingTransport

def test_level_command():
    transport = RecordingTransport()
    MyInstrument(transport=transport).level = 1.5
    assert transport.writes == ["SOUR:LEV 1.5"]
```

Every command in this package is tested this way against the string printed
in the manufacturer's programming manual. Transcribing a few hundred commands
out of a manual introduces typing errors, but a mistyped command is not
something an instrument reports back in any useful way. Asserting the exact
bytes is the only check available short of connecting the instrument itself.

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

## Known limits

Every command is asserted against the manufacturer's programming manual, and
that catches a command which was transcribed wrongly. It does not catch a
misunderstanding about how an instrument behaves, and only an instrument can
settle that. The delta and pulse delta modes on the Keithley 6221 depend on a
2182A being cabled to it over the trigger link, and those modes, together with
the current setpoint on the Mercury iPS, are where the distinction is most
likely to matter.

Two calibration figures are unresolved. The rotation probe's vendor quotes
50000 stepper pulses per revolution in its own configuration files, but the
driver is calibrated against 50004, and over a full turn the two disagree by
0.029 degrees. The return codes from the rotator's controller library are
undocumented by its vendor, so the driver passes them back untouched rather
than guessing at which value means success.

## Origin

A fork of [masonlab/labdrivers](https://github.com/masonlab/labdrivers), whose
history it keeps.

## License

MIT. See [LICENSE](LICENSE).
