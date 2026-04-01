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
    PB_MATCH_RADIUS_KM,
    REGISTRY_CACHE_DAYS,
)
from .geo import filter_by_radius, haversine_km
from .pb_api import PrezzibenzinaClient
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
    data_source: str = "csv_morning"  # "csv_morning" | "ospzapi_intraday" | "prezzibenzina_intraday"
    national_averages: dict[str, float] = field(default_factory=dict)


class CarburantiMimitCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Fetches and processes MIMIT fuel price data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: MimitApiClient,
        storage: HistoryStorage,
        pb_client: PrezzibenzinaClient | None = None,
    ) -> None:
        interval_h = config_entry.options.get(CONF_UPDATE_INTERVAL_H, DEFAULT_UPDATE_INTERVAL_H)
        super().__init__(
            hass,
            _LOGGER,
            name=f"carburanti_mimit_{config_entry.entry_id[:8]}",
            update_interval=timedelta(hours=interval_h),
        )
        self._client = client
        self._pb_client = pb_client
        self._storage = storage
        self._config_entry = config_entry

        # Registry cache (refreshed at most every REGISTRY_CACHE_DAYS)
        self._registry_cache: dict[int, Station] | None = None
        self._registry_fetched_at: datetime | None = None

        # Full list of enriched stations inside the radius — kept between
        # updates so PB intraday overlays can re-use them without re-fetching.
        self._enriched_cache: list[EnrichedStation] | None = None

        # Schedule daily refresh at MIMIT publish time (08:15 Europe/Rome)
        self._unsub_daily: Callable[[], None] | None = None
        # Schedule intraday spot-check via ospzApi (14:30 Europe/Rome)
        self._unsub_intraday: Callable[[], None] | None = None
        # PrezzibenzinaIT interval-based unsub (single cancel function)
        self._unsub_pb: Callable[[], None] | None = None
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

    def schedule_pb_intraday_refreshes(self) -> None:
        """Register an interval-based trigger for PrezzibenzinaIT spot-checks.

        The refresh cadence mirrors the ``update_interval_h`` option set in the
        config flow.  If that option is, say, 4 h, PrezzibenzinaIT prices are
        queried every 4 h and overlaid onto the MIMIT station cache so sensors
        always show the freshest crowdsourced price available.

        If no PB client is available the method is a no-op.
        """
        if self._pb_client is None:
            return
        self.cancel_pb_intraday_refreshes()

        interval_h: int = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL_H, DEFAULT_UPDATE_INTERVAL_H
        )

        @callback
        def _on_pb_interval(_now: datetime) -> None:
            self.hass.async_create_task(self._async_pb_intraday_update())

        self._unsub_pb = ha_event.async_track_time_interval(
            self.hass, _on_pb_interval, timedelta(hours=interval_h)
        )
        _LOGGER.info(
            "PrezzibenzinaIT: spot-check programmato ogni %dh (intervallo dal config)",
            interval_h,
        )
        # Esegui subito il primo fetch senza aspettare il primo tick dell'intervallo
        self.hass.async_create_task(self._async_pb_intraday_update())

    def cancel_pb_intraday_refreshes(self) -> None:
        """Unsubscribe from the PrezzibenzinaIT interval trigger."""
        if self._unsub_pb is not None:
            self._unsub_pb()
            self._unsub_pb = None

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
        # Keep a copy for PB intraday overlays (avoids re-fetching the CSV)
        self._enriched_cache = list(local_stations)

        _LOGGER.info(
            "MIMIT CSV: %d record prezzi nel raggio %.0f km — fonte csv_morning",
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

        _LOGGER.debug("ospzApi intraday: avvio fetch (lat=%.4f, lon=%.4f, raggio=%.0f km)", lat, lon, radius)

        try:
            distributori = await self._client.async_fetch_stations_near(lat, lon, radius)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("ospzApi intraday: fetch fallito (%s) — dati mattutini invariati", exc)
            return

        if not distributori:
            _LOGGER.debug("ospzApi intraday: nessuna stazione ricevuta — dati mattutini invariati")
            return

        registry = self._registry_cache or {}
        enriched = parse_ospzapi_distributori(distributori, registry, lat, lon)

        if not enriched:
            _LOGGER.debug("ospzApi intraday: risposta non parsabile — dati mattutini invariati")
            return

        updated = self._compute_area_data(enriched)
        updated.data_source = "ospzapi_intraday"
        updated.national_averages = self.data.national_averages
        for fuel_type, area in updated.by_fuel.items():
            area.national_average = self.data.national_averages.get(fuel_type)

        self.async_set_updated_data(updated)
        _LOGGER.info(
            "ospzApi intraday: %d stazioni elaborate, %d fuel type aggiornati",
            len(distributori),
            len(updated.by_fuel),
        )

    async def _async_pb_intraday_update(self) -> None:
        """Fetch fresh prices from prezzibenzina.it and overlay onto cached data.

        Uses the enriched MIMIT station cache as the base so station metadata
        (name, address, brand) is always from the authoritative MIMIT registry.
        PB prices overwrite the cached price of a matching station when a
        station is found within PB_MATCH_RADIUS_KM.
        Fails silently — the previous data is preserved on any error.
        """
        if self._pb_client is None or self.data is None or self._enriched_cache is None:
            return

        lat = self._config_entry.data[CONF_LATITUDE]
        lon = self._config_entry.data[CONF_LONGITUDE]
        radius = self._config_entry.options.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM)

        _LOGGER.debug(
            "PrezzibenzinaIT: avvio fetch intraday (lat=%.4f, lon=%.4f, raggio=%.0f km)",
            lat,
            lon,
            radius,
        )

        pb_stations = await self._pb_client.async_fetch_stations_near(lat, lon, radius)

        if not pb_stations:
            _LOGGER.warning(
                "PrezzibenzinaIT: nessuna stazione ricevuta — prezzi MIMIT invariati"
            )
            return

        _LOGGER.debug("PrezzibenzinaIT: %d prezzi ricevuti dall'API", len(pb_stations))

        merged, matched = self._merge_pb_stations(pb_stations, self._enriched_cache)

        if matched == 0:
            _LOGGER.warning(
                "PrezzibenzinaIT: %d prezzi ricevuti ma 0 stazioni MIMIT entro %.0f m — "
                "verifica coordinate o PB_MATCH_RADIUS_KM",
                len(pb_stations),
                PB_MATCH_RADIUS_KM * 1000,
            )
            return

        updated = self._compute_area_data(merged)
        updated.data_source = "prezzibenzina_intraday"
        updated.national_averages = self.data.national_averages

        # Log per-fuel price deltas at INFO so siano visibili senza debug mode
        for fuel_type, area in updated.by_fuel.items():
            area.national_average = self.data.national_averages.get(fuel_type)
            old_area = self.data.by_fuel.get(fuel_type)
            if old_area is None or area.cheapest_price is None:
                continue
            old_price = old_area.cheapest_price
            new_price = area.cheapest_price
            if old_price is None:
                _LOGGER.info(
                    "PrezzibenzinaIT [%s]: prezzo minimo disponibile → %.4f EUR",
                    fuel_type,
                    new_price,
                )
            elif new_price != old_price:
                direction = "↑" if new_price > old_price else "↓"
                _LOGGER.info(
                    "PrezzibenzinaIT [%s]: prezzo minimo %s %.4f → %.4f EUR (%+.4f)",
                    fuel_type,
                    direction,
                    old_price,
                    new_price,
                    new_price - old_price,
                )
            else:
                _LOGGER.debug(
                    "PrezzibenzinaIT [%s]: prezzo minimo invariato %.4f EUR",
                    fuel_type,
                    new_price,
                )

        self.async_set_updated_data(updated)
        _LOGGER.info(
            "PrezzibenzinaIT: aggiornamento applicato — %d/%d prezzi su stazioni MIMIT, "
            "%d fuel type elaborati",
            matched,
            len(pb_stations),
            len(updated.by_fuel),
        )

    @staticmethod
    def _merge_pb_stations(
        pb_stations: list[dict],
        base_stations: list[EnrichedStation],
    ) -> tuple[list[EnrichedStation], int]:
        """Overlay PrezzibenzinaIT prices onto MIMIT enriched station list.

        For each PB price point the nearest MIMIT station of the same fuel
        type within PB_MATCH_RADIUS_KM gets its price and timestamp updated.
        Stations not matched by any PB entry keep their MIMIT price unchanged.

        A shallow copy of each EnrichedStation is made so the original
        ``_enriched_cache`` is not mutated.

        Returns a tuple of (updated_station_list, matched_count).
        """
        from copy import copy  # local import to keep top-level imports tidy

        updated = [copy(s) for s in base_stations]
        matched = 0

        for pb in pb_stations:
            pb_lat: float = pb["lat"]
            pb_lon: float = pb["lon"]
            pb_fuel: str = pb["fuel_type"]
            pb_price: float = pb["price"]
            pb_is_self: bool = pb["is_self"]
            pb_ts: datetime = pb["reported_at"]

            best: EnrichedStation | None = None
            best_dist = PB_MATCH_RADIUS_KM + 1.0  # sentinel > threshold

            for s in updated:
                if s.fuel_type != pb_fuel:
                    continue
                dist = haversine_km(pb_lat, pb_lon, s.station.lat, s.station.lon)
                if dist < best_dist:
                    best_dist = dist
                    best = s

            if best is not None and best_dist <= PB_MATCH_RADIUS_KM:
                best.price = round(pb_price, 4)
                best.is_self = pb_is_self
                best.reported_at = pb_ts
                matched += 1

        return updated, matched

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
