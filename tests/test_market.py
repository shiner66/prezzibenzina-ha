"""Unit tests for market.py — real-time market data fetching."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.carburanti_mimit.market import (
    MarketContext,
    _fetch_eurusd,
    _fetch_news,
    _fetch_yahoo,
    _pct_change,
    async_fetch_market_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(response_data, *, status=200, content_type="application/json", text=None):
    """Return a mock aiohttp.ClientSession whose .get() yields the given data."""
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if text is not None:
        resp.text = AsyncMock(return_value=text)
    else:
        resp.json = AsyncMock(return_value=response_data)

    session = MagicMock()
    session.get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=resp),
        __aexit__=AsyncMock(return_value=False),
    ))
    return session


def _yahoo_payload(closes: list[float | None]):
    """Minimal Yahoo Finance chart JSON payload."""
    return {
        "chart": {
            "result": [
                {
                    "indicators": {
                        "quote": [{"close": closes}]
                    }
                }
            ],
            "error": None,
        }
    }


def _frankfurter_payload(usd_rate: float):
    return {"base": "EUR", "rates": {"USD": usd_rate}}


_SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>Cessate il fuoco, Brent in calo del 3% - Il Sole 24 Ore</title>
    </item>
    <item>
      <title>OPEC+ mantiene quota produzione per terzo mese - Reuters</title>
    </item>
    <item>
      <title>Greggio: WTI sotto 70 dollari per la prima volta - ANSA</title>
    </item>
  </channel>
</rss>"""


# ---------------------------------------------------------------------------
# MarketContext dataclass
# ---------------------------------------------------------------------------

class TestMarketContextDataclass:
    def test_all_optional_fields_default_none(self):
        ctx = MarketContext()
        assert ctx.brent_usd is None
        assert ctx.ttf_eur is None
        assert ctx.ets_eur is None
        assert ctx.eurusd is None
        assert ctx.brent_eur is None
        assert ctx.news_headlines == []

    def test_fetched_at_defaults_to_utc_now(self):
        before = datetime.now(timezone.utc)
        ctx = MarketContext()
        after = datetime.now(timezone.utc)
        assert before <= ctx.fetched_at <= after

    def test_explicit_values_stored(self):
        ctx = MarketContext(brent_usd=72.5, eurusd=1.08, brent_eur=67.13)
        assert ctx.brent_usd == 72.5
        assert ctx.eurusd == 1.08
        assert ctx.brent_eur == 67.13


# ---------------------------------------------------------------------------
# _pct_change helper
# ---------------------------------------------------------------------------

class TestPctChange:
    def test_positive_change(self):
        result = _pct_change(70.0, 72.1)
        assert result == pytest.approx(3.0, abs=0.01)

    def test_negative_change(self):
        result = _pct_change(72.0, 70.56)
        assert result == pytest.approx(-2.0, abs=0.01)

    def test_none_prev_returns_none(self):
        assert _pct_change(None, 72.0) is None

    def test_none_curr_returns_none(self):
        assert _pct_change(70.0, None) is None

    def test_zero_prev_returns_none(self):
        assert _pct_change(0.0, 72.0) is None


# ---------------------------------------------------------------------------
# _fetch_yahoo
# ---------------------------------------------------------------------------

class TestFetchYahoo:
    @pytest.mark.asyncio
    async def test_happy_path_returns_two_closes(self):
        closes = [70.1, 71.2, 72.3, None, 73.4]  # None in middle — should be filtered
        session = _make_session(_yahoo_payload(closes))
        from aiohttp import ClientTimeout
        timeout = ClientTimeout(total=10)
        result = await _fetch_yahoo(session, "BZ%3DF", timeout)
        # Last two valid closes: 73.4 and 72.3
        assert result is not None
        latest, prev = result
        assert latest == pytest.approx(73.4)
        assert prev == pytest.approx(72.3)

    @pytest.mark.asyncio
    async def test_missing_data_returns_none(self):
        session = _make_session({"chart": {"result": [{"indicators": {"quote": [{}]}}]}})
        from aiohttp import ClientTimeout
        result = await _fetch_yahoo(session, "BZ%3DF", ClientTimeout(total=10))
        assert result is None

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        resp = AsyncMock()
        resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 429"))
        session = MagicMock()
        session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        from aiohttp import ClientTimeout
        with pytest.raises(Exception):
            await _fetch_yahoo(session, "BZ%3DF", ClientTimeout(total=10))


# ---------------------------------------------------------------------------
# _fetch_eurusd
# ---------------------------------------------------------------------------

class TestFetchEurusd:
    @pytest.mark.asyncio
    async def test_happy_path_returns_float(self):
        session = _make_session(_frankfurter_payload(1.0836))
        from aiohttp import ClientTimeout
        result = await _fetch_eurusd(session, ClientTimeout(total=10))
        assert result == pytest.approx(1.0836, abs=1e-4)

    @pytest.mark.asyncio
    async def test_missing_usd_key_returns_none(self):
        session = _make_session({"base": "EUR", "rates": {"GBP": 0.85}})
        from aiohttp import ClientTimeout
        result = await _fetch_eurusd(session, ClientTimeout(total=10))
        assert result is None


# ---------------------------------------------------------------------------
# _fetch_news
# ---------------------------------------------------------------------------

class TestFetchNews:
    @pytest.mark.asyncio
    async def test_happy_path_returns_titles(self):
        session = _make_session(None, text=_SAMPLE_RSS)
        from aiohttp import ClientTimeout
        headlines = await _fetch_news(session, "https://example.com/rss", 10, ClientTimeout(total=10))
        assert len(headlines) == 3
        # Source stripped from first headline
        assert "Il Sole 24 Ore" not in headlines[0]
        assert "cessate" in headlines[0].lower() or "Cessate" in headlines[0]

    @pytest.mark.asyncio
    async def test_empty_feed_returns_empty_list(self):
        empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        session = _make_session(None, text=empty_rss)
        from aiohttp import ClientTimeout
        headlines = await _fetch_news(session, "https://example.com/rss", 10, ClientTimeout(total=10))
        assert headlines == []

    @pytest.mark.asyncio
    async def test_max_5_headlines(self):
        items = "\n".join(
            f"<item><title>Notizia {i} - Fonte</title></item>" for i in range(15)
        )
        rss = f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'
        session = _make_session(None, text=rss)
        from aiohttp import ClientTimeout
        headlines = await _fetch_news(session, "https://example.com/rss", 5, ClientTimeout(total=10))
        assert len(headlines) <= 5


# ---------------------------------------------------------------------------
# async_fetch_market_context — integration-level
# ---------------------------------------------------------------------------

class TestAsyncFetchMarketContext:
    @pytest.mark.asyncio
    async def test_returns_none_when_both_brent_and_eurusd_fail(self):
        """If Brent and EUR/USD both fail, return None."""
        with (
            patch("custom_components.carburanti_mimit.market._fetch_yahoo", side_effect=Exception("timeout")),
            patch("custom_components.carburanti_mimit.market._fetch_eurusd", side_effect=Exception("timeout")),
            patch("custom_components.carburanti_mimit.market._fetch_news", return_value=[]),
        ):
            session = MagicMock()
            result = await async_fetch_market_context(session)
        assert result is None

    @pytest.mark.asyncio
    async def test_partial_success_brent_ok_eurusd_fails(self):
        """Brent available but EUR/USD fails → MarketContext with brent_usd set, brent_eur=None."""
        with (
            patch("custom_components.carburanti_mimit.market._fetch_yahoo", return_value=(72.5, 70.0)),
            patch("custom_components.carburanti_mimit.market._fetch_eurusd", side_effect=Exception("DNS")),
            patch("custom_components.carburanti_mimit.market._fetch_news", return_value=[]),
        ):
            session = MagicMock()
            result = await async_fetch_market_context(session)
        assert result is not None
        assert result.brent_usd == pytest.approx(72.5)
        assert result.eurusd is None
        assert result.brent_eur is None  # can't compute without EUR/USD

    @pytest.mark.asyncio
    async def test_full_success_populates_all_fields(self):
        """All fetches succeed → all derived fields computed."""
        with (
            patch(
                "custom_components.carburanti_mimit.market._fetch_yahoo",
                side_effect=[
                    (72.5, 70.0),    # Brent
                    (35.2, 34.0),    # TTF
                    (62.1, 61.5),    # ETS
                ],
            ),
            patch("custom_components.carburanti_mimit.market._fetch_eurusd", return_value=1.08),
            patch(
                "custom_components.carburanti_mimit.market._fetch_news",
                # oil news task created first, fiscal news task second
                side_effect=[["Headline 1"], ["Fiscal headline"]],
            ),
        ):
            session = MagicMock()
            result = await async_fetch_market_context(session)

        assert result is not None
        assert result.brent_usd == pytest.approx(72.5)
        assert result.ttf_eur == pytest.approx(35.2)
        assert result.ets_eur == pytest.approx(62.1)
        assert result.eurusd == pytest.approx(1.08)
        assert result.brent_eur == pytest.approx(72.5 / 1.08, abs=0.01)
        assert result.brent_change_pct == pytest.approx((72.5 - 70.0) / 70.0 * 100, abs=0.01)
        # fiscal headlines come first in the combined list, then oil market headlines
        assert result.news_headlines == ["Fiscal headline", "Headline 1"]
        # fetched_at should be very recent
        age = (datetime.now(timezone.utc) - result.fetched_at).total_seconds()
        assert age < 5

    @pytest.mark.asyncio
    async def test_zero_eurusd_does_not_raise(self):
        """Zero EUR/USD rate → brent_eur stays None, no ZeroDivisionError."""
        with (
            patch("custom_components.carburanti_mimit.market._fetch_yahoo", return_value=(72.5, 70.0)),
            patch("custom_components.carburanti_mimit.market._fetch_eurusd", return_value=0.0),
            patch("custom_components.carburanti_mimit.market._fetch_news", return_value=[]),
        ):
            session = MagicMock()
            result = await async_fetch_market_context(session)
        # brent_usd present so not None overall
        assert result is not None
        assert result.brent_eur is None  # division by zero guarded
