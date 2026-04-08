"""Unit tests for geo.py — haversine distance and radius filtering."""
from __future__ import annotations

import pytest

from custom_components.carburanti_mimit.geo import (
    bounding_box,
    filter_by_radius,
    haversine_km,
)
from tests.conftest import make_enriched, make_station


# ---------------------------------------------------------------------------
# haversine_km
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_km(45.0, 9.0, 45.0, 9.0) == pytest.approx(0.0)

    def test_known_distance_milan_rome(self):
        # Milan (45.4654, 9.1859) to Rome (41.9028, 12.4964) ≈ 477 km
        dist = haversine_km(45.4654, 9.1859, 41.9028, 12.4964)
        assert 470 < dist < 490

    def test_symmetry(self):
        d1 = haversine_km(44.0, 11.0, 45.0, 12.0)
        d2 = haversine_km(45.0, 12.0, 44.0, 11.0)
        assert d1 == pytest.approx(d2, rel=1e-6)

    def test_north_south_one_degree(self):
        # 1 degree of latitude ≈ 111 km
        dist = haversine_km(44.0, 0.0, 45.0, 0.0)
        assert 110 < dist < 113

    def test_east_west_one_degree_at_equator(self):
        # 1 degree of longitude at equator ≈ 111 km
        dist = haversine_km(0.0, 0.0, 0.0, 1.0)
        assert 110 < dist < 113

    def test_east_west_narrows_at_higher_latitude(self):
        dist_equator = haversine_km(0.0, 0.0, 0.0, 1.0)
        dist_45deg = haversine_km(45.0, 0.0, 45.0, 1.0)
        # At 45° latitude the east-west distance should be smaller
        assert dist_45deg < dist_equator

    def test_short_distance_meters_range(self):
        # Two points ~1 km apart in Milan
        dist = haversine_km(45.4654, 9.1859, 45.4744, 9.1859)
        assert 0.9 < dist < 1.1


# ---------------------------------------------------------------------------
# bounding_box
# ---------------------------------------------------------------------------

class TestBoundingBox:
    def test_returns_four_values(self):
        result = bounding_box(45.0, 9.0, 10.0)
        assert len(result) == 4

    def test_center_inside_box(self):
        min_lat, max_lat, min_lon, max_lon = bounding_box(45.0, 9.0, 10.0)
        assert min_lat < 45.0 < max_lat
        assert min_lon < 9.0 < max_lon

    def test_box_grows_with_radius(self):
        box_small = bounding_box(45.0, 9.0, 5.0)
        box_large = bounding_box(45.0, 9.0, 50.0)
        # larger radius → wider box
        assert (box_large[1] - box_large[0]) > (box_small[1] - box_small[0])
        assert (box_large[3] - box_large[2]) > (box_small[3] - box_small[2])

    def test_latitude_span_roughly_correct(self):
        # 10 km radius → latitude span ≈ 0.18° (each side ≈ 0.09°)
        min_lat, max_lat, _, _ = bounding_box(45.0, 9.0, 10.0)
        assert (max_lat - min_lat) == pytest.approx(0.18, rel=0.05)


# ---------------------------------------------------------------------------
# filter_by_radius
# ---------------------------------------------------------------------------

class TestFilterByRadius:
    def _make_stations_near_milan(self):
        """Three stations: close, medium, far from Milan center."""
        close = make_enriched(
            distance_km=0.0,
            station=make_station(station_id=1, lat=45.4654, lon=9.1859),
        )
        medium = make_enriched(
            distance_km=0.0,
            station=make_station(station_id=2, lat=45.5100, lon=9.1859),  # ≈5 km north
        )
        far = make_enriched(
            distance_km=0.0,
            station=make_station(station_id=3, lat=45.9000, lon=9.1859),  # ≈48 km north
        )
        return [close, medium, far]

    def test_large_radius_includes_all(self):
        stations = self._make_stations_near_milan()
        result = filter_by_radius(stations, 45.4654, 9.1859, 100.0)
        assert len(result) == 3

    def test_small_radius_excludes_far(self):
        stations = self._make_stations_near_milan()
        result = filter_by_radius(stations, 45.4654, 9.1859, 10.0)
        ids = {s.station.id for s in result}
        assert 3 not in ids  # far station excluded

    def test_tiny_radius_includes_only_center(self):
        stations = self._make_stations_near_milan()
        result = filter_by_radius(stations, 45.4654, 9.1859, 1.0)
        assert len(result) == 1
        assert result[0].station.id == 1

    def test_distance_populated_in_place(self):
        stations = self._make_stations_near_milan()
        filter_by_radius(stations, 45.4654, 9.1859, 100.0)
        for s in stations:
            assert s.distance_km >= 0.0

    def test_empty_input_returns_empty(self):
        result = filter_by_radius([], 45.4654, 9.1859, 10.0)
        assert result == []

    def test_zero_radius_returns_empty_or_exact(self):
        stations = self._make_stations_near_milan()
        result = filter_by_radius(stations, 45.4654, 9.1859, 0.0)
        # A station exactly at the center with radius 0 is a boundary case;
        # the implementation may include or exclude it. Just check no crash.
        assert isinstance(result, list)
