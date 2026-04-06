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
    FUEL_ID_TO_MIMIT,
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
    data_source: str = "mimit_csv"  # "mimit_csv" | "mimit_intraday" | "community_overlay"
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

        # Timestamp of the last successful MIMIT CSV fetch (UTC).
        # Used by the intraday MIMIT update to decide whether zone API prices
        # are fresher than what we already have from the CSV snapshot.
        self._mimit_csv_fetched_at: datetime | None = None

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
        """Register an interval-based trigger for intraday price updates.

        Two update sources run sequentially on each tick:
        1. MIMIT OsservaPrezzi real-time API (``/ospzApi/search/zone``) — always
           active; reflects official station price changes within 6 h.
        2. prezzibenzina.it community scraping — optional, enabled by
           ``use_community_prices``; fills gaps where MIMIT hasn't been updated
           yet by crowdsourcing recent pump prices.
        """
        self.cancel_community_refresh()

        interval_min: int = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL_COMMUNITY_MIN, DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN
        )

        @callback
        def _on_intraday_interval(_now: datetime) -> None:
            self.hass.async_create_task(self._async_refresh_intraday())

        self._unsub_community = ha_event.async_track_time_interval(
            self.hass, _on_intraday_interval, timedelta(minutes=interval_min)
        )
        _LOGGER.info(
            "Aggiornamento intraday programmato ogni %d minuti "
            "(MIMIT zone API + %s)",
            interval_min,
            "PB community" if self._config_entry.options.get(
                CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
            ) else "solo MIMIT",
        )
        # Run immediately on first setup so sensors show fresh prices right away
        self.hass.async_create_task(self._async_refresh_intraday())

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

        # Record the moment the CSV was successfully fetched so that the
        # intraday MIMIT update can determine which zone API timestamps are
        # genuinely newer than the daily snapshot.
        self._mimit_csv_fetched_at = datetime.now(timezone.utc)

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

    async def _async_refresh_intraday(self) -> None:
        """Orchestrate one intraday refresh cycle.

        Runs in order:
        1. MIMIT zone API (authoritative official prices, intraday resolution)
        2. PB community scraping (crowdsourced fallback, if enabled)

        Running them sequentially ensures that the PB bridge operates on
        top of the already-updated MIMIT intraday prices, so the ±10%
        plausibility check is applied against the freshest official data.
        """
        await self._async_intraday_mimit_update()

        use_community = self._config_entry.options.get(
            CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
        )
        if use_community:
            await self._async_community_price_update()

    async def _async_intraday_mimit_update(self) -> None:
        """Overlay intraday MIMIT prices from the OsservaPrezzi zone API.

        The ``/ospzApi/search/zone`` endpoint returns the current price for
        each station with an ``insertDate`` timestamp.  Stations are legally
        required to report price changes within 6 hours, so this reflects
        real-time updates between the daily 8am CSV snapshots.

        Only prices whose ``insertDate`` is strictly later than
        ``_mimit_csv_fetched_at`` are applied — earlier timestamps mean the
        price was already captured by the morning CSV.

        Fails silently so the daily CSV data is always preserved.
        """
        if self.data is None or self._enriched_cache is None:
            return

        lat = self._config_entry.data[CONF_LATITUDE]
        lon = self._config_entry.data[CONF_LONGITUDE]
        radius = self._config_entry.options.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM)

        zone_results = await self._client.async_fetch_zone_prices(lat, lon, radius)
        if not zone_results:
            _LOGGER.debug("MIMIT intraday: nessun risultato dalla zone API")
            return

        # Build map: station_id → {(mimit_fuel, is_self): (price, insert_date)}
        # Filter by configured radius (zone API doesn't enforce it strictly).
        zone_map: dict[int, dict[tuple[str, bool], tuple[float, datetime]]] = {}
        for item in zone_results:
            if item["distance_km"] > radius:
                continue
            sid: int = item["station_id"]
            insert_date: datetime | None = item["insert_date"]
            fuel_prices: dict[tuple[str, bool], tuple[float, datetime]] = {}
            for fuel in item["fuels"]:
                mimit_fuel = FUEL_ID_TO_MIMIT.get(fuel["fuel_id"])
                if mimit_fuel is None:
                    continue
                key = (mimit_fuel, fuel["is_self"])
                existing = fuel_prices.get(key)
                if existing is None:
                    fuel_prices[key] = (fuel["price"], insert_date)
                else:
                    # When multiple fuelIds map to the same type (e.g., Gasolio
                    # and Blue Diesel both → "Gasolio"), prefer the lower-fuelId
                    # entry (standard variant) — already assigned first since
                    # standard IDs {1,2,3,4} are lowest; keep as-is.
                    pass
            zone_map[sid] = fuel_prices

        if not zone_map:
            _LOGGER.debug(
                "MIMIT intraday: nessuna stazione zone API entro %.0f km", radius
            )
            return

        # Apply prices to a mutable copy of the enriched cache
        updated_cache = [copy(s) for s in self._enriched_cache]
        update_count = 0
        csv_ts = self._mimit_csv_fetched_at  # may be None on first boot

        for enriched in updated_cache:
            sid = enriched.station.id
            prices_for_station = zone_map.get(sid)
            if not prices_for_station:
                continue
            key = (enriched.fuel_type, enriched.is_self)
            entry = prices_for_station.get(key)
            if entry is None:
                continue
            zone_price, zone_insert_date = entry

            # Only override when the zone API timestamp is strictly newer than
            # the CSV snapshot so we don't regress to stale API data on boot.
            if csv_ts is not None and zone_insert_date is not None:
                if zone_insert_date <= csv_ts:
                    continue

            new_price = round(zone_price, 3)
            if new_price != round(enriched.price, 3):
                _LOGGER.debug(
                    "MIMIT intraday [%s %s %s]: %.3f → %.3f EUR/L (API ts: %s)",
                    enriched.station.nome,
                    enriched.fuel_type,
                    "self" if enriched.is_self else "servito",
                    enriched.price,
                    new_price,
                    zone_insert_date,
                )
                enriched.price = new_price
                update_count += 1

        if update_count == 0:
            _LOGGER.debug(
                "MIMIT intraday: %d stazioni zone API, nessun prezzo cambiato rispetto al CSV",
                len(zone_map),
            )
            return

        updated = self._compute_area_data(updated_cache)
        updated.data_source = "mimit_intraday"
        updated.national_averages = self.data.national_averages
        for fuel_type, area in updated.by_fuel.items():
            area.national_average = self.data.national_averages.get(fuel_type)

        self._enriched_cache = updated_cache
        self.async_set_updated_data(updated)
        _LOGGER.info(
            "MIMIT intraday: %d prezzi aggiornati da zone API (%d stazioni nell'area)",
            update_count,
            len(zone_map),
        )

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

        # Compute the date of the last MIMIT publish (08:00 Europe/Rome).
        # A PB report is only a useful bridge if it post-dates that snapshot:
        #   • If current Rome time ≥ 08:00 → last MIMIT update was today
        #   • If current Rome time < 08:00 → last MIMIT update was yesterday
        tz_rome = dt_util.get_time_zone("Europe/Rome")
        now_rome = now.astimezone(tz_rome)
        if now_rome.hour >= MIMIT_UPDATE_HOUR:
            last_mimit_date = now_rome.date()
        else:
            last_mimit_date = now_rome.date() - timedelta(days=1)

        # Tuple type: (price, is_user_reported, report_date | None)
        _PBEntry = tuple  # (float, bool, date | None)

        for i, mimit_id in enumerate(ordered_mimit_ids):
            pb_id = mimit_to_pb[mimit_id]
            if i > 0:
                await asyncio.sleep(PB_SCRAPE_DELAY_S)

            prices = await self._client.async_scrape_station_community_prices(pb_id)
            if not prices:
                continue

            # Keep the most recent (latest date) entry per fuel/service combination.
            # If dates are equal or missing, prefer the lower price (conservative).
            fuel_self: dict[str, _PBEntry] = {}    # mimit_fuel → (price, is_user_reported, report_date)
            fuel_servito: dict[str, _PBEntry] = {}
            for row in prices:
                mimit_fuel = row["mimit_fuel"]
                if mimit_fuel is None:
                    continue
                price: float = row["price"]
                is_user_reported: bool = row["is_user_reported"]
                report_date = row.get("report_date")  # datetime.date | None
                entry: _PBEntry = (price, is_user_reported, report_date)
                target = fuel_self if row["is_self"] else fuel_servito
                existing = target.get(mimit_fuel)
                if existing is None:
                    target[mimit_fuel] = entry
                else:
                    # Prefer the entry with the more recent date; on tie prefer lower price
                    ex_date = existing[2]
                    if report_date is not None and (ex_date is None or report_date > ex_date):
                        target[mimit_fuel] = entry
                    elif report_date == ex_date and price < existing[0]:
                        target[mimit_fuel] = entry

            if not fuel_self and not fuel_servito:
                continue

            for enriched in mimit_station_map.get(mimit_id, []):
                ft = enriched.fuel_type
                self_data = fuel_self.get(ft)    # (price, is_user_reported, report_date) | None
                servito_data = fuel_servito.get(ft)
                if self_data is None and servito_data is None:
                    continue

                enriched.community_price_self = self_data[0] if self_data else None
                enriched.community_price_servito = servito_data[0] if servito_data else None
                enriched.community_updated_at = now
                enriched.community_is_user_reported = bool(
                    (self_data and self_data[1]) or (servito_data and servito_data[1])
                )

                # PB bridge: use community price as best estimate between MIMIT updates.
                #
                # Conditions (all must be true):
                #   1. PB report date >= last_mimit_date: only reports that post-date the
                #      last official MIMIT snapshot (08:00 Europe/Rome) are useful as a
                #      bridge — older reports are already reflected in MIMIT data.
                #   2. PB price is within ±10% of the MIMIT price (plausibility — filters
                #      typos and outlier user reports).
                #   3. Self-service preferred because operators apply their own markup on
                #      full-service prices; however, both modes are bridged when a valid
                #      report is available (self → self, servito → servito).
                mimit_price = enriched.price
                if mimit_price <= 0:
                    continue

                pb_candidate: _PBEntry | None = self_data if enriched.is_self else servito_data
                if pb_candidate is not None:
                    pb_price, _, pb_date = pb_candidate
                    pb_recent = pb_date is not None and pb_date >= last_mimit_date
                    pb_plausible = abs(pb_price - mimit_price) / mimit_price <= 0.10
                    if pb_recent and pb_plausible:
                        enriched.price = round(pb_price, 3)
                        _LOGGER.debug(
                            "PB bridge [%s %s]: %.3f → %.3f EUR/L (report: %s, last_mimit: %s)",
                            enriched.station.nome, ft, mimit_price, pb_price, pb_date, last_mimit_date,
                        )

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
