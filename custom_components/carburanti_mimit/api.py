"""HTTP client for MIMIT fuel price data."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import aiohttp

from .const import (
    COMMUNITY_SERVICE_SELF,
    COMMUNITY_SERVICE_USER_RPT,
    FUEL_MAP_PB_TO_MIMIT,
    HTTP_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_VALIDATION,
    PB_SCRAPE_TIMEOUT_S,
    URL_PB_STATION,
    URL_PRICES,
    URL_REGIONAL_AVERAGES,
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

    async def async_fetch_regional_csv(self) -> str:
        """Fetch national/regional road fuel average prices CSV from MIMIT.

        Returns raw CSV text. Raises aiohttp.ClientError on network errors.
        """
        return await self._fetch_csv(URL_REGIONAL_AVERAGES)

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

    async def async_scrape_station_community_prices(
        self,
        station_id: int,
    ) -> list[dict[str, Any]]:
        """Scrape community-reported prices for one station from prezzibenzina.it.

        Returns a list of dicts with keys:
            date (str), fuel (str), service (str), price (float),
            is_self (bool), is_user_reported (bool), mimit_fuel (str | None)

        Returns an empty list on any failure (network, parse, etc.).
        """
        url = URL_PB_STATION.format(station_id=station_id)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "it-IT,it;q=0.9",
            "Cookie": "cookiebar=accepted",
        }
        timeout = aiohttp.ClientTimeout(total=PB_SCRAPE_TIMEOUT_S)
        try:
            async with self._session.get(url, headers=headers, timeout=timeout) as resp:
                resp.raise_for_status()
                html = await resp.text(errors="replace")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PB scrape station %d failed: %s", station_id, exc)
            return []

        _LOGGER.debug(
            "PB scrape station %d: HTTP OK, html len=%d, "
            "has st_reports_row=%s, first 300 chars: %s",
            station_id,
            len(html),
            "st_reports_row" in html,
            html[:300].replace("\n", " "),
        )

        results = self._parse_community_html(html)

        if not results and "st_reports_row" in html:
            # Regex didn't match despite the marker being present — log a wider
            # context around the first occurrence to diagnose the actual structure.
            idx = html.find("st_reports_row")
            _LOGGER.debug(
                "PB scrape station %d: regex miss — HTML context around st_reports_row: %s",
                station_id,
                html[max(0, idx - 50): idx + 400].replace("\n", " "),
            )

        return results

    @staticmethod
    def _parse_community_html(html: str) -> list[dict[str, Any]]:
        """Extract community price rows from a prezzibenzina.it station page."""
        rows = re.findall(
            r'class="st_reports_row"[^>]*>\s*'
            r'<div class="st_reports_data">([^<]+)</div>\s*'
            r'<div class="st_reports_fuel[^"]*">([^<]+)</div>\s*'
            r'<div class="st_reports_service">([^<]+)</div>\s*'
            r'<div class="st_reports_price">([\d.,]+)\s*&euro;</div>',
            html,
        )
        results: list[dict[str, Any]] = []
        for date_str, fuel_raw, service_raw, price_raw in rows:
            fuel = fuel_raw.strip()
            service = service_raw.strip()
            try:
                price = float(price_raw.replace(",", "."))
            except ValueError:
                continue
            if price <= 0:
                continue
            mimit_fuel = FUEL_MAP_PB_TO_MIMIT.get(fuel)
            results.append({
                "date": date_str.strip(),
                "fuel": fuel,
                "service": service,
                "price": price,
                "is_self": service in COMMUNITY_SERVICE_SELF,
                "is_user_reported": service in COMMUNITY_SERVICE_USER_RPT,
                "mimit_fuel": mimit_fuel,
            })
        return results

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
