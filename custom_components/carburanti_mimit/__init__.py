"""Carburanti MIMIT — Home Assistant integration.

Fetches Italian fuel price data from the MIMIT open-data portal,
ranks cheapest stations within a configurable radius, tracks price
history, and predicts future trends.

Data source: Ministero delle Imprese e del Made in Italy (MIMIT)
License: IODL 2.0
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MimitApiClient
from .const import DOMAIN, PLATFORMS
from .coordinator import CarburantiMimitCoordinator
from .pb_api import PrezzibenzinaClient
from .services import async_register_services, async_unregister_services
from .storage import HistoryStorage

_LOGGER = logging.getLogger(__name__)


@dataclass
class CarburantiMimitRuntimeData:
    """Objects shared across all platforms for a single config entry."""

    coordinator: CarburantiMimitCoordinator
    client: MimitApiClient
    storage: HistoryStorage
    pb_client: PrezzibenzinaClient


# Type alias for typed config entries
type CarburantiMimitConfigEntry = ConfigEntry[CarburantiMimitRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CarburantiMimitConfigEntry,
) -> bool:
    """Set up a Carburanti MIMIT config entry."""
    session = async_get_clientsession(hass)
    client = MimitApiClient(session)
    pb_client = PrezzibenzinaClient(session)

    storage = HistoryStorage(hass, entry.entry_id)
    await storage.async_load()

    coordinator = CarburantiMimitCoordinator(hass, entry, client, storage, pb_client)

    # Initial data fetch — raises ConfigEntryNotReady on failure
    await coordinator.async_config_entry_first_refresh()

    # Store runtime objects on the entry itself (HA 2024.x pattern)
    entry.runtime_data = CarburantiMimitRuntimeData(
        coordinator=coordinator,
        client=client,
        storage=storage,
        pb_client=pb_client,
    )

    # Forward to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services (idempotent — only registers once)
    async_register_services(hass)

    # Re-create coordinator when options change
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Start the 08:15 Europe/Rome daily trigger
    coordinator.schedule_daily_refresh()
    # Start the 14:30 Europe/Rome intraday spot-check via ospzApi
    coordinator.schedule_intraday_refresh()
    # Start PrezzibenzinaIT spot-checks (10:30 and 13:30 Europe/Rome)
    coordinator.schedule_pb_intraday_refreshes()

    _LOGGER.info(
        "Carburanti MIMIT entry '%s' (%s) set up successfully",
        entry.title,
        entry.entry_id,
    )
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: CarburantiMimitConfigEntry,
) -> bool:
    """Unload a Carburanti MIMIT config entry."""
    # Cancel scheduled time triggers
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        runtime.coordinator.cancel_daily_refresh()
        runtime.coordinator.cancel_intraday_refresh()
        runtime.coordinator.cancel_pb_intraday_refreshes()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Unregister services only when no more entries remain
    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state.recoverable
    ]
    if not remaining:
        async_unregister_services(hass)

    return unloaded


async def _async_update_listener(
    hass: HomeAssistant,
    entry: CarburantiMimitConfigEntry,
) -> None:
    """Reload the entry when options are changed via the UI."""
    await hass.config_entries.async_reload(entry.entry_id)
