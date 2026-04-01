"""HTTP client for prezzibenzina.it API (reverse-engineered, unofficial).

The prezzibenzina.it app exposes a private REST API at api3.prezzibenzina.it.
Endpoint names were recovered from the Android APK dex; exact request shapes
were reconstructed statically and may require minor adjustments if the server
changes its contract.

All public methods fail silently and return empty results so the integration
always falls back gracefully to MIMIT CSV data.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import (
    HTTP_TIMEOUT_SECONDS,
    PB_API_BASE,
    PB_APP_VERSION,
    PB_ENDPOINT_CREATE_SESSION,
    PB_ENDPOINT_GET_SESSION_KEY,
    PB_ENDPOINT_GET_STATIONS,
    PB_FUEL_TO_MIMIT,
    PB_PLATFORM,
    PB_SDK,
    PB_SESSION_TTL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)


class PrezzibenzinaClient:
    """Async HTTP client for the unofficial prezzibenzina.it API.

    Implements an anonymous session flow mirroring what the Android app does:
      1. pb_get_session_key  — obtain a one-time token
      2. pb_create_session   — exchange the token for a session_key
      3. pb_get_stations     — pass session_key as query param

    If any step of the session flow fails the client proceeds without a session
    key; the server sometimes returns data anyway for simple read endpoints.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        # Stable fake device identifiers (regenerated per HA restart)
        self._udid = str(uuid.uuid4())
        self._pbid = str(uuid.uuid4())
        # Session state
        self._session_key: str | None = None
        self._session_expires_at: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_fetch_stations_near(
        self,
        lat: float,
        lon: float,
        radius_km: float,
    ) -> list[dict[str, Any]]:
        """Return normalised station/price dicts near a location.

        Each dict has keys: lat, lon, fuel_type (MIMIT name), price (float),
        is_self (bool), reported_at (datetime UTC).

        Returns an empty list on any error — callers must not raise.
        """
        try:
            return await self._fetch_stations(lat, lon, radius_km)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PrezzibenzinaClient: unhandled error (%s)", exc)
            return []

    # ------------------------------------------------------------------
    # Private fetch logic
    # ------------------------------------------------------------------

    async def _fetch_stations(
        self,
        lat: float,
        lon: float,
        radius_km: float,
    ) -> list[dict[str, Any]]:
        session_key = await self._ensure_session()
        params = self._build_params(lat, lon, radius_km, session_key)

        # Try GET first (most likely verb for a read endpoint)
        raw = await self._try_get(PB_ENDPOINT_GET_STATIONS, params)
        if raw is None:
            # Fallback: POST application/x-www-form-urlencoded
            raw = await self._try_post_form(PB_ENDPOINT_GET_STATIONS, params)
        if raw is None:
            return []

        return self._parse_stations_response(raw)

    def _build_params(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        session_key: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "lat": lat,
            "lng": lon,
            "latitude": lat,
            "longitude": lon,
            "platform": PB_PLATFORM,
            "pbid": self._pbid,
            "udid": self._udid,
            "appversion": PB_APP_VERSION,
            "sdk": PB_SDK,
        }
        if session_key:
            params["session_key"] = session_key
            params["token"] = session_key
        return params

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> str | None:
        """Return a valid session key, creating one if needed or expired."""
        now = datetime.now(timezone.utc)
        if (
            self._session_key is not None
            and self._session_expires_at is not None
            and now < self._session_expires_at
        ):
            return self._session_key

        # Step 1 — obtain one-time token via pb_get_session_key
        key_params: dict[str, Any] = {
            "platform": PB_PLATFORM,
            "pbid": self._pbid,
            "udid": self._udid,
            "appversion": PB_APP_VERSION,
        }
        raw_key = await self._try_get(PB_ENDPOINT_GET_SESSION_KEY, key_params)
        if raw_key is None:
            raw_key = await self._try_post_form(PB_ENDPOINT_GET_SESSION_KEY, key_params)
        if raw_key is None:
            _LOGGER.debug("PrezzibenzinaClient: pb_get_session_key returned nothing — proceeding without session")
            return None

        token = self._extract_token(raw_key)
        if token is None:
            _LOGGER.debug("PrezzibenzinaClient: could not parse session token from response")
            return None

        # Step 2 — exchange token via pb_create_session (fire-and-forget; some
        # servers only need the token, others require the create call)
        create_params = {**key_params, "token": token, "session_key": token}
        await self._try_get(PB_ENDPOINT_CREATE_SESSION, create_params)

        self._session_key = token
        self._session_expires_at = now + timedelta(hours=PB_SESSION_TTL_HOURS)
        _LOGGER.debug("PrezzibenzinaClient: new session obtained")
        return self._session_key

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _try_get(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict | list | None:
        # L'API è RPC-over-HTTP: l'azione va nel parametro "do", non nel path URL.
        # GET https://api3.prezzibenzina.it/?do=pb_get_stations&lat=...
        url = PB_API_BASE.rstrip("/") + "/"
        try:
            async with self._session.get(
                url,
                params={"do": endpoint, **params},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.debug(
                        "PrezzibenzinaClient GET do=%s → HTTP %d", endpoint, resp.status
                    )
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PrezzibenzinaClient GET do=%s failed: %s", endpoint, exc)
            return None

    async def _try_post_form(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict | list | None:
        # Stessa convenzione per POST: "do" come primo campo del body form.
        url = PB_API_BASE.rstrip("/") + "/"
        try:
            async with self._session.post(
                url,
                data={"do": endpoint, **params},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.debug(
                        "PrezzibenzinaClient POST do=%s → HTTP %d", endpoint, resp.status
                    )
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PrezzibenzinaClient POST do=%s failed: %s", endpoint, exc)
            return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_stations_response(
        self,
        raw: Any,
    ) -> list[dict[str, Any]]:
        """Parse a pb_get_stations response into normalised dicts.

        The exact JSON shape is unknown; we probe common wrapper keys and
        handle both flat and nested price structures.
        """
        if not isinstance(raw, dict):
            return []

        stations_list: list | None = None
        for key in ("stations", "data", "distributori", "results", "items"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                stations_list = candidate
                break

        if stations_list is None:
            return []

        results: list[dict[str, Any]] = []
        for item in stations_list:
            if not isinstance(item, dict):
                continue

            lat = _coerce_float(item.get("latitude") or item.get("lat"))
            lon = _coerce_float(item.get("longitude") or item.get("lng") or item.get("lon"))
            if lat is None or lon is None:
                continue

            prices_raw = item.get("prices") or []
            if not isinstance(prices_raw, list):
                prices_raw = []

            for price_entry in prices_raw:
                if not isinstance(price_entry, dict):
                    continue

                fuel_pb = (
                    price_entry.get("fuel")
                    or price_entry.get("carburante")
                    or price_entry.get("fuelType")
                    or ""
                )
                mimit_fuel = PB_FUEL_TO_MIMIT.get(str(fuel_pb).lower())
                if mimit_fuel is None:
                    continue

                price = _coerce_float(
                    price_entry.get("price") or price_entry.get("prezzo")
                )
                if price is None or price <= 0:
                    continue

                is_self = bool(
                    price_entry.get("self")
                    or price_entry.get("isSelf")
                    or price_entry.get("is_self")
                )

                ts_raw = (
                    price_entry.get("update")
                    or price_entry.get("dtComu")
                    or price_entry.get("updated_at")
                    or price_entry.get("updatedAt")
                )
                reported_at = _parse_timestamp(ts_raw)

                results.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "fuel_type": mimit_fuel,
                        "price": price,
                        "is_self": is_self,
                        "reported_at": reported_at,
                    }
                )

        return results

    @staticmethod
    def _extract_token(raw: Any) -> str | None:
        """Extract a session token string from various response shapes."""
        if not isinstance(raw, dict):
            return None
        for key in ("session_key", "token", "sessiontoken", "key", "data"):
            val = raw.get(key)
            if isinstance(val, str) and len(val) > 4:
                return val
            if isinstance(val, dict):
                for subkey in ("session_key", "token", "key"):
                    subval = val.get(subkey)
                    if isinstance(subval, str) and len(subval) > 4:
                        return subval
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _coerce_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: Any) -> datetime:
    """Parse a timestamp string; fall back to UTC now on failure."""
    if isinstance(raw, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)
