"""HTTP client for MIMIT fuel price data."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import (
    HTTP_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_VALIDATION,
    URL_API_POSITION,
    URL_PRICES,
    URL_REGISTRY,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
_TIMEOUT_VALIDATION = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_VALIDATION)


class MimitApiClient:
    """Async HTTP client for MIMIT open-data endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def async_fetch_prices_csv(self) -> str:
        """Fetch current fuel prices CSV from MIMIT.

        Returns raw CSV text (pipe-delimited, UTF-8).
        Raises aiohttp.ClientError on network or HTTP errors.
        """
        return await self._fetch_csv(URL_PRICES)

    async def async_fetch_registry_csv(self) -> str:
        """Fetch active station registry CSV from MIMIT.

        Returns raw CSV text (pipe-delimited, UTF-8).
        Raises aiohttp.ClientError on network or HTTP errors.
        """
        return await self._fetch_csv(URL_REGISTRY)

    async def async_validate_connectivity(self) -> bool:
        """Lightweight connectivity check — just tries to reach the registry URL.

        Returns True on success, False on failure (does not raise).
        """
        try:
            async with self._session.get(
                URL_REGISTRY,
                timeout=_TIMEOUT_VALIDATION,
                allow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("MIMIT connectivity check failed: %s", exc)
            return False

    async def async_fetch_stations_near(
        self,
        lat: float,
        lon: float,
        radius_km: float,
    ) -> list[dict[str, Any]]:
        """Search stations near a position via the unofficial REST API.

        Returns the list of station dicts from the ``distributori`` key,
        or an empty list if the API is unavailable (it is reverse-engineered
        and may be unstable).
        """
        payload = {"lat": lat, "lon": lon, "raggio": radius_km}
        try:
            async with self._session.post(
                URL_API_POSITION,
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data.get("distributori", []) if isinstance(data, dict) else []
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "MIMIT REST API unavailable (%s) — falling back to CSV-only mode", exc
            )
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_csv(self, url: str) -> str:
        """Fetch a URL and return text decoded as UTF-8, stripping BOM."""
        async with self._session.get(url, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            raw = await resp.read()
        # Force UTF-8 regardless of the Content-Type charset header,
        # which MIMIT sometimes sets incorrectly.
        text = raw.decode("utf-8", errors="replace")
        # Strip potential BOM
        return text.lstrip("\ufeff")
