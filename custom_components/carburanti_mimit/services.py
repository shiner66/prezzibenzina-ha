"""Custom HA service calls for Carburanti MIMIT integration."""
from __future__ import annotations

import logging
from datetime import timezone
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MimitApiClient
from .const import ALL_FUEL_TYPES, CONF_LATITUDE, CONF_LONGITUDE, DOMAIN
from .geo import filter_by_radius
from .parser import merge_prices_with_registry, parse_prices_csv, parse_registry_csv

_LOGGER = logging.getLogger(__name__)

SERVICE_FORCE_REFRESH = "force_refresh"
SERVICE_GET_CHEAPEST_NEAR = "get_cheapest_near"
SERVICE_CLEAR_HISTORY = "clear_history"

_SCHEMA_FORCE_REFRESH = vol.Schema(
    {
        vol.Optional("entry_id"): str,
    }
)

_SCHEMA_GET_CHEAPEST_NEAR = vol.Schema(
    {
        vol.Required("latitude"): vol.Coerce(float),
        vol.Required("longitude"): vol.Coerce(float),
        vol.Required("fuel_type"): vol.In(ALL_FUEL_TYPES),
        vol.Optional("radius_km", default=5): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=100)),
        vol.Optional("top_n", default=3): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
    }
)

_SCHEMA_CLEAR_HISTORY = vol.Schema(
    {
        vol.Required("entry_id"): str,
        vol.Optional("fuel_type"): vol.In(ALL_FUEL_TYPES),
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Register all custom services. Safe to call multiple times (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_REFRESH):
        return  # Already registered (can happen with multiple config entries)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_REFRESH,
        _make_force_refresh_handler(hass),
        schema=_SCHEMA_FORCE_REFRESH,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CHEAPEST_NEAR,
        _make_get_cheapest_near_handler(hass),
        schema=_SCHEMA_GET_CHEAPEST_NEAR,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_HISTORY,
        _make_clear_history_handler(hass),
        schema=_SCHEMA_CLEAR_HISTORY,
    )

    _LOGGER.debug("Registered %s services", DOMAIN)


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove all custom services (called when last entry is unloaded)."""
    for service in (SERVICE_FORCE_REFRESH, SERVICE_GET_CHEAPEST_NEAR, SERVICE_CLEAR_HISTORY):
        hass.services.async_remove(DOMAIN, service)


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------

def _make_force_refresh_handler(hass: HomeAssistant):
    async def _handle(call: ServiceCall) -> None:
        """Trigger an immediate coordinator refresh.

        If ``entry_id`` is given, only refresh that entry.
        Otherwise refresh all Carburanti MIMIT entries.
        """
        target_id: str | None = call.data.get("entry_id")
        entries = hass.config_entries.async_entries(DOMAIN)

        for entry in entries:
            if target_id and entry.entry_id != target_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if runtime is None:
                continue
            coordinator = getattr(runtime, "coordinator", None)
            if coordinator is not None:
                await coordinator.async_request_refresh()
                _LOGGER.debug("Force-refreshed coordinator for entry %s", entry.entry_id)

    return _handle


def _make_get_cheapest_near_handler(hass: HomeAssistant):
    async def _handle(call: ServiceCall) -> ServiceResponse:
        """Return the cheapest stations near a given coordinate.

        Reuses cached CSV data from an existing coordinator if available;
        otherwise fetches fresh from MIMIT.

        Returns a dict suitable for use with ``response_variable`` in automations.
        """
        lat: float = call.data["latitude"]
        lon: float = call.data["longitude"]
        fuel_type: str = call.data["fuel_type"]
        radius_km: float = call.data["radius_km"]
        top_n: int = call.data["top_n"]

        # Try to reuse an existing coordinator's cached registry + last prices fetch
        registry = None
        prices_csv: str | None = None
        data_age_seconds: int = 0

        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            runtime = getattr(entry, "runtime_data", None)
            if runtime is None:
                continue
            coord = getattr(runtime, "coordinator", None)
            if coord is None:
                continue
            # Borrow the registry cache
            if coord._registry_cache is not None:
                registry = coord._registry_cache
            # Calculate age of last coordinator data
            if coord.data is not None:
                age = (
                    __import__("datetime").datetime.now(timezone.utc)
                    - coord.data.last_updated
                ).total_seconds()
                data_age_seconds = int(age)
            break

        # Fetch fresh prices CSV (always, since we need current prices)
        session = async_get_clientsession(hass)
        client = MimitApiClient(session)

        try:
            prices_csv = await client.async_fetch_prices_csv()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("get_cheapest_near: could not fetch prices: %s", exc)
            return {"fuel_type": fuel_type, "results": [], "error": str(exc)}

        if registry is None:
            try:
                registry_csv = await client.async_fetch_registry_csv()
                registry = parse_registry_csv(registry_csv)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("get_cheapest_near: could not fetch registry: %s", exc)
                return {"fuel_type": fuel_type, "results": [], "error": str(exc)}

        from .parser import parse_prices_csv as _parse_prices

        prices = _parse_prices(prices_csv)
        enriched = merge_prices_with_registry(prices, registry)
        local = filter_by_radius(enriched, lat, lon, radius_km)

        # Filter by fuel type and sort by price
        filtered = [s for s in local if s.fuel_type == fuel_type]
        filtered.sort(key=lambda s: s.price)
        results = [s.to_dict() for s in filtered[:top_n]]

        return {
            "fuel_type": fuel_type,
            "results": results,
            "data_age_seconds": data_age_seconds,
        }

    return _handle


def _make_clear_history_handler(hass: HomeAssistant):
    async def _handle(call: ServiceCall) -> None:
        """Clear the local JSON history for a given config entry."""
        entry_id: str = call.data["entry_id"]
        fuel_type: str | None = call.data.get("fuel_type")

        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id != entry_id:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if runtime is None:
                break
            storage = getattr(runtime, "storage", None)
            if storage is not None:
                await storage.async_clear(fuel_type)
                _LOGGER.info(
                    "Cleared history for entry %s fuel_type=%s",
                    entry_id,
                    fuel_type or "ALL",
                )
            break

    return _handle
