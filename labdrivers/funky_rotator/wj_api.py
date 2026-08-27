"""Low-level Python binding for the Fangcun Keyi WJ_API stepper-motor controller.

All 30 functions the controller library exports are bound here, and nothing
else in the package touches the DLL directly.

The controller is a generic multi-axis stepper driver, so everything here is
expressed in *pulses*, *axes* and raw controller units. Converting pulses into
something physical (degrees, for the rotation probe) is the job of the driver
built on top of this, not of this module.

Every call is ``__stdcall`` and returns an ``INT32``. The vendor documents
neither what that return code means nor the bit layout of the axis status
word, and I have not worked either of them out, so both are handed back to
the caller exactly as they arrived. See ``strict`` below.
"""

import ctypes
import os

# The DLL ships alongside this module. Vendor builds are 32-bit stdcall, so the
# interpreter's bitness has to match the DLL's.
DEFAULT_DLL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "WJ_API.dll"
)

# WJ_Open takes a serial port number, where 0 selects the USB connection.
USB_PORT = 0

# WJ_Get_Axes_Num reports 4 or 8 depending on the controller board fitted.
FALLBACK_AXIS_COUNT = 4


class WJApiError(RuntimeError):
    """Raised when a WJ_API call reports a failure and strict checking is on."""


class WJApi:
    """Thin wrapper around WJ_API.dll exposing all 30 exported functions.

    :param dll_path: Location of WJ_API.dll (default: alongside this module).
    :param strict: If True, any call returning a nonzero code raises WJApiError.
                   Nobody has established which code means success, so this is
                   off by default and every method hands back the raw code. Turn
                   it on once you know, and the calls will report their own
                   failures.
    """

    def __init__(self, dll_path=None, strict=False):
        self.dll_path = DEFAULT_DLL_PATH if dll_path is None else str(dll_path)
        self.strict = bool(strict)
        self._axis_count = None

        if not hasattr(ctypes, "WinDLL"):
            raise RuntimeError(
                "WJ_API.dll is a Windows stdcall library and can only be loaded "
                "on Windows."
            )
        if not os.path.isfile(self.dll_path):
            raise RuntimeError(
                f"Could not find WJ_API.dll at '{self.dll_path}'. Pass the "
                "correct location as dll_path, or place the DLL next to this "
                "module."
            )

        try:
            self._dll = ctypes.WinDLL(self.dll_path)
        except OSError as err:
            raise RuntimeError(
                f"Could not load '{self.dll_path}'. The vendor DLL is 32-bit, so "
                "this usually means the running Python is 64-bit. Use a 32-bit "
                f"interpreter or a 64-bit build of the DLL. Original error: {err}"
            )

        self._bind()

    # Binding
    #
    # argtypes and restype are declared once, here. ctypes stores them on the
    # function pointer itself, so setting them at call time would change the
    # signature every other caller sees.

    def _bind(self):
        c_int32 = ctypes.c_int32
        out_int = ctypes.POINTER(ctypes.c_int32)

        signatures = {
            # Communication
            "WJ_Open": [c_int32],
            "WJ_Close": [],
            # Queries
            "WJ_Get_Axis_Acc": [c_int32, out_int],
            "WJ_Get_Axis_Dec": [c_int32, out_int],
            "WJ_Get_Axis_Vel": [c_int32, out_int],
            "WJ_Get_Axis_Subdivision": [c_int32, out_int],
            "WJ_Get_Axis_Status": [c_int32, out_int],
            "WJ_Get_Axes_Status": [out_int],
            "WJ_Get_Axis_Pulses": [c_int32, out_int],
            "WJ_Get_Axes_Pulses": [out_int],
            "WJ_Get_Axes_Num": [out_int],
            # Motion
            "WJ_Move_Axis_Pulses": [c_int32, c_int32],
            "WJ_Move_Axes_Pulses": [out_int],
            "WJ_Move_Axis_Vel": [c_int32, c_int32],
            "WJ_Move_Axes_Vel": [out_int],
            "WJ_Move_Axis_Emergency_Stop": [c_int32],
            "WJ_Move_Axis_Slow_Stop": [c_int32],
            "WJ_Move_Axis_Home": [c_int32, c_int32],
            # Settings
            "WJ_Set_Axis_Acc": [c_int32, c_int32],
            "WJ_Set_Axis_Dec": [c_int32, c_int32],
            "WJ_Set_Axis_Vel": [c_int32, c_int32],
            "WJ_Set_Axis_Subdivision": [c_int32, c_int32],
            "WJ_Set_Axis_Slow_Stop": [c_int32, c_int32],
            "WJ_Set_Led_Twinkle": [],
            "WJ_Set_Axis_Pulses_Zero": [c_int32],
            "WJ_Set_Default": [],
            "WJ_Set_Move_Axis_Vel_Acc": [c_int32, c_int32],
            "WJ_Set_Axis_Home_Pulses": [c_int32, c_int32],
            # Digital IO
            "WJ_IO_Output": [c_int32, c_int32],
            "WJ_IO_Input": [c_int32, out_int],
        }

        missing = []
        for name, argtypes in signatures.items():
            try:
                function = getattr(self._dll, name)
            except AttributeError:
                missing.append(name)
                continue
            function.argtypes = argtypes
            function.restype = ctypes.c_int32

        if missing:
            raise RuntimeError(
                f"'{self.dll_path}' is missing {len(missing)} of the 30 expected "
                f"WJ_API exports ({', '.join(missing)}). This is probably not a "
                "WJ_API stepper controller DLL."
            )

    # Call helpers

    def _call(self, name, *args):
        """Invoke a bound function, honouring the strict-checking setting."""
        code = getattr(self._dll, name)(*args)
        if self.strict and code != 0:
            raise WJApiError(f"{name}{args} returned {code}.")
        return code

    def _call_out(self, name, *args):
        """Invoke a function whose last parameter is an INT32* output value.

        :return: The value the controller wrote, not the return code.
        """
        value = ctypes.c_int32()
        self._call(name, *args, ctypes.byref(value))
        return value.value

    def _buffer(self, values=None):
        """Build an INT32 array sized to the controller's actual axis count.

        The controller writes one element per axis, so the buffer has to match
        the board actually fitted rather than an assumed size.
        """
        length = max(self.axis_count, FALLBACK_AXIS_COUNT)
        buffer = (ctypes.c_int32 * length)()
        if values is not None:
            if len(values) > length:
                raise RuntimeError(
                    f"Got {len(values)} values for a {length}-axis controller."
                )
            for index, value in enumerate(values):
                buffer[index] = int(value)
        return buffer

    def _check_axis(self, axis):
        if not 1 <= int(axis) <= self.axis_count:
            raise RuntimeError(
                f"The axis must be an integer from 1 to {self.axis_count} on "
                f"this controller, but got {axis}."
            )
        return int(axis)

    # Communication

    def open(self, port=USB_PORT):
        """Open the controller connection.

        :param port: Serial port number, or 0 (default) for the USB connection.
        """
        code = self._call("WJ_Open", int(port))
        # Re-read the axis count now that a board is actually attached.
        self._axis_count = None
        return code

    def close(self):
        """Close the controller connection."""
        return self._call("WJ_Close")

    # Queries

    @property
    def axis_count(self):
        """Returns the number of axes the fitted controller board provides (4 or 8)."""
        if self._axis_count is None:
            count = self._call_out("WJ_Get_Axes_Num")
            self._axis_count = count if count in (4, 8) else FALLBACK_AXIS_COUNT
        return self._axis_count

    def get_axis_acceleration(self, axis):
        """Returns the acceleration setting of one axis, in controller units."""
        return self._call_out("WJ_Get_Axis_Acc", self._check_axis(axis))

    def get_axis_deceleration(self, axis):
        """Returns the deceleration setting of one axis, in controller units."""
        return self._call_out("WJ_Get_Axis_Dec", self._check_axis(axis))

    def get_axis_velocity(self, axis):
        """Returns the velocity setting of one axis, in controller units."""
        return self._call_out("WJ_Get_Axis_Vel", self._check_axis(axis))

    def get_axis_subdivision(self, axis):
        """Returns the microstepping subdivision setting of one axis."""
        return self._call_out("WJ_Get_Axis_Subdivision", self._check_axis(axis))

    def get_axis_status(self, axis):
        """Returns the raw status word of one axis.

        The vendor does not document the bit layout. Empirically, 0 means the
        axis is idle and a nonzero value means it is moving.
        """
        return self._call_out("WJ_Get_Axis_Status", self._check_axis(axis))

    def get_axes_status(self):
        """Returns the raw status word of every axis, as a list."""
        buffer = self._buffer()
        self._call("WJ_Get_Axes_Status", buffer)
        return list(buffer)[: self.axis_count]

    def get_axis_pulses(self, axis):
        """Returns the absolute pulse position of one axis."""
        return self._call_out("WJ_Get_Axis_Pulses", self._check_axis(axis))

    def get_axes_pulses(self):
        """Returns the absolute pulse position of every axis, as a list."""
        buffer = self._buffer()
        self._call("WJ_Get_Axes_Pulses", buffer)
        return list(buffer)[: self.axis_count]

    def get_axes_number(self):
        """Query the controller for its axis count (4 or 8)."""
        return self._call_out("WJ_Get_Axes_Num")

    # Motion

    def move_axis_pulses(self, axis, pulses):
        """Move one axis by a relative number of pulses (may be negative)."""
        return self._call("WJ_Move_Axis_Pulses", self._check_axis(axis), int(pulses))

    def move_axes_pulses(self, pulses):
        """Move every axis by a relative number of pulses.

        :param pulses: One value per axis, in axis order.
        """
        return self._call("WJ_Move_Axes_Pulses", self._buffer(pulses))

    def move_axis_velocity(self, axis, velocity):
        """Run one axis continuously at the given velocity."""
        return self._call("WJ_Move_Axis_Vel", self._check_axis(axis), int(velocity))

    def move_axes_velocity(self, velocities):
        """Run every axis continuously, one velocity per axis in axis order."""
        return self._call("WJ_Move_Axes_Vel", self._buffer(velocities))

    def emergency_stop(self, axis):
        """Stop one axis immediately, without a deceleration ramp."""
        return self._call("WJ_Move_Axis_Emergency_Stop", self._check_axis(axis))

    def slow_stop(self, axis):
        """Stop one axis using its configured deceleration ramp."""
        return self._call("WJ_Move_Axis_Slow_Stop", self._check_axis(axis))

    def move_axis_home(self, axis, value):
        """Send one axis to its home position."""
        return self._call("WJ_Move_Axis_Home", self._check_axis(axis), int(value))

    # Settings

    def set_axis_acceleration(self, axis, value):
        """Set the acceleration of one axis, in controller units."""
        return self._call("WJ_Set_Axis_Acc", self._check_axis(axis), int(value))

    def set_axis_deceleration(self, axis, value):
        """Set the deceleration of one axis, in controller units."""
        return self._call("WJ_Set_Axis_Dec", self._check_axis(axis), int(value))

    def set_axis_velocity(self, axis, value):
        """Set the velocity of one axis, in controller units."""
        return self._call("WJ_Set_Axis_Vel", self._check_axis(axis), int(value))

    def set_axis_subdivision(self, axis, value):
        """Set the microstepping subdivision of one axis."""
        return self._call("WJ_Set_Axis_Subdivision", self._check_axis(axis), int(value))

    def set_axis_slow_stop(self, axis, value):
        """Configure the slow-stop deceleration behavior of one axis."""
        return self._call("WJ_Set_Axis_Slow_Stop", self._check_axis(axis), int(value))

    def set_led_twinkle(self):
        """Blink the controller's front-panel LED, to identify the unit."""
        return self._call("WJ_Set_Led_Twinkle")

    def set_axis_pulses_zero(self, axis):
        """Define the current position of one axis as pulse zero."""
        return self._call("WJ_Set_Axis_Pulses_Zero", self._check_axis(axis))

    def set_default(self):
        """Restore the controller's factory default settings."""
        return self._call("WJ_Set_Default")

    def set_move_axis_velocity_acceleration(self, axis, value):
        """Set the combined move velocity/acceleration profile of one axis."""
        return self._call(
            "WJ_Set_Move_Axis_Vel_Acc", self._check_axis(axis), int(value)
        )

    def set_axis_home_pulses(self, axis, value):
        """Set the pulse position that one axis treats as home."""
        return self._call("WJ_Set_Axis_Home_Pulses", self._check_axis(axis), int(value))

    # Digital IO

    def io_output(self, line, value):
        """Set a digital output line."""
        return self._call("WJ_IO_Output", int(line), int(value))

    def io_input(self, line):
        """Read a digital input line."""
        return self._call_out("WJ_IO_Input", int(line))
