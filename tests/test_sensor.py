"""Unit tests for sensor.py — entity state and attribute logic."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.carburanti_mimit.coordinator import CoordinatorData, FuelAreaData
from custom_components.carburanti_mimit.sensor import (
    AveragePriceSensor,
    CheapestPriceSensor,
    FavoriteStationSensor,
)
from tests.conftest import make_enriched, make_station


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_area(
    fuel_type: str = "Benzina",
    cheapest: float = 1.800,
    average: float = 1.850,
    n: int = 3,
) -> FuelAreaData:
    """Build a FuelAreaData with n stations, with IDs 1…n."""
    stations = [
        make_enriched(
            price=cheapest + i * 0.010,
            fuel_type=fuel_type,
            station=make_station(station_id=i + 1, nome=f"Stazione {i + 1}"),
            distance_km=1.0 + i,
        )
        for i in range(n)
    ]
    area = FuelAreaData(fuel_type=fuel_type)
    area.cheapest_price = cheapest
    area.cheapest_station = stations[0]
    area.top_stations = stations
    area.all_stations = stations        # new field: full list for favorite sensors
    area.average_price = average
    area.self_cheapest_price = cheapest
    area.servito_cheapest_price = cheapest + 0.05
    area.station_count = n
    return area


def _make_coordinator(fuel_type: str = "Benzina", n: int = 3) -> MagicMock:
    coord = MagicMock()
    coord.ai_cache = {}
    area = _make_area(fuel_type=fuel_type, n=n)
    coord.data = CoordinatorData(
        by_fuel={fuel_type: area},
        last_updated=datetime(2024, 1, 15, 8, 15, tzinfo=timezone.utc),
        station_count_in_radius=n,
        data_source="mimit_csv",
        stations_in_radius=[
            {"id": i + 1, "name": f"Stazione {i + 1}", "bandiera": "ENI",
             "comune": "Milano", "distance_km": 1.0 + i}
            for i in range(n)
        ],
    )
    return coord


def _make_entry(entry_id: str = "test_entry", options: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = options or {
        "fuel_types": ["Benzina"],
        "top_n": 3,
        "favorite_station_ids": [],
        "radius_km": 10,
        "ai_provider": "none",
        "ai_api_key": "",
    }
    return entry


# ---------------------------------------------------------------------------
# CheapestPriceSensor
# ---------------------------------------------------------------------------

class TestCheapestPriceSensor:
    def _sensor(self, fuel_type: str = "Benzina") -> CheapestPriceSensor:
        return CheapestPriceSensor(_make_coordinator(fuel_type), _make_entry(), fuel_type)

    def test_native_value_returns_cheapest_price(self):
        assert self._sensor().native_value == pytest.approx(1.800)

    def test_native_value_none_when_no_data(self):
        coord = MagicMock()
        coord.data = None
        assert CheapestPriceSensor(coord, _make_entry(), "Benzina").native_value is None

    def test_unit_benzina(self):
        assert self._sensor("Benzina")._attr_native_unit_of_measurement == "EUR/L"

    def test_unit_metano(self):
        assert self._sensor("Metano")._attr_native_unit_of_measurement == "EUR/kg"

    def test_attributes_station_name(self):
        assert self._sensor().extra_state_attributes["station_name"] == "Stazione 1"

    def test_attributes_top_stations_list(self):
        attrs = self._sensor().extra_state_attributes
        assert isinstance(attrs["top_stations"], list)
        assert len(attrs["top_stations"]) == 3

    def test_attributes_empty_when_no_station(self):
        coord = MagicMock()
        area = FuelAreaData(fuel_type="Benzina")
        coord.data = CoordinatorData(
            by_fuel={"Benzina": area},
            last_updated=datetime(2024, 1, 15, tzinfo=timezone.utc),
            station_count_in_radius=0,
        )
        assert CheapestPriceSensor(coord, _make_entry(), "Benzina").extra_state_attributes == {}

    def test_unique_id_format(self):
        uid = self._sensor()._attr_unique_id
        assert "test_entry" in uid and "Benzina" in uid and "cheapest" in uid


# ---------------------------------------------------------------------------
# AveragePriceSensor
# ---------------------------------------------------------------------------

class TestAveragePriceSensor:
    def _sensor(self) -> AveragePriceSensor:
        return AveragePriceSensor(_make_coordinator(), _make_entry(), "Benzina")

    def test_native_value(self):
        assert self._sensor().native_value == pytest.approx(1.850)

    def test_none_when_no_data(self):
        coord = MagicMock()
        coord.data = None
        assert AveragePriceSensor(coord, _make_entry(), "Benzina").native_value is None

    def test_unique_id(self):
        assert "average" in self._sensor()._attr_unique_id


# ---------------------------------------------------------------------------
# FavoriteStationSensor — station_id-based
# ---------------------------------------------------------------------------

class TestFavoriteStationSensor:
    def _sensor(self, station_id: int = 1, fuel_type: str = "Benzina", n: int = 3) -> FavoriteStationSensor:
        return FavoriteStationSensor(
            _make_coordinator(fuel_type, n), _make_entry(), fuel_type, station_id
        )

    # --- native_value ---

    def test_station_1_returns_cheapest(self):
        assert self._sensor(1).native_value == pytest.approx(1.800)

    def test_station_2_returns_second_price(self):
        assert self._sensor(2).native_value == pytest.approx(1.810)

    def test_station_3_returns_third_price(self):
        assert self._sensor(3).native_value == pytest.approx(1.820)

    def test_unknown_station_id_returns_none(self):
        # ID 99 doesn't exist in the 3-station area
        assert self._sensor(99, n=3).native_value is None

    def test_none_when_no_coordinator_data(self):
        coord = MagicMock()
        coord.data = None
        sensor = FavoriteStationSensor(coord, _make_entry(), "Benzina", 1)
        assert sensor.native_value is None

    # --- available ---

    def test_available_when_station_found(self):
        assert self._sensor(1).available is True

    def test_unavailable_for_missing_station(self):
        assert self._sensor(99, n=3).available is False

    # --- extra_state_attributes ---

    def test_attributes_has_station_id(self):
        assert self._sensor(2).extra_state_attributes["station_id"] == 2

    def test_attributes_has_correct_station_name(self):
        attrs = self._sensor(2).extra_state_attributes
        # _display_name prefixes bandiera when nome doesn't start with it
        assert "Stazione 2" in attrs["station_name"]

    def test_attributes_empty_for_unknown_station(self):
        assert self._sensor(99, n=3).extra_state_attributes == {}

    def test_attributes_has_distance(self):
        attrs = self._sensor(1).extra_state_attributes
        assert attrs["distance_km"] == pytest.approx(1.0)

    def test_attributes_has_data_source(self):
        assert self._sensor(1).extra_state_attributes["data_source"] == "mimit_csv"

    # --- unique_id ---

    def test_unique_id_contains_station_id(self):
        uid = self._sensor(42)._attr_unique_id
        assert "42" in uid and "station" in uid

    def test_unique_ids_differ_per_station(self):
        coord = _make_coordinator(n=3)
        entry = _make_entry()
        s1 = FavoriteStationSensor(coord, entry, "Benzina", 1)
        s2 = FavoriteStationSensor(coord, entry, "Benzina", 2)
        assert s1._attr_unique_id != s2._attr_unique_id

    def test_unique_ids_differ_per_fuel(self):
        coord = MagicMock()
        coord.data = None
        entry = _make_entry()
        sb = FavoriteStationSensor(coord, entry, "Benzina", 1)
        sg = FavoriteStationSensor(coord, entry, "Gasolio", 1)
        assert sb._attr_unique_id != sg._attr_unique_id

    # --- translation placeholders (dynamic: uses real station name) ---

    def test_placeholders_with_known_station(self):
        placeholders = self._sensor(1)._attr_translation_placeholders
        assert placeholders["fuel_type"] == "Benzina"
        assert "Stazione 1" in placeholders["station_name"]

    def test_placeholders_fallback_when_no_data(self):
        coord = MagicMock()
        coord.data = None
        sensor = FavoriteStationSensor(coord, _make_entry(), "Benzina", 7)
        ph = sensor._attr_translation_placeholders
        assert ph["fuel_type"] == "Benzina"
        assert "7" in ph["station_name"]   # fallback shows ID
