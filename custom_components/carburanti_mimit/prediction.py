"""Pure-Python price trend analysis and 7-day forecast.

Algorithm
---------
1. Extract price series from the last 30 days of HistoryStorage.
2. Interpolate small gaps (≤ 3 consecutive missing days) linearly.
3. Compute OLS linear regression over the last 14 points.
4. If R² > 0.6 → use linear extrapolation for the 7-day forecast.
   Otherwise → use a linearly-weighted moving average (WMA).
5. Clamp predictions to [0.5 × mean_7d, 2.0 × mean_7d] to avoid nonsense.
6. Confidence: high (≥30 pts, R²≥0.7) | medium (≥14 pts) | low (< 14 pts).

No external dependencies — only the standard library is used.

Optional AI enrichment
----------------------
When ``CONF_AI_PROVIDER`` is configured, ``async_ai_prediction()`` calls the
selected LLM API with a natural-language prompt and returns an explanatory
text stored as the ``ai_analysis`` sensor attribute.  Errors fall back silently
to ``None``.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from statistics import mean
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from .storage import DailySnapshot

_LOGGER = logging.getLogger(__name__)

# Thresholds
_R2_THRESHOLD = 0.6
_R2_HIGH_CONFIDENCE = 0.7
_TREND_DAILY_THRESHOLD = 0.002   # 0.2 % per day → "stable" zone
_CLAMP_LOW_FACTOR = 0.5
_CLAMP_HIGH_FACTOR = 2.0
_MIN_POINTS_LOW = 7
_MIN_POINTS_MEDIUM = 14
_MIN_POINTS_HIGH = 30
_MAX_GAP_INTERPOLATE = 3

# AI API endpoints
_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


# ---------------------------------------------------------------------------
# Public result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PredictionResult:
    """Holds the complete forecast for one fuel type."""

    trend_direction: str           # "up" | "down" | "stable"
    trend_pct_7d: float            # Predicted % change over 7 days
    predicted_prices: list[float]  # 7 daily forecasts (index 0 = tomorrow)
    confidence: str                # "high" | "medium" | "low"
    method_used: str               # "linear_regression" | "moving_average"
    weekly_change_pct: float | None   = None  # actual % vs 7 days ago
    monthly_change_pct: float | None  = None  # actual % vs 30 days ago
    ai_analysis: str | None           = field(default=None)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_prediction(
    history: list[DailySnapshot],
    fuel_type: str,
) -> PredictionResult | None:
    """Compute a 7-day price forecast.

    Returns ``None`` if fewer than ``_MIN_POINTS_LOW`` data points are
    available (sensors should then report ``unavailable``).
    """
    prices = _extract_prices(history)
    if len(prices) < _MIN_POINTS_LOW:
        return None

    # Historical changes
    weekly_change = _pct_change(prices[-8], prices[-1]) if len(prices) >= 8 else None
    monthly_change = _pct_change(prices[-31], prices[-1]) if len(prices) >= 31 else None

    # Confidence based on data volume
    if len(prices) >= _MIN_POINTS_HIGH:
        confidence_base = "high"
    elif len(prices) >= _MIN_POINTS_MEDIUM:
        confidence_base = "medium"
    else:
        confidence_base = "low"

    # Fit linear regression on last 14 points (or all if fewer)
    fit_window = prices[-14:]
    n = len(fit_window)
    xs = list(range(n))
    slope, intercept = _linear_regression(xs, fit_window)
    r2 = _r_squared(xs, fit_window, slope, intercept)

    # Choose method
    if r2 >= _R2_THRESHOLD and len(prices) >= _MIN_POINTS_MEDIUM:
        method = "linear_regression"
        predicted = [intercept + slope * (n + i) for i in range(7)]
    else:
        method = "moving_average"
        predicted = _weighted_moving_average_forecast(prices, slope)

    # Clamp predictions
    mean_7d = mean(prices[-7:]) if len(prices) >= 7 else mean(prices)
    lo = _CLAMP_LOW_FACTOR * mean_7d
    hi = _CLAMP_HIGH_FACTOR * mean_7d
    predicted = [max(lo, min(hi, p)) for p in predicted]

    # Round to 4 decimal places (EUR/L precision)
    predicted = [round(p, 4) for p in predicted]

    # Trend direction and overall 7-day change
    current = prices[-1]
    p7 = predicted[-1]
    trend_pct = _pct_change(current, p7)
    trend_dir = _trend_direction(slope, mean_7d)

    # Downgrade confidence if R² is poor
    if confidence_base == "high" and r2 < _R2_HIGH_CONFIDENCE:
        confidence_base = "medium"

    return PredictionResult(
        trend_direction=trend_dir,
        trend_pct_7d=round(trend_pct, 2),
        predicted_prices=predicted,
        confidence=confidence_base,
        method_used=method,
        weekly_change_pct=round(weekly_change, 2) if weekly_change is not None else None,
        monthly_change_pct=round(monthly_change, 2) if monthly_change is not None else None,
    )


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _extract_prices(history: list[DailySnapshot]) -> list[float]:
    """Extract a clean price series, interpolating small gaps."""
    prices: list[float | None] = [s.cheapest for s in history]
    # Linear interpolation for gaps up to _MAX_GAP_INTERPOLATE
    i = 0
    while i < len(prices):
        if prices[i] is None:
            # Find gap end
            j = i + 1
            while j < len(prices) and prices[j] is None:
                j += 1
            gap_len = j - i
            if gap_len <= _MAX_GAP_INTERPOLATE and i > 0 and j < len(prices):
                # Interpolate between prices[i-1] and prices[j]
                p_start = prices[i - 1]
                p_end = prices[j]
                for k in range(gap_len):
                    prices[i + k] = p_start + (p_end - p_start) * (k + 1) / (gap_len + 1)
            else:
                # Truncate at first unresolvable gap
                prices = prices[:i]
                break
            i = j
        else:
            i += 1

    return [p for p in prices if p is not None]


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares linear regression.

    Returns ``(slope, intercept)``.
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _r_squared(
    xs: list[float],
    ys: list[float],
    slope: float,
    intercept: float,
) -> float:
    """Coefficient of determination R²."""
    if len(ys) < 2:
        return 0.0
    y_mean = mean(ys)
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    if ss_tot == 0:
        return 1.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    return max(0.0, 1.0 - ss_res / ss_tot)


def _weighted_moving_average_forecast(
    prices: list[float],
    slope: float,
) -> list[float]:
    """7-day forecast using linearly-weighted moving average + slope extrapolation.

    More recent values get higher weight (weight = position index, 1-based).
    """
    window = prices[-7:] if len(prices) >= 7 else prices
    n = len(window)
    weights = list(range(1, n + 1))
    total_weight = sum(weights)
    wma = sum(w * p for w, p in zip(weights, window)) / total_weight
    return [wma + slope * (i + 1) for i in range(7)]


def _trend_direction(slope: float, mean_price: float) -> str:
    """Classify daily slope as 'up', 'down', or 'stable'."""
    if mean_price == 0:
        return "stable"
    relative = slope / mean_price
    if relative > _TREND_DAILY_THRESHOLD:
        return "up"
    if relative < -_TREND_DAILY_THRESHOLD:
        return "down"
    return "stable"


def _pct_change(old: float | None, new: float | None) -> float:
    """Return percentage change from *old* to *new*."""
    if old is None or new is None or old == 0:
        return 0.0
    return (new - old) / old * 100.0


# ---------------------------------------------------------------------------
# Optional AI enrichment
# ---------------------------------------------------------------------------

async def async_ai_prediction(
    session: aiohttp.ClientSession,
    provider: str,
    api_key: str,
    history: list[DailySnapshot],
    fuel_type: str,
    prediction: PredictionResult,
) -> str | None:
    """Call an LLM for a natural-language explanation of the price trend.

    Returns the explanation string, or ``None`` on any error (silent fallback).
    """
    from .const import AI_PROVIDER_CLAUDE, AI_PROVIDER_OPENAI  # avoid circular at top

    summary_lines = [
        f"{s.date}: {s.cheapest:.3f} EUR" if s.cheapest is not None else f"{s.date}: N/A"
        for s in history[-30:]
    ]
    summary = "\n".join(summary_lines)

    prompt = (
        f"Ecco i prezzi giornalieri più bassi del carburante '{fuel_type}' "
        f"in Italia negli ultimi 30 giorni:\n\n{summary}\n\n"
        f"Il modello statistico prevede una tendenza '{prediction.trend_direction}' "
        f"del {prediction.trend_pct_7d:+.1f}% nei prossimi 7 giorni "
        f"(metodo: {prediction.method_used}, confidenza: {prediction.confidence}).\n\n"
        "In 2-3 frasi, quali fattori di mercato potrebbero spiegare questa tendenza "
        "per il mercato italiano?"
    )

    try:
        if provider == AI_PROVIDER_CLAUDE:
            return await _call_claude(session, api_key, prompt)
        if provider == AI_PROVIDER_OPENAI:
            return await _call_openai(session, api_key, prompt)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("AI prediction call failed (%s) — skipping", exc)
    return None


async def _call_claude(
    session: aiohttp.ClientSession,
    api_key: str,
    prompt: str,
) -> str | None:
    """Call the Anthropic Claude API."""
    import aiohttp as _aiohttp

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with session.post(
        _CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    content = data.get("content", [])
    if content and isinstance(content, list):
        return content[0].get("text", "").strip() or None
    return None


async def _call_openai(
    session: aiohttp.ClientSession,
    api_key: str,
    prompt: str,
) -> str | None:
    """Call the OpenAI Chat Completions API."""
    import aiohttp as _aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with session.post(
        _OPENAI_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip() or None
    return None
