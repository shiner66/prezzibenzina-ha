"""DataUpdateCoordinator for Carburanti MIMIT."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, median
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
    CONF_UPDATE_INTERVAL_COMMUNITY_MIN,
    CONF_UPDATE_INTERVAL_H,
    CONF_USE_COMMUNITY_PRICES,
    DEFAULT_FUEL_TYPES,
    DEFAULT_INCLUDE_SELF,
    DEFAULT_INCLUDE_SERVITO,
    DEFAULT_RADIUS_KM,
    DEFAULT_TOP_N,
    DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN,
    DEFAULT_UPDATE_INTERVAL_H,
    DEFAULT_USE_COMMUNITY_PRICES,
    MIMIT_UPDATE_HOUR,
    MIMIT_UPDATE_MINUTE,
    PB_DISCOVERY_MATCH_KM,
    PB_MAX_STATIONS,
    PB_SCRAPE_DELAY_S,
    REGISTRY_CACHE_DAYS,
)
from .geo import filter_by_radius, haversine_km
from .parser import (
    EnrichedStation,
    Station,
    merge_prices_with_registry,
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
    data_source: str = "mimit_csv"  # "mimit_csv" | "community_overlay"
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

        # Full list of enriched stations inside the radius — kept between
        # updates so community overlays can re-use them without re-fetching.
        self._enriched_cache: list[EnrichedStation] | None = None

        # Schedule daily refresh at MIMIT publish time (08:15 Europe/Rome)
        self._unsub_daily: Callable[[], None] | None = None
        # Community price scraping interval (configurable, default 30 min)
        self._unsub_community: Callable[[], None] | None = None
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

    def schedule_community_refresh(self) -> None:
        """Register an interval-based trigger for community price scraping.

        Scrapes prezzibenzina.it pages for the top PB_MAX_STATIONS nearest
        stations and overlays crowdsourced prices onto the MIMIT cache.
        If ``use_community_prices`` is False this is a no-op.
        """
        use_community = self._config_entry.options.get(
            CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
        )
        if not use_community:
            return

        self.cancel_community_refresh()

        interval_min: int = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL_COMMUNITY_MIN, DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN
        )

        @callback
        def _on_community_interval(_now: datetime) -> None:
            self.hass.async_create_task(self._async_community_price_update())

        self._unsub_community = ha_event.async_track_time_interval(
            self.hass, _on_community_interval, timedelta(minutes=interval_min)
        )
        _LOGGER.info(
            "Community prices: aggiornamento programmato ogni %d minuti", interval_min
        )
        # Run immediately on first setup so sensors show fresh prices right away
        self.hass.async_create_task(self._async_community_price_update())

    def cancel_community_refresh(self) -> None:
        """Unsubscribe from the community price interval trigger."""
        if self._unsub_community is not None:
            self._unsub_community()
            self._unsub_community = None

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
        # Keep a copy for community overlays (avoids re-fetching the CSV)
        self._enriched_cache = list(local_stations)

        _LOGGER.info(
            "MIMIT CSV: %d record prezzi nel raggio %.0f km",
            len(local_stations),
            radius,
        )

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

    async def _async_community_price_update(self) -> None:
        """Scrape community prices from prezzibenzina.it and overlay onto cached data.

        Flow:
        1. Discover PB station IDs in the area via search_handler.php
        2. Match each PB station to the nearest MIMIT station by GPS (≤ PB_DISCOVERY_MATCH_KM)
        3. Scrape up to PB_MAX_STATIONS matched PB pages for community prices
        4. Update EnrichedStation community fields and push to sensors

        Fails silently — previous data is preserved on any error.
        """
        if self.data is None or self._enriched_cache is None:
            return

        use_community = self._config_entry.options.get(
            CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
        )
        if not use_community:
            return

        lat = self._config_entry.data[CONF_LATITUDE]
        lon = self._config_entry.data[CONF_LONGITUDE]
        radius = self._config_entry.options.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM)

        # --- 1. Discover PB station IDs in the search area ---
        pb_stations = await self._client.async_discover_pb_stations(lat, lon, radius)
        if not pb_stations:
            _LOGGER.debug("Community prices: nessuna stazione PB trovata nell'area")
            return

        # --- 2. Build unique-per-station MIMIT index for GPS matching ---
        # One EnrichedStation per unique station.id (pick the first fuel type found)
        mimit_by_id: dict[int, EnrichedStation] = {}
        for s in self._enriched_cache:
            if s.station.id not in mimit_by_id:
                mimit_by_id[s.station.id] = s

        # --- 3. Match PB stations → MIMIT stations by GPS proximity ---
        # pb_id → mimit_station_id (one-to-one: nearest within threshold)
        pb_to_mimit: dict[int, int] = {}
        for pb in pb_stations:
            best_mimit_id: int | None = None
            best_dist = PB_DISCOVERY_MATCH_KM + 1.0  # sentinel > threshold
            for mimit_station in mimit_by_id.values():
                d = haversine_km(pb["lat"], pb["lng"], mimit_station.station.lat, mimit_station.station.lon)
                if d < best_dist:
                    best_dist = d
                    best_mimit_id = mimit_station.station.id
            if best_mimit_id is not None and best_dist <= PB_DISCOVERY_MATCH_KM:
                pb_to_mimit[pb["id"]] = best_mimit_id

        if not pb_to_mimit:
            _LOGGER.debug(
                "Community prices: %d stazioni PB trovate ma nessun match con MIMIT entro %.0f m",
                len(pb_stations),
                PB_DISCOVERY_MATCH_KM * 1000,
            )
            return

        _LOGGER.debug(
            "Community prices: %d/%d stazioni PB abbinate a MIMIT per GPS",
            len(pb_to_mimit),
            len(pb_stations),
        )

        # Build mutable copies grouped by MIMIT station ID
        updated_cache = [copy(s) for s in self._enriched_cache]
        mimit_station_map: dict[int, list[EnrichedStation]] = {}
        for s in updated_cache:
            mimit_station_map.setdefault(s.station.id, []).append(s)

        # Also build reverse: mimit_id → pb_id for efficient lookup
        mimit_to_pb: dict[int, int] = {v: k for k, v in pb_to_mimit.items()}

        # Select top PB_MAX_STATIONS matched stations (sorted by distance in MIMIT cache)
        ordered_mimit_ids: list[int] = []
        seen: set[int] = set()
        for s in sorted(self._enriched_cache, key=lambda x: x.distance_km):
            mid = s.station.id
            if mid not in seen and mid in mimit_to_pb:
                seen.add(mid)
                ordered_mimit_ids.append(mid)
            if len(ordered_mimit_ids) >= PB_MAX_STATIONS:
                break

        # --- 4. Scrape PB pages and apply community prices ---
        scraped_count = 0
        now = datetime.now(timezone.utc)

        for i, mimit_id in enumerate(ordered_mimit_ids):
            pb_id = mimit_to_pb[mimit_id]
            if i > 0:
                await asyncio.sleep(PB_SCRAPE_DELAY_S)

            prices = await self._client.async_scrape_station_community_prices(pb_id)
            if not prices:
                continue

            fuel_self: dict[str, tuple[float, bool]] = {}
            fuel_servito: dict[str, tuple[float, bool]] = {}
            for row in prices:
                mimit_fuel = row["mimit_fuel"]
                if mimit_fuel is None:
                    continue
                price: float = row["price"]
                is_user_reported: bool = row["is_user_reported"]
                if row["is_self"]:
                    existing = fuel_self.get(mimit_fuel)
                    if existing is None or price < existing[0]:
                        fuel_self[mimit_fuel] = (price, is_user_reported)
                else:
                    existing = fuel_servito.get(mimit_fuel)
                    if existing is None or price < existing[0]:
                        fuel_servito[mimit_fuel] = (price, is_user_reported)

            if not fuel_self and not fuel_servito:
                continue

            for enriched in mimit_station_map.get(mimit_id, []):
                ft = enriched.fuel_type
                self_data = fuel_self.get(ft)
                servito_data = fuel_servito.get(ft)
                if self_data is None and servito_data is None:
                    continue
                enriched.community_price_self = self_data[0] if self_data else None
                enriched.community_price_servito = servito_data[0] if servito_data else None
                enriched.community_updated_at = now
                enriched.community_is_user_reported = bool(
                    (self_data and self_data[1]) or (servito_data and servito_data[1])
                )
                # NOTE: MIMIT price is NOT overridden here. MIMIT is the authoritative
                # source (mandatory official reporting). PB community prices are stored
                # as supplementary attributes for context only.
            scraped_count += 1

        if scraped_count == 0:
            _LOGGER.debug("Community prices: nessun dato ottenuto dal scraping")
            return

        updated = self._compute_area_data(updated_cache)
        updated.data_source = "community_overlay"
        updated.national_averages = self.data.national_averages
        for fuel_type, area in updated.by_fuel.items():
            area.national_average = self.data.national_averages.get(fuel_type)

        self._enriched_cache = updated_cache
        self.async_set_updated_data(updated)
        _LOGGER.info(
            "Community prices: aggiornamento applicato da %d/%d stazioni PB abbinate",
            scraped_count,
            len(ordered_mimit_ids),
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

            # Remove statistical outliers: prices more than 20% below the median
            # are almost certainly fake MIMIT mandatory-reporting prices (some stations
            # report an artificially low price to comply with the law without actually
            # offering it).  Only applied when ≥ 4 stations are present so the filter
            # does not misfire on sparse areas.
            if len(filtered) >= 4:
                med = median(s.price for s in filtered)
                # 12% below median: catches fake mandatory-reporting prices while
                # keeping real discounters (typically max 8-10% below median).
                floor = med * 0.88
                valid = [s for s in filtered if s.price >= floor]
                if valid:
                    removed = len(filtered) - len(valid)
                    if removed:
                        _LOGGER.debug(
                            "%s: rimossi %d prezzi anomali sotto %.3f EUR/L (mediana %.3f)",
                            fuel_type, removed, floor, med,
                        )
                    filtered = valid

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
