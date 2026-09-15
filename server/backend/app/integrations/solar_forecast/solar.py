"""Solar geometry — pure arithmetic, no dependencies.

One function: is the sun up at this moment, and how far. Needed because nights
are trivially zero on both sides of the comparison: include them and MAE looks
excellent while skill looks like nothing, because the baseline nails them too.
Filtering them out is what makes the accuracy numbers mean anything.

Standard low-precision solar position (NOAA's, the one in every almanac):
accurate to a fraction of a degree, which is far more than "is it light" needs.
Deliberately not a dependency — `pvlib` would be a large addition to every
deployed image for twenty lines of trigonometry, and coglib's bar exists for
exactly that reason.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def solar_elevation_deg(at: datetime, latitude: float, longitude: float) -> float:
    """Sun elevation above the horizon, in degrees, at a UTC moment.

    Negative means below the horizon. `at` is treated as UTC — a naive
    timestamp is assumed UTC rather than local, because everything in the algo
    harness stores tz-aware UTC and silently reinterpreting a naive value as
    local time would shift the whole day by an hour for half the year.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    at = at.astimezone(timezone.utc)

    # Fractional year (radians).
    day_of_year = at.timetuple().tm_yday
    hour = at.hour + at.minute / 60 + at.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)

    # Equation of time (minutes) and solar declination (radians).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    # True solar time -> hour angle. `at` is already UTC, so the timezone
    # offset term of the standard formula is zero.
    time_offset = eqtime + 4 * longitude
    true_solar_min = hour * 60 + time_offset
    hour_angle = math.radians(true_solar_min / 4 - 180)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        hour_angle
    )
    # Clamp before acos: accumulated float error can push this a hair past ±1
    # inside the Arctic circle, and a ValueError from acos is a very confusing
    # way to find that out.
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))
