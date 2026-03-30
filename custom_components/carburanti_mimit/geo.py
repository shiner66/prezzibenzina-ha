"""Geographic utilities: haversine distance and radius filtering."""
from __future__ import annotations

import math

from .parser import EnrichedStation

EARTH_RADIUS_KM = 6371.0
_DEG_TO_RAD = math.pi / 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in kilometres between two WGS-84 points."""
    dlat = (lat2 - lat1) * _DEG_TO_RAD
    dlon = (lon2 - lon1) * _DEG_TO_RAD
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * _DEG_TO_RAD)
        * math.cos(lat2 * _DEG_TO_RAD)
        * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bounding_box(
    lat: float, lon: float, radius_km: float
) -> tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lon, max_lon) for a fast pre-filter.

    The longitude correction uses ``cos(lat)`` to account for meridian
    convergence.  Works correctly for Italian latitudes (36°–47°N).
    """
    delta_lat = radius_km / EARTH_RADIUS_KM / _DEG_TO_RAD
    delta_lon = radius_km / (EARTH_RADIUS_KM * math.cos(lat * _DEG_TO_RAD)) / _DEG_TO_RAD
    return (
        lat - delta_lat,
        lat + delta_lat,
        lon - delta_lon,
        lon + delta_lon,
    )


def filter_by_radius(
    stations: list[EnrichedStation],
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> list[EnrichedStation]:
    """Return stations within *radius_km* of (*center_lat*, *center_lon*).

    Uses a bounding-box pre-filter to minimise haversine calls.
    Populates ``distance_km`` on each matching station in-place.
    """
    min_lat, max_lat, min_lon, max_lon = bounding_box(center_lat, center_lon, radius_km)

    result: list[EnrichedStation] = []
    for s in stations:
        slat = s.station.lat
        slon = s.station.lon
        # Cheap bounding-box check first
        if not (min_lat <= slat <= max_lat and min_lon <= slon <= max_lon):
            continue
        # Accurate haversine check
        dist = haversine_km(center_lat, center_lon, slat, slon)
        if dist <= radius_km:
            s.distance_km = dist
            result.append(s)

    return result
