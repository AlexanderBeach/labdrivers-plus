"""Driver for the Fangcun Keyi rotation probe.

The probe is a single-axis sample rotator for a VTI, driven by a stepper motor
whose controller is spoken to through :mod:`labdrivers.funky_rotator.wj_api`.
This module owns everything physical, meaning degrees, angle limits and the
calibration between them, and wj_api owns pulses and raw controller units.

Specifications, from the vendor manual (Rotation Probe User's Manual v2.2):
    Angle range         -5 deg to 365 deg, continuously tunable
    Angle resolution    better than 0.01 deg
    Temperature range   1.5 K to 320 K
    Speed setting       integer, minimum 1 (~1.8 deg/s), 1 to 3 recommended
"""

import time

from ..core.sweep import round_trip, sweep_values
from .wj_api import USB_PORT, WJApi

# Pulses per full revolution of the probe.
#
# The vendor quotes 50000 in its own configuration files, but 50004 is the
# figure this driver is calibrated against, and I do not know which of them
# is right. Over a full turn they differ by 0.029 deg, so it matters if you
# care about the third decimal place. Measure your own probe and pass
# pulses_per_revolution= if you need to be sure.
PULSES_PER_REVOLUTION = 50004
VENDOR_PULSES_PER_REVOLUTION = 50000

# Hard travel limits. The manual is emphatic about these: "Attention! The angle
# range is limited between -5 and 365 degrees. Don't exceed this range."
MAXIMUM_ANGLE = 365.0
MINIMUM_ANGLE = -5.0

# Longest a single move is allowed to take before wait_while_moving gives up.
# A full 370 degree sweep at the minimum speed of ~1.8 deg/s takes ~3.5 minutes.
DEFAULT_MOVE_TIMEOUT = 600.0

DEGREE = "°"


class Rotator:
    """Interface to a Fangcun Keyi rotation probe.

    :param axis: Controller axis the probe is wired to (default: 1).
    :param port: Serial port number, or 0 (default) for the USB connection.
    :param dll_path: Location of WJ_API.dll (default: alongside wj_api.py).
    :param pulses_per_revolution: Stepper pulses per 360 deg (default: 50004).
    :param strict: If True, failed controller calls raise instead of returning
                   a status code. See wj_api.WJApi.
    """

    def __init__(
        self,
        axis=1,
        port=USB_PORT,
        dll_path=None,
        pulses_per_revolution=PULSES_PER_REVOLUTION,
        strict=False,
    ):
        if float(pulses_per_revolution) <= 0:
            raise RuntimeError(
                "The number of pulses per revolution must be a positive number, "
                f"but got {pulses_per_revolution}."
            )

        self._api = WJApi(dll_path=dll_path, strict=strict)
        self._pulses_per_revolution = float(pulses_per_revolution)

        # Close before opening. The controller holds its USB handle across
        # program restarts and refuses a second connection while it is held.
        self._api.close()
        self._api.open(port)

        self._axis = self._api._check_axis(axis)

    # Unit conversion

    def _degrees_to_pulses(self, degrees):
        """Convert an angle in degrees to controller pulses."""
        return round(float(degrees) * self._pulses_per_revolution / 360.0)

    def _pulses_to_degrees(self, pulses):
        """Convert controller pulses to an angle in degrees."""
        return float(pulses) * 360.0 / self._pulses_per_revolution

    def _check_angle(self, angle):
        """Reject any angle outside the probe's mechanical travel."""
        if not MINIMUM_ANGLE <= float(angle) <= MAXIMUM_ANGLE:
            raise RuntimeError(
                f"The angle must be between {MINIMUM_ANGLE}{DEGREE} and "
                f"{MAXIMUM_ANGLE}{DEGREE}, but got {angle}{DEGREE}. Exceeding "
                "range can damage the probe."
            )
        return float(angle)

    # Position

    @property
    def axis(self):
        """Returns the controller axis the probe is wired to."""
        return self._axis

    @property
    def pulses(self):
        """Returns the absolute position of the probe, in controller pulses."""
        return self._api.get_axis_pulses(self._axis)

    @property
    def angle(self):
        """Returns the absolute position of the probe, in degrees."""
        return self._pulses_to_degrees(self.pulses)

    @property
    def is_moving(self):
        """Returns True while the probe is in motion.

        The vendor does not document the status word, but a value of 0 means
        the axis is idle and any nonzero value means it is moving.
        """
        return self._api.get_axis_status(self._axis) != 0

    # Motion
    #
    # move_to() is absolute and move_by() is relative. Both read the position
    # from the controller at the moment they are called, so neither computes a
    # move from a stale reading.

    def move_to(self, angle, wait=True, progress=False):
        """Move the probe to an absolute angle.

        :param angle: Target angle in degrees, within the probe's travel.
        :param wait: Block until the move finishes (default: True).
        :param progress: Print a live position line while waiting.
        """
        self._check_angle(angle)
        change = self._degrees_to_pulses(angle) - self.pulses
        self._api.move_axis_pulses(self._axis, change)
        if wait:
            self.wait_while_moving(progress=progress)

    def move_by(self, degrees, wait=True, progress=False):
        """Move the probe by a relative number of degrees.

        :param degrees: Angle to move through, positive or negative.
        :param wait: Block until the move finishes (default: True).
        :param progress: Print a live position line while waiting.
        """
        self._check_angle(self.angle + float(degrees))
        self._api.move_axis_pulses(self._axis, self._degrees_to_pulses(degrees))
        if wait:
            self.wait_while_moving(progress=progress)

    def wait_while_moving(
        self, poll_interval=0.5, timeout=DEFAULT_MOVE_TIMEOUT, progress=False
    ):
        """Block until the probe stops moving.

        :param poll_interval: Seconds between status reads (default: 0.5).
        :param timeout: Seconds to wait before giving up (default: 600).
        :param progress: Print a live position line while waiting.
        :raises RuntimeError: If the probe is still moving after the timeout.
        """
        deadline = time.monotonic() + float(timeout)
        while self.is_moving:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"The probe was still moving after {timeout} s, at "
                    f"{self.angle:.2f}{DEGREE}. Check that nothing is obstructing "
                    "it, then stop it with emergency_stop()."
                )
            if progress:
                print(f"\rMoving, currently at {self.angle:.2f}{DEGREE}", end="")
            time.sleep(poll_interval)
        if progress:
            print(f"\rStopped at {self.angle:.2f}{DEGREE}" + " " * 20)

    def stop(self):
        """Stop the probe using its configured deceleration ramp."""
        self._api.slow_stop(self._axis)

    def emergency_stop(self):
        """Stop the probe immediately, with no deceleration ramp."""
        self._api.emergency_stop(self._axis)

    def define_zero(self):
        """Define the probe's current position as zero degrees.

        Use this to align the driver with the angle shown on the probe's dial.
        """
        self._api.set_axis_pulses_zero(self._axis)

    # Motion settings

    @property
    def speed(self):
        """Returns the speed setting of the probe.

        A setting of 1 is roughly 1.8 deg/s.
        """
        return self._api.get_axis_velocity(self._axis)

    @speed.setter
    def speed(self, value):
        if int(value) >= 1:
            self._api.set_axis_velocity(self._axis, int(value))
        else:
            raise RuntimeError(
                "The speed must be an integer of 1 or greater, where 1 is about "
                f"1.8{DEGREE}/s. The manual recommends 1 to 3."
            )

    @property
    def acceleration(self):
        """Returns the acceleration setting of the probe, in controller units."""
        return self._api.get_axis_acceleration(self._axis)

    @acceleration.setter
    def acceleration(self, value):
        self._api.set_axis_acceleration(self._axis, int(value))

    @property
    def deceleration(self):
        """Returns the deceleration setting of the probe, in controller units."""
        return self._api.get_axis_deceleration(self._axis)

    @deceleration.setter
    def deceleration(self, value):
        self._api.set_axis_deceleration(self._axis, int(value))

    @property
    def subdivision(self):
        """Returns the microstepping subdivision setting of the probe."""
        return self._api.get_axis_subdivision(self._axis)

    @subdivision.setter
    def subdivision(self, value):
        self._api.set_axis_subdivision(self._axis, int(value))

    def sweep_angles(
        self, start, stop, points=None, step=None, settle=0.0, return_to_start=False
    ):
        """Move through a series of angles, yielding once the probe arrives.

            for angle in probe.sweep_angles(0, 180, step=5):
                x, y = lockin.measure()

        :param start: First angle, in degrees.
        :param stop: Last angle, in degrees.
        :param points: Number of angles, including both ends.
        :param step: Spacing between angles, as an alternative to points.
        :param settle: Extra seconds to wait after each move finishes.
        :param return_to_start: Sweep back again, for hysteresis.
        :yield: The angle actually reached, in degrees.
        """
        angles = sweep_values(start, stop, points=points, step=step)
        if return_to_start:
            angles = round_trip(angles)

        for angle in angles:
            self.move_to(angle, wait=True)
            if settle:
                time.sleep(settle)
            yield self.angle

    # Connection

    def identify(self):
        """Blink the controller's front-panel LED, to identify the unit."""
        self._api.set_led_twinkle()

    def close(self):
        """Close the connection to the stepper controller."""
        self._api.close()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()
        return False
