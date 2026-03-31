"""Sensor entities for Carburanti MIMIT integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AI_PROVIDER_NONE,
    CONF_AI_API_KEY,
    CONF_AI_PROVIDER,
    CONF_FUEL_TYPES,
    DEFAULT_FUEL_TYPES,
    FUEL_UNITS,
    SENSOR_AVERAGE,
    SENSOR_CHEAPEST,
    SENSOR_PREDICTION,
    SENSOR_TREND,
)
from .coordinator import CarburantiMimitCoordinator, FuelAreaData
from .entity import CarburantiMimitEntity
from .prediction import PredictionResult, async_ai_prediction, compute_prediction

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all sensor entities for this config entry."""
    coordinator: CarburantiMimitCoordinator = entry.runtime_data.coordinator
    fuel_types: list[str] = entry.options.get(CONF_FUEL_TYPES, DEFAULT_FUEL_TYPES)

    entities: list[SensorEntity] = []
    for fuel_type in fuel_types:
        entities.append(CheapestPriceSensor(coordinator, entry, fuel_type))
        entities.append(AveragePriceSensor(coordinator, entry, fuel_type))
        entities.append(PriceTrendSensor(coordinator, entry, fuel_type))
        entities.append(PricePredictionSensor(coordinator, entry, fuel_type))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _area(coordinator: CarburantiMimitCoordinator, fuel_type: str) -> FuelAreaData | None:
    """Return FuelAreaData for fuel_type from coordinator, or None if unavailable."""
    if coordinator.data is None:
        return None
    return coordinator.data.by_fuel.get(fuel_type)


# ---------------------------------------------------------------------------
# Sensor classes
# ---------------------------------------------------------------------------

class CheapestPriceSensor(CarburantiMimitEntity, SensorEntity):
    """Cheapest price for a fuel type in the configured radius.

    State  → cheapest price (float, EUR/L or EUR/kg for Metano)
    Attributes → station details + top-N list
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: CarburantiMimitCoordinator,
        config_entry: ConfigEntry,
        fuel_type: str,
    ) -> None:
        super().__init__(coordinator, config_entry, fuel_type)
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_CHEAPEST}"
        self._attr_translation_key = SENSOR_CHEAPEST
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._attr_native_unit_of_measurement = FUEL_UNITS.get(fuel_type, "EUR/L")

    @property
    def native_value(self) -> float | None:
        area = _area(self.coordinator, self._fuel_type)
        return area.cheapest_price if area else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        area = _area(self.coordinator, self._fuel_type)
        if not area or not area.cheapest_station:
            return {}
        s = area.cheapest_station
        return {
            "station_name": s.station.nome,
            "address": s.station.indirizzo,
            "comune": s.station.comune,
            "provincia": s.station.provincia,
            "bandiera": s.station.bandiera,
            "distance_km": round(s.distance_km, 2),
            "is_self_service": s.is_self,
            "reported_at": s.reported_at.isoformat(),
            "self_service_cheapest": area.self_cheapest_price,
            "full_service_cheapest": area.servito_cheapest_price,
            "stations_in_radius": area.station_count,
            "top_stations": [st.to_dict() for st in area.top_stations],
            "last_updated": (
                self.coordinator.data.last_updated.isoformat()
                if self.coordinator.data
                else None
            ),
        }


class AveragePriceSensor(CarburantiMimitEntity, SensorEntity):
    """Average price for a fuel type in the configured radius."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: CarburantiMimitCoordinator,
        config_entry: ConfigEntry,
        fuel_type: str,
    ) -> None:
        super().__init__(coordinator, config_entry, fuel_type)
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_AVERAGE}"
        self._attr_translation_key = SENSOR_AVERAGE
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._attr_native_unit_of_measurement = FUEL_UNITS.get(fuel_type, "EUR/L")

    @property
    def native_value(self) -> float | None:
        area = _area(self.coordinator, self._fuel_type)
        return area.average_price if area else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        area = _area(self.coordinator, self._fuel_type)
        if not area:
            return {}
        return {"stations_in_radius": area.station_count}


class PriceTrendSensor(CarburantiMimitEntity, SensorEntity):
    """Price trend indicator: 'up', 'down', or 'stable'.

    State  → trend string
    Attributes → weekly/monthly change percentages
    """

    _attr_state_class = None
    _attr_device_class = None

    def __init__(
        self,
        coordinator: CarburantiMimitCoordinator,
        config_entry: ConfigEntry,
        fuel_type: str,
    ) -> None:
        super().__init__(coordinator, config_entry, fuel_type)
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_TREND}"
        self._attr_translation_key = SENSOR_TREND
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._prediction: PredictionResult | None = None

    @property
    def native_value(self) -> str | None:
        if self._prediction is None:
            return None
        return self._prediction.trend_direction

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._prediction:
            return {}
        return {
            "weekly_change_pct": self._prediction.weekly_change_pct,
            "monthly_change_pct": self._prediction.monthly_change_pct,
            "trend_pct_7d": self._prediction.trend_pct_7d,
        }

    def _handle_coordinator_update(self) -> None:
        """Recompute prediction on coordinator data refresh."""
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        self._prediction = compute_prediction(history, self._fuel_type)
        super()._handle_coordinator_update()


class PricePredictionSensor(CarburantiMimitEntity, SensorEntity):
    """Predicted price for tomorrow based on 30-day history.

    State  → tomorrow's predicted price (float)
    Attributes → 7-day forecast, confidence, method, optional AI analysis
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: CarburantiMimitCoordinator,
        config_entry: ConfigEntry,
        fuel_type: str,
    ) -> None:
        super().__init__(coordinator, config_entry, fuel_type)
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_PREDICTION}"
        self._attr_translation_key = SENSOR_PREDICTION
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._attr_native_unit_of_measurement = FUEL_UNITS.get(fuel_type, "EUR/L")
        self._prediction: PredictionResult | None = None
        self._ai_analysis: str | None = None

    @property
    def available(self) -> bool:
        """Sensor is unavailable until at least 7 days of history exist."""
        return self._prediction is not None

    @property
    def native_value(self) -> float | None:
        if not self._prediction:
            return None
        prices = self._prediction.predicted_prices
        return prices[0] if prices else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._prediction:
            return {}
        return {
            "predicted_7d": self._prediction.predicted_prices,
            "confidence": self._prediction.confidence,
            "method": self._prediction.method_used,
            "trend_direction": self._prediction.trend_direction,
            "trend_pct_7d": self._prediction.trend_pct_7d,
            "ai_analysis": self._ai_analysis,
        }

    def _handle_coordinator_update(self) -> None:
        """Recompute prediction and optionally call AI on coordinator refresh."""
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        self._prediction = compute_prediction(history, self._fuel_type)

        # Trigger async AI enrichment (fire-and-forget; result stored in next update)
        ai_provider = self._config_entry.options.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE)
        ai_key = self._config_entry.options.get(CONF_AI_API_KEY, "")
        if (
            self._prediction is not None
            and ai_provider != AI_PROVIDER_NONE
            and ai_key
        ):
            self.hass.async_create_task(self._async_fetch_ai_analysis(ai_provider, ai_key, history))

        super()._handle_coordinator_update()

    async def _async_fetch_ai_analysis(
        self,
        provider: str,
        api_key: str,
        history: list,
    ) -> None:
        """Fetch AI analysis and trigger a state write."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        result = await async_ai_prediction(
            session,
            provider,
            api_key,
            history,
            self._fuel_type,
            self._prediction,  # type: ignore[arg-type]
        )
        if result != self._ai_analysis:
            self._ai_analysis = result
            self.async_write_ha_state()
