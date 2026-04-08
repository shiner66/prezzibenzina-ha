"""Real-time market data fetching for fuel price prediction enrichment.

Fetches without any API key:
- Brent crude oil price + 24h change  (Yahoo Finance)
- TTF natural gas price + 24h change  (Yahoo Finance)  ← raffinerie costs
- EU ETS CO₂ carbon price             (Yahoo Finance)  ← raffinerie costs
- EUR/USD exchange rate               (Frankfurter ECB) ← import cost driver
- Top 5 recent oil news headlines     (Google News RSS) ← explains price moves

All fetches run in parallel (asyncio.gather, return_exceptions=True).
Any individual failure returns None / empty list gracefully — the rest of the
data is still used.  The whole function returns None only when both price
feeds (Brent AND EUR/USD) fail.

Cache the result in the coordinator for MARKET_DATA_CACHE_SECONDS (60 min).
"""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

# Yahoo Finance chart endpoint — 5 days of daily OHLC, no API key needed
_YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d&includePrePost=false"
_SYMBOL_BRENT = "BZ%3DF"     # Brent crude futures
_SYMBOL_TTF   = "TTF%3DF"    # TTF natural gas (EUR/MWh)
_SYMBOL_ETS   = "EUAU.DE"    # EU ETS carbon credits (EUR/ton, Xetra)

# ECB official EUR/USD via Frankfurter (no API key, unlimited)
_FRANKFURTER_URL = "https://api.frankfurter.app/latest?to=USD"

# Google News RSS for Italian oil market news
_GNEWS_URL = (
    "https://news.google.com/rss/search"
    "?q=petrolio+brent+OPEC+greggio+carburante"
    "&hl=it&gl=IT&ceid=IT%3Ait"
)

_FETCH_TIMEOUT_S = 10   # per individual HTTP request
_NEWS_MAX = 5           # number of headlines to keep
_SOURCE_RE = re.compile(r"\s*-\s*[^-]+$")   # strips " - Fonte" from titles


@dataclass
class MarketContext:
    """Snapshot of global oil market data fetched at *fetched_at* UTC."""

    # Brent crude (USD/bbl)
    brent_usd: float | None = None
    brent_prev_usd: float | None = None       # prior daily close
    brent_change_pct: float | None = None     # (current-prev)/prev×100

    # TTF natural gas (EUR/MWh) — raffinerie energy cost driver
    ttf_eur: float | None = None
    ttf_prev_eur: float | None = None
    ttf_change_pct: float | None = None

    # EU ETS carbon credits (EUR/ton CO₂) — raffinerie compliance cost
    ets_eur: float | None = None

    # EUR/USD rate (from ECB via Frankfurter)
    eurusd: float | None = None

    # Derived: Brent expressed in EUR/bbl (brent_usd / eurusd)
    brent_eur: float | None = None

    # Google News headlines (Italian, oil/energy topic)
    news_headlines: list[str] = field(default_factory=list)

    # UTC timestamp of the fetch
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def async_fetch_market_context(
    session: aiohttp.ClientSession,
) -> MarketContext | None:
    """Fetch all market data in parallel; return None if critical data unavailable.

    "Critical" means both Brent price AND EUR/USD rate failed.  Partial results
    (e.g. TTF missing, news missing) still produce a valid MarketContext.
    """
    timeout = _aiohttp_timeout(_FETCH_TIMEOUT_S)

    brent_task   = asyncio.create_task(_fetch_yahoo(session, _SYMBOL_BRENT, timeout))
    ttf_task     = asyncio.create_task(_fetch_yahoo(session, _SYMBOL_TTF, timeout))
    ets_task     = asyncio.create_task(_fetch_yahoo(session, _SYMBOL_ETS, timeout))
    eurusd_task  = asyncio.create_task(_fetch_eurusd(session, timeout))
    news_task    = asyncio.create_task(_fetch_news(session, timeout))

    results = await asyncio.gather(
        brent_task, ttf_task, ets_task, eurusd_task, news_task,
        return_exceptions=True,
    )

    brent_data, ttf_data, ets_data, eurusd, news = results

    # Unwrap exceptions → None / []
    if isinstance(brent_data, Exception):
        _LOGGER.debug("Brent fetch failed: %s", brent_data)
        brent_data = None
    if isinstance(ttf_data, Exception):
        _LOGGER.debug("TTF fetch failed: %s", ttf_data)
        ttf_data = None
    if isinstance(ets_data, Exception):
        _LOGGER.debug("ETS fetch failed: %s", ets_data)
        ets_data = None
    if isinstance(eurusd, Exception):
        _LOGGER.debug("EUR/USD fetch failed: %s", eurusd)
        eurusd = None
    if isinstance(news, Exception):
        _LOGGER.debug("News fetch failed: %s", news)
        news = []

    # Require at least Brent price to be useful
    if brent_data is None and eurusd is None:
        _LOGGER.debug("Market context: critical data missing (Brent + EUR/USD) — skipping")
        return None

    # Unpack OHLC tuples
    brent_curr, brent_prev = brent_data if brent_data else (None, None)
    ttf_curr,   ttf_prev   = ttf_data   if ttf_data   else (None, None)
    ets_curr,   _          = ets_data   if ets_data   else (None, None)

    # Derived fields
    brent_change = _pct_change(brent_prev, brent_curr)
    ttf_change   = _pct_change(ttf_prev, ttf_curr)
    brent_eur    = None
    if brent_curr is not None and eurusd is not None and eurusd > 0:
        brent_eur = round(brent_curr / eurusd, 2)

    ctx = MarketContext(
        brent_usd=brent_curr,
        brent_prev_usd=brent_prev,
        brent_change_pct=brent_change,
        ttf_eur=ttf_curr,
        ttf_prev_eur=ttf_prev,
        ttf_change_pct=ttf_change,
        ets_eur=ets_curr,
        eurusd=eurusd,
        brent_eur=brent_eur,
        news_headlines=news or [],
        fetched_at=datetime.now(timezone.utc),
    )
    _LOGGER.debug(
        "Market context: Brent=%.2f USD/bbl TTF=%.2f EUR/MWh EUR/USD=%.4f news=%d",
        brent_curr or 0, ttf_curr or 0, eurusd or 0, len(news or []),
    )
    return ctx


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _fetch_yahoo(
    session: aiohttp.ClientSession,
    symbol: str,
    timeout,
) -> tuple[float, float] | None:
    """Fetch (latest_close, prior_close) from Yahoo Finance chart API.

    Returns None if the response is malformed or unavailable.
    """
    url = _YF_CHART.format(symbol=symbol)
    async with session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    closes = (
        data.get("chart", {})
            .get("result", [{}])[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close", [])
    )
    # Filter out None entries (market was closed on some days)
    valid = [c for c in closes if c is not None]
    if len(valid) < 2:
        return None
    return round(valid[-1], 4), round(valid[-2], 4)


async def _fetch_eurusd(
    session: aiohttp.ClientSession,
    timeout,
) -> float | None:
    """Fetch EUR/USD rate from Frankfurter (ECB official data, no API key)."""
    async with session.get(_FRANKFURTER_URL, timeout=timeout) as resp:
        resp.raise_for_status()
        data = await resp.json()
    rate = data.get("rates", {}).get("USD")
    return round(float(rate), 6) if rate is not None else None


async def _fetch_news(
    session: aiohttp.ClientSession,
    timeout,
) -> list[str]:
    """Fetch top N Italian oil market headlines from Google News RSS."""
    async with session.get(_GNEWS_URL, timeout=timeout) as resp:
        resp.raise_for_status()
        text = await resp.text()

    root = ET.fromstring(text)
    titles: list[str] = []
    for item in root.iter("item"):
        title_el = item.find("title")
        if title_el is None or not title_el.text:
            continue
        # Strip trailing " - Source name"
        clean = _SOURCE_RE.sub("", title_el.text).strip()
        if clean:
            titles.append(clean)
        if len(titles) >= _NEWS_MAX:
            break
    return titles


def _pct_change(prev: float | None, curr: float | None) -> float | None:
    """Return (curr-prev)/prev × 100, or None if either value is missing."""
    if prev is None or curr is None or prev == 0:
        return None
    return round((curr - prev) / prev * 100, 2)


def _aiohttp_timeout(seconds: int):
    """Return an aiohttp.ClientTimeout for *seconds* total."""
    import aiohttp as _aiohttp
    return _aiohttp.ClientTimeout(total=seconds)
