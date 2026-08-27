"""Helpers for building the list of points a measurement sweeps through.

Almost every measurement is a loop over values of something, whether that is
a gate voltage, a magnetic field, a temperature or an angle. The arithmetic
for turning a start, a stop and either a count or a step size into that list
of values is the same in every case, so it lives here and every driver's sweep
method calls into it.
"""

import math

from .errors import RangeError


def sweep_values(start, stop, points=None, step=None, spacing="linear"):
    """Build the list of values a sweep visits.

    Give either ``points`` or ``step``, not both.

        sweep_values(0, 1, points=5)      -> [0.0, 0.25, 0.5, 0.75, 1.0]
        sweep_values(0, 1, step=0.25)     -> [0.0, 0.25, 0.5, 0.75, 1.0]
        sweep_values(1, 10, points=3, spacing="logarithmic")

    The stop value is always included. A step that does not divide the interval
    evenly is honored for every whole step and the stop value is appended, so
    a sweep never silently ends short of where it was asked to go.

    :param start: First value.
    :param stop: Last value.
    :param points: How many values, including both ends. Two or more.
    :param step: Spacing between values. The sign is ignored. Direction comes
                 from start and stop.
    :param spacing: 'linear' or 'logarithmic'. A logarithmic sweep needs both
                    ends to have the same sign and neither to be zero.
    :return: A list of floats.
    """
    start, stop = float(start), float(stop)

    if (points is None) == (step is None):
        raise RangeError("A sweep needs either points= or step=, but not both.")

    if step is not None:
        if float(step) == 0:
            raise RangeError("The sweep step size cannot be zero.")
        if spacing != "linear":
            raise RangeError(
                "A logarithmic sweep is defined by a number of points, not a "
                "step size."
            )
        magnitude = abs(float(step))
        direction = 1.0 if stop >= start else -1.0
        values = []
        count = int(abs(stop - start) / magnitude) + 1
        for index in range(count):
            values.append(start + direction * magnitude * index)
        # Include the endpoint even when the step does not divide the interval.
        if not values or abs(values[-1] - stop) > magnitude * 1e-9:
            values.append(stop)
        return values

    count = int(points)
    if count < 2:
        raise RangeError(f"A sweep needs at least 2 points, but got {points}.")

    if spacing == "logarithmic":
        if start == 0 or stop == 0 or (start < 0) != (stop < 0):
            raise RangeError(
                "A logarithmic sweep needs both ends to be nonzero and to have "
                f"the same sign, but got {start} to {stop}."
            )
        sign = -1.0 if start < 0 else 1.0
        first, last = math.log10(abs(start)), math.log10(abs(stop))
        return [
            sign * 10 ** (first + (last - first) * index / (count - 1))
            for index in range(count)
        ]

    if spacing != "linear":
        raise RangeError(
            f"The sweep spacing can be 'linear' or 'logarithmic', but got "
            f"{spacing!r}."
        )
    return [start + (stop - start) * index / (count - 1) for index in range(count)]


def round_trip(values):
    """Turn a one-way sweep into an up-and-back one.

    The return leg is the same values reversed, with the turning point not
    repeated, which is what a hysteresis measurement wants.

        round_trip([0, 1, 2]) -> [0, 1, 2, 1, 0]
    """
    values = list(values)
    return values + values[-2::-1]
