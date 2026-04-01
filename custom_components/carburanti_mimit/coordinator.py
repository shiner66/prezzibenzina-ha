"""DataUpdateCoordinator for Carburanti MIMIT."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import event as ha_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MimitApiClient
from .const import (
    CONF_FUEL_TYPES,
    CONF_INCLUDE_SELF,
    CONF_INCLUDE_SERVITO,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_RADIUS_KM,
    CONF_TOP_N,
    CONF_UPDATE_INTERVAL_H,
    DEFAULT_FUEL_TYPES,
    DEFAULT_INCLUDE_SELF,
    DEFAULT_INCLUDE_SERVITO,
    DEFAULT_RADIUS_KM,
    DEFAULT_TOP_N,
    DEFAULT_UPDATE_INTERVAL_H,
    MIMIT_INTRADAY_HOUR,
    MIMIT_INTRADAY_MINUTE,
    MIMIT_UPDATE_HOUR,
    MIMIT_UPDATE_MINUTE,
    REGISTRY_CACHE_DAYS,
)
from .geo import filter_by_radius
from .parser import (
    EnrichedStation,
    Station,
    merge_prices_with_registry,
    parse_ospzapi_distributori,
    parse_prices_csv,
    parse_regional_csv,
    parse_registry_csv,
)
from .statistics_helper import async_push_price_statistics

if TYPE_CHECKING:
    from .storage import HistoryStorage

_LOGGER = logging.getLogger(__name__)


@dataclass
class FuelAreaData:
    """Aggregated results for one fuel type in the search area."""

    fuel_type: str
    cheapest_price: float | None = None
    cheapest_station: EnrichedStation | None = None
    top_stations: list[EnrichedStation] = field(default_factory=list)
    average_price: float | None = None
    self_cheapest_price: float | None = None
    servito_cheapest_price: float | None = None
    station_count: int = 0
    national_average: float | None = None  # from MediaRegionaleStradale.csv


@dataclass
class CoordinatorData:
    """Full coordinator payload."""

    by_fuel: dict[str, FuelAreaData]
    last_updated: datetime
    station_count_in_radius: int
    data_source: str = "csv_morning"  # "csv_morning" | "ospzapi_intraday"
    national_averages: dict[str, float] = field(default_factory=dict)


class CarburantiMimitCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Fetches and processes MIMIT fuel price data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: MimitApiClient,
        storage: HistoryStorage,
    ) -> None:
        interval_h = config_entry.options.get(CONF_UPDATE_INTERVAL_H, DEFAULT_UPDATE_INTERVAL_H)
        super().__init__(
            hass,
            _LOGGER,
            name=f"carburanti_mimit_{config_entry.entry_id[:8]}",
            update_interval=timedelta(hours=interval_h),
        )
        self._client = client
        self._storage = storage
        self._config_entry = config_entry

        # Registry cache (refreshed at most every REGISTRY_CACHE_DAYS)
        self._registry_cache: dict[int, Station] | None = None
        self._registry_fetched_at: datetime | None = None

        # Schedule daily refresh at MIMIT publish time (08:15 Europe/Rome)
        self._unsub_daily: Callable[[], None] | None = None
        # Schedule intraday spot-check via ospzApi (14:30 Europe/Rome)
        self._unsub_intraday: Callable[[], None] | None = None
        # Shared AI results cache: fuel_type → {analysis, risk_level, price_3d, brief, price_tomorrow, confidence}
        self.ai_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def schedule_daily_refresh(self) -> None:
        """Register a time-based trigger that fires at 08:15 Europe/Rome.

        This complements the interval-based refresh so that HA always picks
        up the new data shortly after MIMIT publishes it, regardless of
        when HA was last restarted.
        """
        if self._unsub_daily is not None:
            self._unsub_daily()

        tz = dt_util.get_time_zone("Europe/Rome")
        target_time = datetime.now(tz).replace(
            hour=MIMIT_UPDATE_HOUR,
            minute=MIMIT_UPDATE_MINUTE,
            second=0,
            microsecond=0,
        )

        @callback
        def _on_daily_time(_now: datetime) -> None:
            _LOGGER.debug("Daily MIMIT refresh triggered at 08:15 Europe/Rome")
            self.hass.async_create_task(self.async_request_refresh())
            # Re-schedule for tomorrow
            self.schedule_daily_refresh()

        self._unsub_daily = ha_event.async_track_point_in_time(
            self.hass, _on_daily_time, target_time + timedelta(days=1)
        )

    def cancel_daily_refresh(self) -> None:
        """Unsubscribe from the daily time trigger."""
        if self._unsub_daily is not None:
            self._unsub_daily()
            self._unsub_daily = None

    def schedule_intraday_refresh(self) -> None:
        """Register a time trigger at 14:30 Europe/Rome for an ospzApi spot-check.

        The ospzApi queries the live MIMIT database, which may contain prices
        updated by stations after the 08:00 CSV snapshot.  This is a best-effort
        supplement — if the API is unavailable the morning CSV data is kept.
        """
        if self._unsub_intraday is not None:
            self._unsub_intraday()

        tz = dt_util.get_time_zone("Europe/Rome")
        target_time = datetime.now(tz).replace(
            hour=MIMIT_INTRADAY_HOUR,
            minute=MIMIT_INTRADAY_MINUTE,
            second=0,
            microsecond=0,
        )

        @callback
        def _on_intraday_time(_now: datetime) -> None:
            _LOGGER.debug("Intraday ospzApi spot-check triggered at %02d:%02d Europe/Rome",
                          MIMIT_INTRADAY_HOUR, MIMIT_INTRADAY_MINUTE)
            self.hass.async_create_task(self._async_intraday_update())
            self.schedule_intraday_refresh()  # re-schedule for tomorrow

        self._unsub_intraday = ha_event.async_track_point_in_time(
            self.hass, _on_intraday_time, target_time + timedelta(days=1)
        )

    def cancel_intraday_refresh(self) -> None:
        """Unsubscribe from the intraday time trigger."""
        if self._unsub_intraday is not None:
            self._unsub_intraday()
            self._unsub_intraday = None

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Fetch CSV data, merge, filter by radius, compute per-fuel summaries."""
        try:
            registry = await self._get_registry()
            prices_csv = await self._client.async_fetch_prices_csv()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"MIMIT network error: {exc}") from exc

        prices = parse_prices_csv(prices_csv)
        enriched = merge_prices_with_registry(prices, registry)

        lat = self._config_entry.data[CONF_LATITUDE]
        lon = self._config_entry.data[CONF_LONGITUDE]
        radius = self._config_entry.options.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM)

        local_stations = filter_by_radius(enriched, lat, lon, radius)
        data = self._compute_area_data(local_stations)

        # Fetch national/regional averages for context (best-effort)
        national_averages = await self._fetch_national_averages()
        data.national_averages = national_averages
        for fuel_type, area in data.by_fuel.items():
            area.national_average = national_averages.get(fuel_type)

        # Persist to local JSON history
        await self._storage.async_record_snapshot(data)

        # Inject into HA long-term statistics
        now_utc = datetime.now(timezone.utc)
        for fuel_type, area in data.by_fuel.items():
            await async_push_price_statistics(
                self.hass,
                self._config_entry.entry_id,
                fuel_type,
                area.cheapest_price,
                area.average_price,
                now_utc,
            )

        return data

    async def _async_intraday_update(self) -> None:
        """Attempt an intraday spot-check via the MIMIT ospzApi.

        Queries the live MIMIT database (which may be more current than the
        08:00 CSV snapshot) and updates coordinator data if the API returns
        valid results.  Fails silently — morning CSV data is preserved on any
        error.
        """
        if self.data is None:
            return  # can't update without base data

        lat = self._config_entry.data[CONF_LATITUDE]
        lon = self._config_entry.data[CONF_LONGITUDE]
        radius = self._config_entry.options.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM)

        try:
            distributori = await self._client.async_fetch_stations_near(lat, lon, radius)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Intraday ospzApi call failed (%s) — keeping morning CSV data", exc)
            return

        if not distributori:
            _LOGGER.debug("Intraday ospzApi returned no stations — keeping morning CSV data")
            return

        registry = self._registry_cache or {}
        enriched = parse_ospzapi_distributori(distributori, registry, lat, lon)

        if not enriched:
            _LOGGER.debug("Intraday ospzApi: could not parse distributori response — keeping morning data")
            return

        updated = self._compute_area_data(enriched)
        updated.data_source = "ospzapi_intraday"
        updated.national_averages = self.data.national_averages
        for fuel_type, area in updated.by_fuel.items():
            area.national_average = self.data.national_averages.get(fuel_type)

        self.async_set_updated_data(updated)
        _LOGGER.debug(
            "Intraday ospzApi update applied: %d fuel types refreshed",
            len(updated.by_fuel),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_national_averages(self) -> dict[str, float]:
        """Fetch and parse the MIMIT MediaRegionaleStradale.csv.

        Returns an empty dict on any network or parse failure.
        """
        try:
            raw = await self._client.async_fetch_regional_csv()
            return parse_regional_csv(raw)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not fetch national averages (%s) — skipping", exc)
            return {}

    async def _get_registry(self) -> dict[int, Station]:
        """Return the cached station registry, refreshing if stale."""
        now = datetime.now(timezone.utc)
        max_age = timedelta(days=REGISTRY_CACHE_DAYS)

        if (
            self._registry_cache is None
            or self._registry_fetched_at is None
            or (now - self._registry_fetched_at) > max_age
        ):
            _LOGGER.debug("Fetching station registry from MIMIT")
            raw = await self._client.async_fetch_registry_csv()
            self._registry_cache = parse_registry_csv(raw)
            self._registry_fetched_at = now
            _LOGGER.debug("Registry loaded: %d stations", len(self._registry_cache))

        return self._registry_cache  # type: ignore[return-value]

    def _compute_area_data(
        self,
        enriched: list[EnrichedStation],
    ) -> CoordinatorData:
        """Group by fuel type and compute per-type summaries."""
        fuel_types: list[str] = self._config_entry.options.get(
            CONF_FUEL_TYPES, DEFAULT_FUEL_TYPES
        )
        top_n: int = self._config_entry.options.get(CONF_TOP_N, DEFAULT_TOP_N)
        include_self: bool = self._config_entry.options.get(
            CONF_INCLUDE_SELF, DEFAULT_INCLUDE_SELF
        )
        include_servito: bool = self._config_entry.options.get(
            CONF_INCLUDE_SERVITO, DEFAULT_INCLUDE_SERVITO
        )

        by_fuel: dict[str, FuelAreaData] = {}

        for fuel_type in fuel_types:
            stations = [s for s in enriched if s.fuel_type == fuel_type]

            # Apply self/servito filter
            filtered = [
                s
                for s in stations
                if (include_self and s.is_self) or (include_servito and not s.is_self)
            ]

            if not filtered:
                by_fuel[fuel_type] = FuelAreaData(fuel_type=fuel_type)
                continue

            # Sort ascending by price
            filtered.sort(key=lambda s: s.price)

            cheapest = filtered[0]
            top_stations = filtered[:top_n]
            avg = mean(s.price for s in filtered)

            self_only = [s for s in filtered if s.is_self]
            servito_only = [s for s in filtered if not s.is_self]

            by_fuel[fuel_type] = FuelAreaData(
                fuel_type=fuel_type,
                cheapest_price=round(cheapest.price, 4),
                cheapest_station=cheapest,
                top_stations=top_stations,
                average_price=round(avg, 4),
                self_cheapest_price=round(self_only[0].price, 4) if self_only else None,
                servito_cheapest_price=(
                    round(servito_only[0].price, 4) if servito_only else None
                ),
                station_count=len(filtered),
            )

        total = sum(area.station_count for area in by_fuel.values())

        return CoordinatorData(
            by_fuel=by_fuel,
            last_updated=datetime.now(timezone.utc),
            station_count_in_radius=total,
        )
