"""Sensor entities for Carburanti MIMIT integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    AI_PROVIDER_NONE,
    CONF_AI_API_KEY,
    CONF_AI_PROVIDER,
    CONF_FUEL_TYPES,
    DEFAULT_FUEL_TYPES,
    FUEL_UNITS,
    SENSOR_AI_INSIGHT,
    SENSOR_AVERAGE,
    SENSOR_CHEAPEST,
    SENSOR_PREDICTION,
    SENSOR_PREDICTION_3D,
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
        pred = PricePredictionSensor(coordinator, entry, fuel_type)
        insight = PriceAIInsightSensor(coordinator, entry, fuel_type)
        pred._peer_ai_insight = insight  # direct reference, no dispatcher needed
        entities.append(pred)
        entities.append(PricePrediction3dSensor(coordinator, entry, fuel_type))
        entities.append(insight)

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
            "data_source": (
                self.coordinator.data.data_source
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
        attrs: dict[str, Any] = {"stations_in_radius": area.station_count}
        if area.national_average is not None:
            attrs["national_average"] = area.national_average
            if area.average_price is not None:
                attrs["vs_national_pct"] = round(
                    (area.average_price - area.national_average) / area.national_average * 100, 2
                )
        if self.coordinator.data:
            attrs["data_source"] = self.coordinator.data.data_source
        return attrs


class PriceTrendSensor(CarburantiMimitEntity, SensorEntity):
    """Price trend indicator: 'up', 'down', or 'stable'.

    State  → trend string
    Attributes → weekly/monthly change percentages + statistical indicators
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
            "price_volatility": self._prediction.price_volatility,
            "price_momentum": self._prediction.price_momentum,
            "price_acceleration": self._prediction.price_acceleration,
        }

    async def async_added_to_hass(self) -> None:
        """Compute prediction from existing history at startup, before first coordinator update."""
        await super().async_added_to_hass()
        if self._prediction is None:
            history = self.coordinator._storage.get_history(self._fuel_type, days=30)
            self._prediction = compute_prediction(history, self._fuel_type)

    def _handle_coordinator_update(self) -> None:
        """Recompute prediction on coordinator data refresh."""
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        self._prediction = compute_prediction(history, self._fuel_type)
        super()._handle_coordinator_update()


class PricePredictionSensor(CarburantiMimitEntity, RestoreEntity, SensorEntity):
    """Predicted price for tomorrow based on 30-day history.

    State  → tomorrow's predicted price (float)
    Attributes → 7-day forecast, confidence, method, geopolitical AI analysis

    Inherits RestoreEntity so that ai_analysis and ai_risk_level survive HA
    restarts without waiting for the next coordinator update (08:15).
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
        self._ai_risk_level: str | None = None
        self._ai_price_3d: float | None = None
        self._ai_brief: str | None = None
        self._peer_ai_insight: PriceAIInsightSensor | None = None

    @property
    def available(self) -> bool:
        """Sensor is available as soon as the coordinator has a current price.

        Statistical prediction requires ≥3 days of history; until then
        native_value is None but AI analysis attributes may already be populated.
        """
        if self.coordinator.data is None:
            return False
        area = _area(self.coordinator, self._fuel_type)
        return area is not None and area.cheapest_price is not None

    @property
    def native_value(self) -> float | None:
        if not self._prediction:
            return None
        prices = self._prediction.predicted_prices
        return prices[0] if prices else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "ai_analysis": self._ai_analysis,
            "ai_risk_level": self._ai_risk_level,
            "ai_brief": self._ai_brief,
            "ai_predicted_price_3d": self._ai_price_3d,
        }
        if self._prediction:
            attrs.update({
                "predicted_7d": self._prediction.predicted_prices,
                "predicted_price_3d": self._prediction.predicted_price_3d,
                "confidence": self._prediction.confidence,
                "method": self._prediction.method_used,
                "trend_direction": self._prediction.trend_direction,
                "trend_pct_7d": self._prediction.trend_pct_7d,
                "price_volatility": self._prediction.price_volatility,
                "price_momentum": self._prediction.price_momentum,
                "price_acceleration": self._prediction.price_acceleration,
            })
        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore AI analysis from last state, then compute statistical prediction."""
        await super().async_added_to_hass()

        # Restore AI analysis from HA state machine so it shows immediately
        # on restart without waiting for the next coordinator update.
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            self._ai_analysis = last_state.attributes.get("ai_analysis")
            self._ai_risk_level = last_state.attributes.get("ai_risk_level")
            self._ai_brief = last_state.attributes.get("ai_brief")
            ai_3d = last_state.attributes.get("ai_predicted_price_3d")
            if ai_3d is not None:
                try:
                    self._ai_price_3d = float(ai_3d)
                except (ValueError, TypeError):
                    pass

        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        if self._prediction is None:
            self._prediction = compute_prediction(history, self._fuel_type)

        # If AI is configured but no cached analysis exists (e.g. first boot after
        # installing the integration or enabling AI), fire an immediate background call
        # rather than waiting for the next scheduled coordinator update.
        ai_provider = self._config_entry.options.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE)
        ai_key = self._config_entry.options.get(CONF_AI_API_KEY, "")
        _LOGGER.error(
            "DIAG async_added_to_hass %s — provider=%r key_set=%s ai_analysis_none=%s",
            self._fuel_type,
            ai_provider,
            bool(ai_key),
            self._ai_analysis is None,
        )
        if ai_provider != AI_PROVIDER_NONE and ai_key and self._ai_analysis is None:
            area = _area(self.coordinator, self._fuel_type)
            self.hass.async_create_task(
                self._async_fetch_ai_analysis(
                    ai_provider,
                    ai_key,
                    history,
                    area.cheapest_price if area else None,
                    area.national_average if area else None,
                )
            )

    def _handle_coordinator_update(self) -> None:
        """Recompute prediction and optionally call AI on coordinator refresh."""
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        self._prediction = compute_prediction(history, self._fuel_type)

        ai_provider = self._config_entry.options.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE)
        ai_key = self._config_entry.options.get(CONF_AI_API_KEY, "")
        if ai_provider != AI_PROVIDER_NONE and ai_key:
            area = _area(self.coordinator, self._fuel_type)
            current_price = area.cheapest_price if area else None
            national_average = area.national_average if area else None
            self.hass.async_create_task(
                self._async_fetch_ai_analysis(
                    ai_provider, ai_key, history, current_price, national_average
                )
            )

        super()._handle_coordinator_update()

    async def _async_fetch_ai_analysis(
        self,
        provider: str,
        api_key: str,
        history: list,
        current_price: float | None = None,
        national_average: float | None = None,
    ) -> None:
        """Fetch AI geopolitical analysis and trigger a state write."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        analysis, risk_level, price_3d, brief = await async_ai_prediction(
            session,
            provider,
            api_key,
            history,
            self._fuel_type,
            self._prediction,
            current_price=current_price,
            national_average=national_average,
        )
        _LOGGER.error(
            "DIAG AI fetch done for %s — analysis=%s risk=%s brief=%s peer=%s",
            self._fuel_type,
            "yes" if analysis else "None",
            risk_level,
            brief,
            "yes" if self._peer_ai_insight is not None else "None",
        )
        if (analysis != self._ai_analysis
                or risk_level != self._ai_risk_level
                or price_3d != self._ai_price_3d
                or brief != self._ai_brief):
            self._ai_analysis = analysis
            self._ai_risk_level = risk_level
            self._ai_price_3d = price_3d
            self._ai_brief = brief
            # Push results directly to peer AI insight sensor
            if self._peer_ai_insight is not None:
                pred = self._prediction
                self._peer_ai_insight.update_from_ai(
                    analysis=analysis,
                    risk_level=risk_level,
                    price_3d=price_3d,
                    brief=brief,
                    price_tomorrow=(pred.predicted_prices[0] if pred and pred.predicted_prices else None),
                    confidence=(pred.confidence if pred else None),
                )
            self.async_write_ha_state()


# ---------------------------------------------------------------------------
# New sensors: 3-day statistical forecast and AI insight
# ---------------------------------------------------------------------------

class PricePrediction3dSensor(CarburantiMimitEntity, SensorEntity):
    """3-day statistical price forecast.

    State  → predicted price in 3 days (float, EUR/L)
    Attributes → confidence, method, tomorrow's statistical forecast, AI 3-day estimate
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
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_PREDICTION_3D}"
        self._attr_translation_key = SENSOR_PREDICTION_3D
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._attr_native_unit_of_measurement = FUEL_UNITS.get(fuel_type, "EUR/L")
        self._prediction: PredictionResult | None = None

    @property
    def native_value(self) -> float | None:
        if not self._prediction:
            return None
        return self._prediction.predicted_price_3d

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._prediction:
            attrs["confidence"] = self._prediction.confidence
            attrs["method"] = self._prediction.method_used
            attrs["statistical_prediction_tomorrow"] = (
                self._prediction.predicted_prices[0]
                if self._prediction.predicted_prices
                else None
            )
        ai_data = self.coordinator.ai_cache.get(self._fuel_type, {})
        if ai_data.get("price_3d") is not None:
            attrs["ai_predicted_price_3d"] = ai_data["price_3d"]
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._prediction is None:
            history = self.coordinator._storage.get_history(self._fuel_type, days=30)
            self._prediction = compute_prediction(history, self._fuel_type)

    def _handle_coordinator_update(self) -> None:
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        self._prediction = compute_prediction(history, self._fuel_type)
        super()._handle_coordinator_update()


class PriceAIInsightSensor(CarburantiMimitEntity, RestoreEntity, SensorEntity):
    """AI one-sentence summary as sensor state.

    State  → brief AI summary (e.g. "Prezzi in calo per eccesso offerta OPEC+")
    Attributes → risk level, statistical confidence, tomorrow/3d price estimates, full analysis

    Inherits RestoreEntity so the last AI brief survives HA restarts.
    Updated directly by PricePredictionSensor.update_from_ai() after each AI call.
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
        self._attr_unique_id = f"{config_entry.entry_id}_{fuel_type}_{SENSOR_AI_INSIGHT}"
        self._attr_translation_key = SENSOR_AI_INSIGHT
        self._attr_translation_placeholders = {"fuel_type": fuel_type}
        self._ai_brief: str | None = None
        self._ai_risk_level: str | None = None
        self._ai_analysis: str | None = None
        self._ai_price_tomorrow: float | None = None
        self._ai_price_3d: float | None = None
        self._statistical_confidence: str | None = None

    @property
    def native_value(self) -> str | None:
        if self._ai_brief:
            return self._ai_brief
        if self._ai_risk_level:
            return f"Rischio {self._ai_risk_level}"
        # Last-resort fallback: if we got analysis text but tags were not parsed
        if self._ai_analysis:
            return self._ai_analysis[:80].replace("\n", " ").strip()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ai_risk_level": self._ai_risk_level,
            "statistical_confidence": self._statistical_confidence,
            "ai_predicted_tomorrow": self._ai_price_tomorrow,
            "ai_predicted_3d": self._ai_price_3d,
            "full_analysis": self._ai_analysis,
        }

    async def async_added_to_hass(self) -> None:
        """Restore last AI state on HA restart."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            if last_state.state not in ("unknown", "unavailable", None):
                self._ai_brief = last_state.state
            self._ai_risk_level = last_state.attributes.get("ai_risk_level")
            self._ai_analysis = last_state.attributes.get("full_analysis")
            self._statistical_confidence = last_state.attributes.get("statistical_confidence")
            for attr, key in [
                ("_ai_price_tomorrow", "ai_predicted_tomorrow"),
                ("_ai_price_3d", "ai_predicted_3d"),
            ]:
                val = last_state.attributes.get(key)
                if val is not None:
                    try:
                        setattr(self, attr, float(val))
                    except (ValueError, TypeError):
                        pass

        # Populate statistical confidence from current history immediately
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        pred = compute_prediction(history, self._fuel_type)
        if pred:
            self._statistical_confidence = pred.confidence

    def update_from_ai(
        self,
        analysis: str | None,
        risk_level: str | None,
        price_3d: float | None,
        brief: str | None,
        price_tomorrow: float | None,
        confidence: str | None,
    ) -> None:
        """Called directly by PricePredictionSensor after a successful AI fetch."""
        _LOGGER.error(
            "DIAG update_from_ai called for %s — brief=%s risk=%s",
            self._fuel_type, brief, risk_level,
        )
        self._ai_analysis = analysis
        self._ai_risk_level = risk_level
        self._ai_price_3d = price_3d
        self._ai_brief = brief
        self._ai_price_tomorrow = price_tomorrow
        self._statistical_confidence = confidence
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Update statistical confidence on coordinator refresh."""
        history = self.coordinator._storage.get_history(self._fuel_type, days=30)
        pred = compute_prediction(history, self._fuel_type)
        if pred:
            self._statistical_confidence = pred.confidence
        super()._handle_coordinator_update()
