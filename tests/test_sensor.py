"""Unit tests for sensor.py — entity state and attribute logic."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
    top_n: int = 3,
) -> FuelAreaData:
    stations = [
        make_enriched(
            price=cheapest + i * 0.010,
            fuel_type=fuel_type,
            station=make_station(station_id=i + 1, nome=f"Stazione {i + 1}"),
            distance_km=1.0 + i,
        )
        for i in range(top_n)
    ]
    area = FuelAreaData(fuel_type=fuel_type)
    area.cheapest_price = cheapest
    area.cheapest_station = stations[0]
    area.top_stations = stations
    area.average_price = average
    area.self_cheapest_price = cheapest
    area.servito_cheapest_price = cheapest + 0.05
    area.station_count = top_n
    return area


def _make_coordinator_with_data(fuel_type: str = "Benzina", top_n: int = 3) -> MagicMock:
    coordinator = MagicMock()
    coordinator.ai_cache = {}
    area = _make_area(fuel_type=fuel_type, top_n=top_n)
    coordinator.data = CoordinatorData(
        by_fuel={fuel_type: area},
        last_updated=datetime(2024, 1, 15, 8, 15, 0, tzinfo=timezone.utc),
        station_count_in_radius=top_n,
        data_source="mimit_csv",
    )
    return coordinator


def _make_config_entry(entry_id: str = "test_entry", options: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = options or {
        "fuel_types": ["Benzina"],
        "top_n": 3,
        "show_individual_stations": False,
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
        coordinator = _make_coordinator_with_data(fuel_type)
        entry = _make_config_entry()
        return CheapestPriceSensor(coordinator, entry, fuel_type)

    def test_native_value_returns_cheapest_price(self):
        sensor = self._sensor()
        assert sensor.native_value == pytest.approx(1.800)

    def test_native_value_none_when_no_data(self):
        coordinator = MagicMock()
        coordinator.data = None
        entry = _make_config_entry()
        sensor = CheapestPriceSensor(coordinator, entry, "Benzina")
        assert sensor.native_value is None

    def test_unit_of_measurement_benzina(self):
        sensor = self._sensor("Benzina")
        assert sensor._attr_native_unit_of_measurement == "EUR/L"

    def test_unit_of_measurement_metano(self):
        sensor = self._sensor("Metano")
        # Metano uses EUR/kg
        assert sensor._attr_native_unit_of_measurement == "EUR/kg"

    def test_extra_state_attributes_has_station_name(self):
        sensor = self._sensor()
        attrs = sensor.extra_state_attributes
        assert "station_name" in attrs
        assert attrs["station_name"] == "Stazione 1"

    def test_extra_state_attributes_has_top_stations_list(self):
        sensor = self._sensor()
        attrs = sensor.extra_state_attributes
        assert "top_stations" in attrs
        assert isinstance(attrs["top_stations"], list)
        assert len(attrs["top_stations"]) == 3

    def test_extra_state_attributes_empty_when_no_station(self):
        coordinator = MagicMock()
        area = FuelAreaData(fuel_type="Benzina")
        area.cheapest_price = None
        area.cheapest_station = None
        coordinator.data = CoordinatorData(
            by_fuel={"Benzina": area},
            last_updated=datetime(2024, 1, 15, tzinfo=timezone.utc),
            station_count_in_radius=0,
        )
        entry = _make_config_entry()
        sensor = CheapestPriceSensor(coordinator, entry, "Benzina")
        assert sensor.extra_state_attributes == {}

    def test_unique_id_format(self):
        sensor = self._sensor()
        assert "test_entry" in sensor._attr_unique_id
        assert "Benzina" in sensor._attr_unique_id
        assert "cheapest" in sensor._attr_unique_id


# ---------------------------------------------------------------------------
# AveragePriceSensor
# ---------------------------------------------------------------------------

class TestAveragePriceSensor:
    def _sensor(self, fuel_type: str = "Benzina") -> AveragePriceSensor:
        coordinator = _make_coordinator_with_data(fuel_type)
        entry = _make_config_entry()
        return AveragePriceSensor(coordinator, entry, fuel_type)

    def test_native_value_returns_average(self):
        sensor = self._sensor()
        assert sensor.native_value == pytest.approx(1.850)

    def test_native_value_none_when_no_data(self):
        coordinator = MagicMock()
        coordinator.data = None
        entry = _make_config_entry()
        sensor = AveragePriceSensor(coordinator, entry, "Benzina")
        assert sensor.native_value is None

    def test_unique_id_format(self):
        sensor = self._sensor()
        assert "average" in sensor._attr_unique_id


# ---------------------------------------------------------------------------
# FavoriteStationSensor
# ---------------------------------------------------------------------------

class TestFavoriteStationSensor:
    def _sensor(self, rank: int = 1, fuel_type: str = "Benzina", top_n: int = 3) -> FavoriteStationSensor:
        coordinator = _make_coordinator_with_data(fuel_type, top_n=top_n)
        entry = _make_config_entry()
        return FavoriteStationSensor(coordinator, entry, fuel_type, rank)

    def test_rank_1_returns_cheapest_price(self):
        sensor = self._sensor(rank=1)
        assert sensor.native_value == pytest.approx(1.800)

    def test_rank_2_returns_second_cheapest(self):
        sensor = self._sensor(rank=2)
        assert sensor.native_value == pytest.approx(1.810)

    def test_rank_3_returns_third_cheapest(self):
        sensor = self._sensor(rank=3)
        assert sensor.native_value == pytest.approx(1.820)

    def test_rank_beyond_available_returns_none(self):
        sensor = self._sensor(rank=5, top_n=3)
        assert sensor.native_value is None

    def test_available_false_when_rank_exceeds_stations(self):
        sensor = self._sensor(rank=5, top_n=3)
        assert sensor.available is False

    def test_available_true_for_valid_rank(self):
        sensor = self._sensor(rank=1, top_n=3)
        assert sensor.available is True

    def test_extra_state_attributes_has_rank(self):
        sensor = self._sensor(rank=2)
        attrs = sensor.extra_state_attributes
        assert attrs["rank"] == 2

    def test_extra_state_attributes_has_station_name(self):
        sensor = self._sensor(rank=1)
        attrs = sensor.extra_state_attributes
        assert attrs["station_name"] == "Stazione 1"

    def test_extra_state_attributes_rank_2_has_correct_station(self):
        sensor = self._sensor(rank=2)
        attrs = sensor.extra_state_attributes
        assert attrs["station_name"] == "Stazione 2"

    def test_extra_state_attributes_empty_when_rank_exceeds(self):
        sensor = self._sensor(rank=5, top_n=3)
        assert sensor.extra_state_attributes == {}

    def test_extra_state_attributes_has_distance(self):
        sensor = self._sensor(rank=1)
        attrs = sensor.extra_state_attributes
        assert "distance_km" in attrs
        assert attrs["distance_km"] == pytest.approx(1.0)

    def test_extra_state_attributes_has_data_source(self):
        sensor = self._sensor(rank=1)
        attrs = sensor.extra_state_attributes
        assert "data_source" in attrs
        assert attrs["data_source"] == "mimit_csv"

    def test_unique_id_includes_rank(self):
        sensor = self._sensor(rank=3)
        assert "3" in sensor._attr_unique_id
        assert "station" in sensor._attr_unique_id

    def test_unique_ids_differ_per_rank(self):
        coordinator = _make_coordinator_with_data(top_n=3)
        entry = _make_config_entry()
        s1 = FavoriteStationSensor(coordinator, entry, "Benzina", 1)
        s2 = FavoriteStationSensor(coordinator, entry, "Benzina", 2)
        assert s1._attr_unique_id != s2._attr_unique_id

    def test_unique_ids_differ_per_fuel_type(self):
        coordinator = MagicMock()
        coordinator.data = None
        entry = _make_config_entry()
        s_benzina = FavoriteStationSensor(coordinator, entry, "Benzina", 1)
        s_gasolio = FavoriteStationSensor(coordinator, entry, "Gasolio", 1)
        assert s_benzina._attr_unique_id != s_gasolio._attr_unique_id

    def test_translation_placeholders_contain_rank_and_fuel(self):
        sensor = self._sensor(rank=2, fuel_type="Gasolio")
        placeholders = sensor._attr_translation_placeholders
        assert placeholders["rank"] == "2"
        assert placeholders["fuel_type"] == "Gasolio"

    def test_native_value_none_when_no_coordinator_data(self):
        coordinator = MagicMock()
        coordinator.data = None
        entry = _make_config_entry()
        sensor = FavoriteStationSensor(coordinator, entry, "Benzina", 1)
        assert sensor.native_value is None
