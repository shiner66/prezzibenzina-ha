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
    PB_ENDPOINT_GET_PRICES,
    PB_ENDPOINT_GET_SESSION_KEY,
    PB_ENDPOINT_GET_STATIONS,
    PB_ENDPOINT_GET_STATIONS_FROM_POLYLINE,
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
        base = self._build_session_params(session_key)

        # Usiamo un raggio allargato per il fetch PB in modo da coprire aree con
        # scarsa densità di segnalazioni; il filtro finale usa il raggio reale.
        fetch_radius = max(radius_km, 50.0)

        # ------------------------------------------------------------------
        # Strategia 1a: pb_get_stations_from_polyline — Google Encoded Polyline
        # ------------------------------------------------------------------
        poly_encoded = _encode_bounding_box(lat, lon, fetch_radius)
        _LOGGER.debug(
            "PrezzibenzinaClient → pb_get_stations_from_polyline (encoded) polyline=%s",
            poly_encoded,
        )
        raw = await self._try_get(
            PB_ENDPOINT_GET_STATIONS_FROM_POLYLINE,
            {**base, "polyline": poly_encoded},
        )
        if raw is None:
            raw = await self._try_post_form(
                PB_ENDPOINT_GET_STATIONS_FROM_POLYLINE,
                {**base, "polyline": poly_encoded},
            )

        # ------------------------------------------------------------------
        # Strategia 1b: pb_get_stations_from_polyline — coordinate plaintext
        # Alcune implementazioni usano una lista piatta di lat,lng senza encoding.
        # ------------------------------------------------------------------
        if raw is None:
            dlat, dlon = _bounding_box_deltas(lat, lon, fetch_radius)
            sw = (lat - dlat, lon - dlon)
            ne = (lat + dlat, lon + dlon)
            poly_plain = f"{sw[0]:.5f},{sw[1]:.5f},{ne[0]:.5f},{ne[1]:.5f}"
            _LOGGER.debug(
                "PrezzibenzinaClient → pb_get_stations_from_polyline (plain) polyline=%s",
                poly_plain,
            )
            raw = await self._try_get(
                PB_ENDPOINT_GET_STATIONS_FROM_POLYLINE,
                {**base, "polyline": poly_plain},
            )
            if raw is None:
                raw = await self._try_post_form(
                    PB_ENDPOINT_GET_STATIONS_FROM_POLYLINE,
                    {**base, "polyline": poly_plain},
                )

        # ------------------------------------------------------------------
        # Strategia 2: pb_get_stations_on_polyline — segmento SW→NE
        # Usato dall'app per stazioni lungo un percorso; una linea diagonale
        # che attraversa il bounding box può funzionare come area search.
        # ------------------------------------------------------------------
        if raw is None:
            dlat, dlon = _bounding_box_deltas(lat, lon, fetch_radius)
            segment = _google_encode_polyline([
                (lat - dlat, lon - dlon),
                (lat + dlat, lon + dlon),
            ])
            _LOGGER.debug(
                "PrezzibenzinaClient → pb_get_stations_on_polyline segment=%s", segment
            )
            raw = await self._try_get(
                "pb_get_stations_on_polyline",
                {**base, "polyline": segment},
            )
            if raw is None:
                raw = await self._try_post_form(
                    "pb_get_stations_on_polyline",
                    {**base, "polyline": segment},
                )

        # ------------------------------------------------------------------
        # Strategia 3: pb_get_stations con lat/lng (fallback finale)
        # L'API ignora le coordinate ma il filtro client-side scarta le lontane.
        # ------------------------------------------------------------------
        if raw is None:
            params = self._build_params(lat, lon, fetch_radius, session_key)
            _LOGGER.debug(
                "PrezzibenzinaClient → pb_get_stations (fallback) lat=%s lng=%s",
                params["lat"], params["lng"],
            )
            raw = await self._try_get(PB_ENDPOINT_GET_STATIONS, params)
            if raw is None:
                raw = await self._try_post_form(PB_ENDPOINT_GET_STATIONS, params)

        if raw is None:
            return []

        _LOGGER.debug(
            "PrezzibenzinaClient risposta stazioni (primi 800 car): %s", str(raw)[:800]
        )

        # Filtra per raggio reale (non fetch_radius) e recupera prezzi
        nearby = self._filter_nearby(self._parse_station_list(raw), lat, lon, radius_km)
        _LOGGER.debug(
            "PrezzibenzinaClient: %d stazioni nel raggio %.0f km (fetch allargato a %.0f km)",
            len(nearby), radius_km, fetch_radius,
        )
        if not nearby:
            return []
        return await self._fetch_prices_for_stations(
            nearby, [s["id"] for s in nearby], session_key
        )

    @staticmethod
    def _filter_nearby(
        stations: list[dict[str, Any]],
        lat: float,
        lon: float,
        radius_km: float,
    ) -> list[dict[str, Any]]:
        return [s for s in stations if _haversine(lat, lon, s["lat"], s["lon"]) <= radius_km]

    def _build_session_params(self, session_key: str | None) -> dict[str, Any]:
        """Parametri di sessione/device senza coordinate geografiche."""
        params: dict[str, Any] = {
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

    async def _fetch_prices_for_stations(
        self,
        stations: list[dict[str, Any]],
        station_ids: list[str],
        session_key: str | None,
    ) -> list[dict[str, Any]]:
        """Chiama pb_get_prices per un gruppo di stazioni e restituisce i prezzi normalizzati."""
        prices_params: dict[str, Any] = {
            "stationId": ",".join(station_ids),
            "stationIds": ",".join(station_ids),
            "platform": PB_PLATFORM,
            "pbid": self._pbid,
            "udid": self._udid,
            "appversion": PB_APP_VERSION,
        }
        if session_key:
            prices_params["session_key"] = session_key
            prices_params["token"] = session_key

        raw = await self._try_get(PB_ENDPOINT_GET_PRICES, prices_params)
        if raw is None:
            raw = await self._try_post_form(PB_ENDPOINT_GET_PRICES, prices_params)

        _LOGGER.debug(
            "PrezzibenzinaClient pb_get_prices risposta raw (primi 800 car): %s",
            str(raw)[:800],
        )

        if raw is None:
            return []

        # Costruisce un indice lat/lon per stazione tramite id
        station_by_id = {s["id"]: s for s in stations}
        return self._parse_prices_response(raw, station_by_id)

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
            # Varianti del raggio — l'API potrebbe chiamarlo in modi diversi
            "distance": int(radius_km),
            "radius": int(radius_km),
            "raggio": int(radius_km),
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
            _LOGGER.debug(
                "PrezzibenzinaClient: could not parse session token — risposta raw: %s",
                str(raw_key)[:500],
            )
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
        # L'API è RPC-over-HTTP: ?do=<action>&output=json&...
        url = PB_API_BASE.rstrip("/") + "/"
        try:
            async with self._session.get(
                url,
                params={"do": endpoint, "output": "json", **params},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.debug(
                        "PrezzibenzinaClient GET do=%s → HTTP %d", endpoint, resp.status
                    )
                    return None
                raw = await resp.text()
                return _parse_json_body(endpoint, "GET", raw)
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
                data={"do": endpoint, "output": "json", **params},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status not in (200, 201):
                    _LOGGER.debug(
                        "PrezzibenzinaClient POST do=%s → HTTP %d", endpoint, resp.status
                    )
                    return None
                raw = await resp.text()
                return _parse_json_body(endpoint, "POST", raw)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PrezzibenzinaClient POST do=%s failed: %s", endpoint, exc)
            return None

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_station_list(self, raw: Any) -> list[dict[str, Any]]:
        """Estrae la lista di stazioni (solo metadati, senza prezzi) da pb_get_stations.

        Struttura attesa:
          {'pb_get_stations': {'stations': {'station': [{'id':..,'lat':..,'lng':..}, ...]}}}
        """
        if not isinstance(raw, dict):
            return []

        payload = _unwrap_response(raw)
        stations_container = payload.get("stations")

        if isinstance(stations_container, dict):
            raw_list: list = stations_container.get("station") or []
        elif isinstance(stations_container, list):
            raw_list = stations_container
        else:
            raw_list = []
            for key in ("data", "results", "items"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    raw_list = candidate
                    break

        if not raw_list:
            return []

        _LOGGER.debug(
            "PrezzibenzinaClient: struttura primo item stazione: %s",
            str(raw_list[0])[:400],
        )

        result = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            lat = _coerce_float(item.get("lat") or item.get("latitude"))
            lon = _coerce_float(item.get("lng") or item.get("lon") or item.get("longitude"))
            sid = str(item.get("id") or "")
            if lat is None or lon is None or not sid:
                continue
            result.append({"id": sid, "lat": lat, "lon": lon})
        return result

    def _parse_prices_response(
        self,
        raw: Any,
        station_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Estrae prezzi da pb_get_prices e li abbina alle coordinate delle stazioni.

        Struttura attesa (da rifinire sulla base del log raw):
          {'pb_get_prices': {'prices': {'price': [{'station_id':..,'fuel':..,'price':..}, ...]}}}
        oppure prezzi annidati per stazione.
        """
        if not isinstance(raw, dict):
            return []

        payload = _unwrap_response(raw)
        _LOGGER.debug(
            "PrezzibenzinaClient pb_get_prices payload keys: %s", list(payload.keys())
        )

        # Prova a trovare la lista prezzi in vari formati
        prices_list: list = []
        for key in ("prices", "data", "results"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                prices_list = candidate
                break
            if isinstance(candidate, dict):
                # {'price': [...]} o {'prices': [...]}
                for subkey in ("price", "prices", "items"):
                    sub = candidate.get(subkey)
                    if isinstance(sub, list):
                        prices_list = sub
                        break
                if prices_list:
                    break

        if not prices_list:
            return []

        results: list[dict[str, Any]] = []
        for entry in prices_list:
            if not isinstance(entry, dict):
                continue

            sid = str(
                entry.get("station_id")
                or entry.get("stationId")
                or entry.get("stationID")
                or entry.get("id")
                or ""
            )
            station = station_by_id.get(sid)
            if station is None:
                continue

            fuel_pb = str(
                entry.get("fuel")
                or entry.get("carburante")
                or entry.get("fuelType")
                or entry.get("fuel_type")
                or ""
            ).lower()
            mimit_fuel = PB_FUEL_TO_MIMIT.get(fuel_pb)
            if mimit_fuel is None:
                continue

            price = _coerce_float(
                entry.get("price")
                or entry.get("prezzo")
                or entry.get("value")
            )
            if price is None or price <= 0:
                continue

            is_self = bool(
                entry.get("self")
                or entry.get("isSelf")
                or entry.get("is_self")
                or entry.get("self_service")
            )

            ts_raw = (
                entry.get("update")
                or entry.get("dtComu")
                or entry.get("updated_at")
                or entry.get("updatedAt")
                or entry.get("last_updated")
            )

            results.append({
                "lat": station["lat"],
                "lon": station["lon"],
                "fuel_type": mimit_fuel,
                "price": price,
                "is_self": is_self,
                "reported_at": _parse_timestamp(ts_raw),
            })

        return results

    @staticmethod
    def _extract_token(raw: Any) -> str | None:
        """Estrae il session token dalla risposta di pb_get_session_key.

        Il server avvolge la risposta nel nome dell'endpoint:
          {'pb_get_session_key': {'status': 'ok', 'token': 'abc123'}}
        _unwrap_response() rimuove quel wrapper prima di cercare il token.
        """
        if not isinstance(raw, dict):
            return None
        payload = _unwrap_response(raw)
        for key in ("token", "session_key", "sessiontoken", "key"):
            val = payload.get(key)
            if isinstance(val, str) and len(val) > 4:
                return val
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _bounding_box_deltas(lat: float, lon: float, radius_km: float) -> tuple[float, float]:
    """Restituisce (delta_lat, delta_lon) in gradi per un raggio in km."""
    import math
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    return dlat, dlon


def _encode_bounding_box(lat: float, lon: float, radius_km: float) -> str:
    """Codifica un bounding box attorno a (lat, lon) come Google Encoded Polyline.

    Il rettangolo è rappresentato da 5 vertici (il primo si ripete per chiuderlo):
      SW → NW → NE → SE → SW
    """
    dlat, dlon = _bounding_box_deltas(lat, lon, radius_km)
    sw = (lat - dlat, lon - dlon)
    nw = (lat + dlat, lon - dlon)
    ne = (lat + dlat, lon + dlon)
    se = (lat - dlat, lon + dlon)
    return _google_encode_polyline([sw, nw, ne, se, sw])


def _google_encode_polyline(points: list[tuple[float, float]]) -> str:
    """Google Encoded Polyline Algorithm (https://developers.google.com/maps/documentation/utilities/polylinealgorithm)."""
    result: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in points:
        for value, prev in ((lat, prev_lat), (lon, prev_lon)):
            rounded = round(value * 1e5)
            delta = rounded - prev
            delta <<= 1
            if delta < 0:
                delta = ~delta
            while delta >= 0x20:
                result.append(chr((0x20 | (delta & 0x1f)) + 63))
                delta >>= 5
            result.append(chr(delta + 63))
        prev_lat = round(lat * 1e5)
        prev_lon = round(lon * 1e5)
    return "".join(result)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza great-circle in km tra due punti WGS-84 (copia locale di geo.haversine_km)."""
    import math
    R = 6371.0
    d = math.pi / 180.0
    dlat = (lat2 - lat1) * d
    dlon = (lon2 - lon1) * d
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * d) * math.cos(lat2 * d) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _unwrap_response(raw: dict) -> dict:
    """Rimuove il wrapper col nome dell'endpoint.

    Ogni risposta PB è avvolta nel nome dell'azione:
      {'pb_get_stations': {'status': 'ok', 'stations': {...}}}
      → {'status': 'ok', 'stations': {...}}

    Se il dict ha esattamente una chiave il cui valore è un dict, lo
    spacchetta; altrimenti restituisce raw invariato.
    """
    if len(raw) == 1:
        inner = next(iter(raw.values()))
        if isinstance(inner, dict):
            return inner
    return raw


def _parse_json_body(endpoint: str, method: str, raw: str) -> dict | list | None:
    """Tenta di parsare *raw* come JSON; logga i primi 300 caratteri se fallisce.

    Separato da _try_get/_try_post_form perché aiohttp non permette di leggere
    il body due volte: si legge con resp.text() e si parsano poi i dati qui.
    """
    import json  # stdlib, import locale per non appesantire il modulo

    raw = raw.strip()
    if not raw:
        _LOGGER.debug(
            "PrezzibenzinaClient %s do=%s: risposta vuota", method, endpoint
        )
        return None
    try:
        return json.loads(raw)
    except Exception:
        _LOGGER.debug(
            "PrezzibenzinaClient %s do=%s: risposta non-JSON (primi 300 car): %s",
            method,
            endpoint,
            raw[:300],
        )
        return None


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
