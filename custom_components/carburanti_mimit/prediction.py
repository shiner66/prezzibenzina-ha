"""Pure-Python price trend analysis and 7-day forecast with AI geopolitical enrichment.

Algorithm (ensemble)
--------------------
1. Extract price series from the last 30 days of HistoryStorage.
2. Interpolate small gaps (≤ 3 consecutive missing days) linearly.
3. Fit Exponentially Weighted OLS (EWOLS) over the last 14 points — recent
   prices get exponentially higher weight (alpha=0.15).
4. Run Holt's Double Exponential Smoothing (level + trend, α=0.3, β=0.1).
5. Ensemble:
   - If EWOLS R² ≥ 0.6 AND ≥14 data points: 60% EWOLS + 40% Holt forecast.
   - If ≥5 data points (but EWOLS R² < 0.6): Holt only.
   - Otherwise: linearly-weighted moving average (WMA) fallback.
6. Volatility-adaptive clamping:
   [max(0.5×μ₇, μ₇-3σ), min(2.0×μ₇, μ₇+3σ)]
7. Confidence: high (≥30 pts, R²≥0.7) | medium (≥14 pts) | low (< 14 pts).

Additional statistical indicators
----------------------------------
- price_volatility: normalised standard deviation of recent prices (σ/μ)
- price_momentum:   recent-7d mean minus prior-7d mean, as % of prior-7d mean
  Positive → accelerating upward; negative → accelerating downward.
- price_acceleration: second-order change (slope of slope), in EUR/day²

No external dependencies — only the standard library is used.

Optional AI enrichment with real-time market data
--------------------------------------------------
When ``CONF_AI_PROVIDER`` is configured, ``async_ai_prediction()`` calls the
selected LLM API with a prompt that includes:

  • Real-time market data (Brent, TTF gas, ETS carbon, EUR/USD) from market.py
  • Recent oil market news headlines from Google News RSS
  • 30-day local price history
  • Statistical indicators (trend, volatility, momentum, acceleration)
  • Seasonal demand context
  • Italian excise-tax (accise) and VAT structure

The AI receives a dedicated *system* message with today's date and analyst role.
A separate *user* message carries all the data.  Max tokens: 1200 (Claude) /
1500 (OpenAI), up from 800 in previous versions.

The LLM response uses structured tags: ``[RISCHIO:basso|medio|alto]``,
``[PREZZO_3G:X.XXX]``, ``[SINTESI:testo breve]``.

Errors fall back silently to ``None``.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from statistics import mean, stdev
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from .market import MarketContext
    from .storage import DailySnapshot

_LOGGER = logging.getLogger(__name__)

# Thresholds
_R2_THRESHOLD = 0.6
_R2_HIGH_CONFIDENCE = 0.7
_TREND_DAILY_THRESHOLD = 0.002   # 0.2 % per day → "stable" zone
_CLAMP_LOW_FACTOR = 0.5
_CLAMP_HIGH_FACTOR = 2.0
_MIN_POINTS_LOW = 1
_MIN_POINTS_MEDIUM = 14
_MIN_POINTS_HIGH = 30
_MIN_POINTS_HOLT = 5             # Holt needs ≥2 but 5 gives stable init
_MAX_GAP_INTERPOLATE = 3

# Ensemble algorithm parameters
_EWOLS_ALPHA = 0.15   # exponential decay for OLS weights (older = less weight)
_HOLT_ALPHA  = 0.3    # Holt level smoothing
_HOLT_BETA   = 0.1    # Holt trend smoothing
_ENSEMBLE_WLS_WEIGHT  = 0.6   # when EWOLS R² ≥ threshold
_ENSEMBLE_HOLT_WEIGHT = 0.4

# AI API endpoints
_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Pattern used to extract risk level from AI response
_RISK_PATTERN = re.compile(r"\[RISCHIO:(basso|medio|alto)\]", re.IGNORECASE)
# Pattern used to extract AI 3-day price estimate  [PREZZO_3G:1.750]
_PRICE_3D_PATTERN = re.compile(r"\[PREZZO_3G:([\d]+[.,][\d]+)\]", re.IGNORECASE)
# Pattern used to extract the one-line AI summary  [SINTESI:testo breve]
_SINTESI_PATTERN = re.compile(r"\[SINTESI:([^\]]{1,200})\]", re.IGNORECASE)
# Pattern used to extract 7-day price forecast  [PREZZI_7G:1.750,1.748,...]
_PRICES_7D_PATTERN = re.compile(r"\[PREZZI_7G:([\d.]+(?:,[\d.]+){6})\]", re.IGNORECASE)

# Plausible retail fuel price range (EUR/L) used to validate AI-returned values
_MIN_PLAUSIBLE_PRICE = 0.3
_MAX_PLAUSIBLE_PRICE = 5.0

# Italian excise duties (accise) — updated periodically by decree
_ACCISE = {
    "Benzina": 0.7284,
    "Gasolio": 0.6174,
    "GPL": 0.2928,
    "Metano": 0.0000,
    "HVO": 0.6174,
    "Gasolio Riscaldamento": 0.4030,
}


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
    weekly_change_pct: float | None   = None   # actual % vs 7 days ago
    monthly_change_pct: float | None  = None   # actual % vs 30 days ago

    # Additional statistical indicators
    price_volatility: float | None    = None   # σ/μ of last 14 prices (normalised)
    price_momentum: float | None      = None   # (mean_7d − mean_prev_7d) / mean_prev_7d × 100
    price_acceleration: float | None  = None   # EUR/day² (2nd derivative estimate)

    # Statistical 3-day forecast (predicted_prices[2], already in predicted_prices)
    predicted_price_3d: float | None   = None

    # AI enrichment
    ai_analysis: str | None           = field(default=None)
    ai_risk_level: str | None         = field(default=None)  # "basso" | "medio" | "alto"
    ai_predicted_price_3d: float | None = field(default=None)  # AI estimate for day+3


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_prediction(
    history: list[DailySnapshot],
    fuel_type: str,
) -> PredictionResult | None:
    """Compute a 7-day price forecast.

    Returns ``None`` only when no price data at all is available.
    With a single point the forecast is flat (trend = stable, all 7 days
    equal to today's price). Confidence increases with data volume:
    low (<14), medium (14-29), high (≥30 with R²≥0.7).
    """
    prices = _extract_prices(history)
    if len(prices) < _MIN_POINTS_LOW:
        return None

    # Historical changes (only meaningful with enough data)
    weekly_change = _pct_change(prices[-8], prices[-1]) if len(prices) >= 8 else None
    monthly_change = _pct_change(prices[-31], prices[-1]) if len(prices) >= 31 else None

    # Confidence based on data volume
    if len(prices) >= _MIN_POINTS_HIGH:
        confidence_base = "high"
    elif len(prices) >= _MIN_POINTS_MEDIUM:
        confidence_base = "medium"
    else:
        confidence_base = "low"

    mean_7d = mean(prices[-7:]) if len(prices) >= 7 else mean(prices)

    # With a single point we can only predict "flat at today's price"
    if len(prices) == 1:
        predicted = [round(prices[0], 4)] * 7
        return PredictionResult(
            trend_direction="stable",
            trend_pct_7d=0.0,
            predicted_prices=predicted,
            predicted_price_3d=predicted[2],
            confidence="low",
            method_used="moving_average",
            weekly_change_pct=None,
            monthly_change_pct=None,
            price_volatility=None,
            price_momentum=None,
            price_acceleration=None,
        )

    # --- Ensemble: EWOLS + Holt's Double Exponential Smoothing ---------------
    fit_window = prices[-14:]
    n = len(fit_window)
    xs = list(range(n))

    # Method 1: Exponentially Weighted OLS (recent prices get higher weight)
    slope_ew, intercept_ew, r2 = _ewols_regression(xs, fit_window, _EWOLS_ALPHA)

    # Method 2: Holt's Double EMA (level + trend)
    holt_pred: list[float] | None = None
    if len(prices) >= _MIN_POINTS_HOLT:
        holt_pred = _holt_exponential_smoothing(prices, _HOLT_ALPHA, _HOLT_BETA, 7)

    # Ensemble selection
    if r2 >= _R2_THRESHOLD and len(prices) >= _MIN_POINTS_MEDIUM and holt_pred is not None:
        method = "ensemble_ols_holt"
        ewols_f = [intercept_ew + slope_ew * (n + i) for i in range(7)]
        predicted = [
            _ENSEMBLE_WLS_WEIGHT * ew + _ENSEMBLE_HOLT_WEIGHT * h
            for ew, h in zip(ewols_f, holt_pred)
        ]
        slope = slope_ew
    elif holt_pred is not None:
        method = "holt_exponential_smoothing"
        predicted = holt_pred
        slope = (predicted[-1] - predicted[0]) / 6.0 if len(predicted) == 7 else 0.0
    else:
        method = "moving_average"
        slope = slope_ew
        predicted = _weighted_moving_average_forecast(prices, slope)

    # Volatility-adaptive clamping: [max(0.5×μ, μ-3σ), min(2.0×μ, μ+3σ)]
    prices_for_clamp = prices[-14:] if len(prices) >= 14 else prices
    if len(prices_for_clamp) >= 3:
        sigma = stdev(prices_for_clamp)
        lo = max(_CLAMP_LOW_FACTOR * mean_7d, mean_7d - 3 * sigma)
        hi = min(_CLAMP_HIGH_FACTOR * mean_7d, mean_7d + 3 * sigma)
    else:
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

    # Additional statistical indicators
    volatility = _price_volatility(prices[-14:] if len(prices) >= 14 else prices)
    momentum = _price_momentum(prices)
    acceleration = _price_acceleration(prices)

    return PredictionResult(
        trend_direction=trend_dir,
        trend_pct_7d=round(trend_pct, 2),
        predicted_prices=predicted,
        predicted_price_3d=predicted[2] if len(predicted) >= 3 else None,
        confidence=confidence_base,
        method_used=method,
        weekly_change_pct=round(weekly_change, 2) if weekly_change is not None else None,
        monthly_change_pct=round(monthly_change, 2) if monthly_change is not None else None,
        price_volatility=volatility,
        price_momentum=momentum,
        price_acceleration=acceleration,
    )


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _extract_prices(history: list[DailySnapshot]) -> list[float]:
    """Extract a clean price series, interpolating small gaps."""
    prices: list[float | None] = [s.cheapest for s in history]
    i = 0
    while i < len(prices):
        if prices[i] is None:
            j = i + 1
            while j < len(prices) and prices[j] is None:
                j += 1
            gap_len = j - i
            if i == 0:
                # Leading None values — skip them, can't interpolate without a prior price
                prices = prices[j:]
                i = 0
            elif gap_len <= _MAX_GAP_INTERPOLATE and j < len(prices):
                p_start = prices[i - 1]
                p_end = prices[j]
                for k in range(gap_len):
                    prices[i + k] = p_start + (p_end - p_start) * (k + 1) / (gap_len + 1)
                i = j
            else:
                prices = prices[:i]
                break
        else:
            i += 1

    return [p for p in prices if p is not None]


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares linear regression. Returns (slope, intercept)."""
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


def _ew_weights(n: int, alpha: float = _EWOLS_ALPHA) -> list[float]:
    """Exponential weights for *n* points.

    The most-recent point (index n-1) gets weight ``exp(0) = 1``, older points
    decay as ``exp(-alpha * (n-1-i))``.  Weights are returned un-normalised;
    callers that need them to sum to 1 must divide by ``sum(weights)``.
    """
    return [math.exp(-alpha * (n - 1 - i)) for i in range(n)]


def _ewols_regression(
    xs: list[float],
    ys: list[float],
    alpha: float = _EWOLS_ALPHA,
) -> tuple[float, float, float]:
    """Exponentially-weighted OLS.  Returns (slope, intercept, weighted_r2)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0

    ws = _ew_weights(n, alpha)
    W      = sum(ws)
    Wx     = sum(w * x for w, x in zip(ws, xs))
    Wy     = sum(w * y for w, y in zip(ws, ys))
    Wxx    = sum(w * x * x for w, x in zip(ws, xs))
    Wxy    = sum(w * x * y for w, x, y in zip(ws, xs, ys))

    denom = W * Wxx - Wx * Wx
    if denom == 0:
        return 0.0, Wy / W, 0.0

    slope     = (W * Wxy - Wx * Wy) / denom
    intercept = (Wy - slope * Wx) / W

    # Weighted R²
    y_wmean = Wy / W
    ss_tot = sum(w * (y - y_wmean) ** 2 for w, y in zip(ws, ys))
    if ss_tot == 0:
        return slope, intercept, 1.0
    ss_res = sum(w * (y - (slope * x + intercept)) ** 2 for w, x, y in zip(ws, xs, ys))
    r2 = max(0.0, 1.0 - ss_res / ss_tot)
    return slope, intercept, r2


def _holt_exponential_smoothing(
    prices: list[float],
    alpha: float = _HOLT_ALPHA,
    beta: float  = _HOLT_BETA,
    n_forecast: int = 7,
) -> list[float]:
    """Holt's Double Exponential Smoothing (level + trend).

    Returns a list of *n_forecast* predicted values starting from tomorrow.
    Handles degenerate inputs (< 2 prices) gracefully.
    """
    if len(prices) < 2:
        return [round(prices[0], 4)] * n_forecast if prices else [0.0] * n_forecast

    level = prices[0]
    trend = prices[1] - prices[0]

    for p in prices[1:]:
        level_prev, trend_prev = level, trend
        level = alpha * p + (1 - alpha) * (level_prev + trend_prev)
        trend = beta * (level - level_prev) + (1 - beta) * trend_prev

    return [round(level + (i + 1) * trend, 4) for i in range(n_forecast)]


def _weighted_moving_average_forecast(
    prices: list[float],
    slope: float,
) -> list[float]:
    """7-day forecast using linearly-weighted moving average + slope extrapolation."""
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


def _price_volatility(prices: list[float]) -> float | None:
    """Normalised volatility: σ / μ (coefficient of variation).

    Returns a value typically in [0, 0.1] for fuel prices.
    Higher = more erratic price behaviour.
    """
    if len(prices) < 3:
        return None
    try:
        mu = mean(prices)
        if mu == 0:
            return None
        return round(stdev(prices) / mu, 5)
    except Exception:
        return None


def _price_momentum(prices: list[float]) -> float | None:
    """Momentum: percentage change between mean of last 7 days vs previous 7 days.

    Positive → recent prices rising vs the prior week.
    Negative → recent prices falling vs the prior week.
    """
    if len(prices) < 14:
        return None
    recent = mean(prices[-7:])
    prior = mean(prices[-14:-7])
    return round(_pct_change(prior, recent), 3)


def _price_acceleration(prices: list[float]) -> float | None:
    """Second derivative estimate: change of the daily slope (EUR/day²).

    Compares the regression slope of the first half vs second half
    of the last 14 data points. Positive → slope is steepening upward.
    """
    if len(prices) < 14:
        return None
    half = prices[-14:]
    xs_first = list(range(7))
    xs_second = list(range(7, 14))
    slope_first, _ = _linear_regression(xs_first, half[:7])
    slope_second, _ = _linear_regression(xs_second, half[7:])
    return round(slope_second - slope_first, 6)


# ---------------------------------------------------------------------------
# Seasonal context helper
# ---------------------------------------------------------------------------

def _seasonal_context() -> str:
    """Return a brief seasonal demand note based on the current month."""
    month = date.today().month
    if month in (6, 7, 8):
        return (
            "Siamo in estate (picco vacanze estive): domanda di benzina tipicamente alta "
            "per viaggi, pressione al rialzo stagionale."
        )
    if month in (12, 1, 2):
        return (
            "Siamo in inverno: domanda di gasolio per riscaldamento elevata, "
            "condizioni meteo possono ridurre mobilità."
        )
    if month in (3, 4, 5):
        return (
            "Siamo in primavera: domanda in ripresa dopo l'inverno, "
            "stagione di manutenzione delle raffinerie (può ridurre offerta temporaneamente)."
        )
    return (
        "Siamo in autunno: domanda in calo rispetto all'estate, "
        "avvio stagione riscaldamento, raffinerie in produzione normale."
    )


# ---------------------------------------------------------------------------
# Optional AI enrichment with geopolitical analysis
# ---------------------------------------------------------------------------

async def async_ai_prediction(
    session: aiohttp.ClientSession,
    provider: str,
    api_key: str,
    history: list[DailySnapshot],
    fuel_type: str,
    prediction: PredictionResult | None,
    current_price: float | None = None,
    national_average: float | None = None,
    market_context: MarketContext | None = None,
    openai_model: str | None = None,
    claude_model: str | None = None,
    area_stats: dict | None = None,
    top_stations: list | None = None,
    previous_ai: dict | None = None,
) -> tuple[str | None, str | None, float | None, str | None, list[float] | None]:
    """Call an LLM for a geopolitical and market analysis of the price trend.

    Works from day 1: when *prediction* is ``None`` the prompt focuses on pure
    geopolitical/market context for the current price; as history accumulates
    the prompt is progressively enriched with statistical indicators.

    *market_context* (from ``market.py``) injects real-time Brent/TTF/ETS/EUR/USD
    prices and news headlines directly into the prompt so the AI reasons from
    today's actual market data, not its training-data cutoff.

    *area_stats* is a dict with keys cheapest_price, average_price,
    self_cheapest_price, servito_cheapest_price, station_count.

    *top_stations* is a list of dicts with keys bandiera, nome, distance_km,
    price, is_self.

    *previous_ai* is a dict with keys brief, risk_level, price_3d holding the
    results of the last AI call, used to build a calibration feedback loop.

    Returns ``(ai_analysis, ai_risk_level, ai_price_3d, ai_brief, ai_prices_7d)``
    or ``(None, None, None, None, None)`` on error.
    - ai_analysis:    full geopolitical analysis text
    - ai_risk_level:  "basso" | "medio" | "alto" parsed from [RISCHIO:...] tag
    - ai_price_3d:    AI-estimated price in 3 days parsed from [PREZZO_3G:...] tag
    - ai_brief:       one-sentence summary parsed from [SINTESI:...] tag
    - ai_prices_7d:   7-day daily price forecast parsed from [PREZZI_7G:...] tag
    """
    from .const import AI_PROVIDER_CLAUDE, AI_PROVIDER_OPENAI  # avoid circular at top

    prompt = _build_geopolitical_prompt(
        history, fuel_type, prediction, current_price, national_average,
        market_context, area_stats, top_stations, previous_ai,
    )

    try:
        if provider == AI_PROVIDER_CLAUDE:
            text = await _call_claude(session, api_key, prompt, claude_model)
        elif provider == AI_PROVIDER_OPENAI:
            text = await _call_openai(session, api_key, prompt, openai_model)
        else:
            return None, None, None, None, None

        if not text:
            _LOGGER.warning("AI call returned empty response")
            return None, None, None, None, None

        risk = _parse_risk_level(text)
        price_3d = _parse_price_3d(text)
        brief = _parse_brief(text)
        prices_7d = _parse_prices_7d(text)
        if brief is None:
            _LOGGER.warning(
                "AI response missing [SINTESI:] tag — response preview: %.300s",
                text,
            )
        else:
            _LOGGER.debug(
                "AI parsed — risk=%s price_3d=%s brief=%s prices_7d=%s",
                risk, price_3d, brief, prices_7d,
            )
        return text.strip(), risk, price_3d, brief, prices_7d

    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("AI prediction call failed (%s: %s) — skipping", type(exc).__name__, exc)
    return None, None, None, None, None


def _build_geopolitical_prompt(
    history: list[DailySnapshot],
    fuel_type: str,
    prediction: PredictionResult | None,
    current_price: float | None = None,
    national_average: float | None = None,
    market_context: MarketContext | None = None,
    area_stats: dict | None = None,
    top_stations: list | None = None,
    previous_ai: dict | None = None,
) -> str:
    """Build an adaptive prompt for geopolitical + market analysis.

    Enrichment levels:
    - No history  → pure geopolitical/market context for the current price
    - Some history → adds observed price trend (cheapest + avg, up to 90 days)
    - Full stats   → adds 7-day forecast, volatility, momentum, acceleration

    Additional context included when available:
    - Real-time market data (Brent, TTF, ETS, EUR/USD, up to 10 news headlines)
    - Area statistics (spread, self/servito split, station count)
    - Top-N local stations (brand, price, distance)
    - Previous AI analysis for calibration (feedback loop)
    - Weekly price pattern (average by day-of-week)
    """
    today = date.today().isoformat()
    seasonal = _seasonal_context()
    accisa = _ACCISE.get(fuel_type, 0.0)

    # ---- Real-time market data section (Brent, TTF, ETS, EUR/USD, news) -----
    market_section = _build_market_section(market_context)

    # ---- Price / history section (up to 90 days, cheapest + avg) -----------
    if history:
        history_lines = []
        for s in history:
            cheapest_txt = f"{s.cheapest:.4f}" if s.cheapest is not None else "N/D"
            avg_txt = f"{s.average:.4f}" if s.average is not None else "N/D"
            history_lines.append(
                f"  {s.date}: cheapest={cheapest_txt}  avg={avg_txt}  EUR/L"
            )
        history_text = "\n".join(history_lines)
        data_days = len([s for s in history if s.cheapest is not None])
        price_section = (
            f"=== DATI STORICI LOCALI — {fuel_type} (ultimi {data_days} giorni, fino al {today}) ===\n"
            f"{history_text}"
        )
    elif current_price is not None:
        price_section = (
            f"=== PREZZO CORRENTE — {fuel_type} (rilevato il {today}) ===\n"
            f"  Prezzo locale: {current_price:.4f} EUR/L\n"
            f"  (Primo giorno di monitoraggio — storico in costruzione)"
        )
    else:
        price_section = f"=== CARBURANTE: {fuel_type} — data: {today} ==="

    # ---- National average context ----------------------------------------
    if national_average is not None:
        ref_price = current_price or (history[-1].cheapest if history and history[-1].cheapest else None)
        if ref_price is not None:
            diff_pct = (ref_price - national_average) / national_average * 100
            sign = "sopra" if diff_pct >= 0 else "sotto"
            nat_section = (
                f"\n=== CONFRONTO NAZIONALE ===\n"
                f"• Media nazionale {fuel_type}: {national_average:.4f} EUR/L\n"
                f"• Prezzo locale: {abs(diff_pct):.1f}% {sign} la media nazionale"
            )
        else:
            nat_section = (
                f"\n=== CONFRONTO NAZIONALE ===\n"
                f"• Media nazionale {fuel_type}: {national_average:.4f} EUR/L"
            )
    else:
        nat_section = ""

    # ---- Statistical indicators (adaptive) --------------------------------
    if prediction is not None:
        vol_txt = (
            f"{prediction.price_volatility:.4f} (CV)" if prediction.price_volatility is not None else "N/D"
        )
        mom_txt = (
            f"{prediction.price_momentum:+.2f}% (7gg vs 7gg prec.)" if prediction.price_momentum is not None else "N/D"
        )
        acc_txt = (
            f"{prediction.price_acceleration:+.6f} EUR/g²" if prediction.price_acceleration is not None else "N/D"
        )
        wk_txt  = f"{prediction.weekly_change_pct:+.2f}%"  if prediction.weekly_change_pct  is not None else "N/D"
        mo_txt  = f"{prediction.monthly_change_pct:+.2f}%" if prediction.monthly_change_pct is not None else "N/D"
        p3d     = f"{prediction.predicted_price_3d:.4f} EUR/L" if prediction.predicted_price_3d is not None else "N/D"
        stats_section = f"""
=== MODELLO STATISTICO ===
• Tendenza:               {prediction.trend_direction} ({prediction.trend_pct_7d:+.2f}% in 7 giorni)
• Confidenza / metodo:    {prediction.confidence} / {prediction.method_used}
• Previsione domani:      {prediction.predicted_prices[0]:.4f} EUR/L
• Previsione +3 giorni:   {p3d}
• Volatilità:             {vol_txt}
• Momentum:               {mom_txt}
• Accelerazione:          {acc_txt}
• Variaz. settimanale:    {wk_txt}
• Variaz. mensile:        {mo_txt}"""
        context_verb = "che giustificano o modificano la tendenza statistica osservata"
    else:
        stats_section = (
            "\n=== MODELLO STATISTICO ===\n"
            "  Non ancora disponibile (primo giorno di monitoraggio)."
        )
        context_verb = "che determinano il livello di prezzo attuale"

    # ---- Area statistics section ----------------------------------------
    if area_stats:
        area_lines = ["\n=== AREA LOCALE OGGI ==="]
        if area_stats.get("cheapest_price") is not None:
            area_lines.append(f"• Prezzo più basso:    {area_stats['cheapest_price']:.4f} EUR/L")
        if area_stats.get("average_price") is not None:
            area_lines.append(f"• Prezzo medio area:   {area_stats['average_price']:.4f} EUR/L")
        if (area_stats.get("cheapest_price") is not None
                and area_stats.get("average_price") is not None):
            spread = area_stats["average_price"] - area_stats["cheapest_price"]
            area_lines.append(f"• Spread min/media:    {spread:.4f} EUR/L")
        if area_stats.get("self_cheapest_price") is not None:
            area_lines.append(f"• Miglior self:        {area_stats['self_cheapest_price']:.4f} EUR/L")
        if area_stats.get("servito_cheapest_price") is not None:
            area_lines.append(f"• Miglior servito:     {area_stats['servito_cheapest_price']:.4f} EUR/L")
        if area_stats.get("station_count"):
            area_lines.append(f"• Stazioni nell'area:  {area_stats['station_count']}")
        area_section = "\n".join(area_lines)
    else:
        area_section = ""

    # ---- Top stations section -------------------------------------------
    if top_stations:
        top_lines = ["\n=== TOP STAZIONI LOCALI ==="]
        for s in top_stations[:5]:
            parts: list[str] = []
            bandiera = s.get("bandiera", "").strip()
            nome = s.get("nome", "").strip()
            label = f"{bandiera} — {nome}" if bandiera and nome else (bandiera or nome or "?")
            dist = s.get("distance_km")
            dist_txt = f"{dist:.1f}km" if dist is not None else ""
            svc = "self" if s.get("is_self") else "serv"
            price = s.get("price")
            price_txt = f"{price:.3f}" if price is not None else "N/D"
            top_lines.append(f"  {label}  {dist_txt}  [{svc}] {price_txt} EUR/L")
        top_stations_section = "\n".join(top_lines)
    else:
        top_stations_section = ""

    # ---- Previous AI feedback loop section ------------------------------
    if previous_ai and (
        previous_ai.get("brief") or previous_ai.get("risk_level") or previous_ai.get("price_3d")
    ):
        prev_lines = ["\n=== CALIBRAZIONE: ANALISI AI PRECEDENTE ==="]
        if previous_ai.get("brief"):
            prev_lines.append(f"• Sintesi precedente:  \"{previous_ai['brief']}\"")
        if previous_ai.get("risk_level"):
            prev_lines.append(f"• Rischio stimato:     {previous_ai['risk_level']}")
        if previous_ai.get("price_3d") is not None:
            prev_price_3d: float = previous_ai["price_3d"]
            prev_lines.append(f"• Prezzo +3g stimato:  {prev_price_3d:.4f} EUR/L")
            if current_price is not None:
                diff = current_price - prev_price_3d
                sign = "+" if diff >= 0 else ""
                prev_lines.append(
                    f"• Prezzo attuale:      {current_price:.4f} EUR/L "
                    f"(scostamento: {sign}{diff:.4f} EUR/L)"
                )
        prev_lines.append(
            "→ Tieni conto dell'accuratezza della previsione precedente "
            "nel calibrare quella attuale."
        )
        previous_ai_section = "\n".join(prev_lines)
    else:
        previous_ai_section = ""

    # ---- Weekly price pattern (average by day-of-week) ------------------
    weekly_pattern = _weekly_price_pattern(history)
    if weekly_pattern:
        weekly_section = f"\n=== PATTERN SETTIMANALE PREZZI (medie per giorno) ===\n{weekly_pattern}"
    else:
        weekly_section = ""

    return f"""=== DATA ANALISI: {today} ===
{market_section}
{price_section}
{stats_section}
{nat_section}
{area_section}
{top_stations_section}
{previous_ai_section}
{weekly_section}

=== STRUTTURA FISCALE {fuel_type.upper()} (IT) ===
• Accisa: {accisa:.4f} EUR/L  •  IVA: 22%  •  Componente fiscale totale: ~65% del prezzo finale

=== CONTESTO STAGIONALE ===
{seasonal}

=== FRAMEWORK DI ANALISI — considera TUTTI i fattori rilevanti ===

MERCATO PETROLIFERO GLOBALE
• Prezzo Brent/WTI: tendenza recente, spread Brent-WTI, contango/backwardation
• OPEC+: ultime decisioni su quote produzione, compliance dei singoli paesi (Saudi Arabia, UAE, Iraq,
  Russia), eventuali tagli volontari straordinari o riunioni straordinarie
• Offerta non-OPEC+: shale USA (rig count, DUC wells), Canada oil sands, Brasile, Norvegia, Guyana
• Riserva strategica USA (SPR): rilasci o ricostituzione
• Domanda globale: ripresa cinese (import petrolio, PMI), ciclo industriale europeo, stagionalità USA

TENSIONI GEOPOLITICHE E SUPPLY DISRUPTIONS
• Russia-Ucraina: sanzioni petrolio/gas russo, price cap G7/UE ($60/bbl), rotte alternative,
  shadow fleet, manutenzione oleodotti (Druzhba), flussi LNG
• Medio Oriente: tensioni Iran-USA/Israele (rischio chiusura Stretto di Hormuz = ~20% commercio globale),
  accordi Abraham, stabilità Golfo Persico
• Mar Rosso / Houthi Yemen: attacchi alle navi cargo (+10-15 giorni vs rotta Suez, +costi assicurazione),
  rotta alternativa circumnavigazione Africa (Baltic Clean Tanker Index)
• Libya: frammentazione politica, interruzioni produzione (spesso sotto quota OPEC+)
• Algeria / Nigeria / Angola: affidabilità forniture verso Europa, gasdotti verso Italia (Medgaz, TMPC)
• Venezuela: sanzioni USA, ripresa produzione, accordi con Chevron
• Iraq Kurdistan: controversie oleodotto Turchia-Iraq

RAFFINAZIONE E LOGISTICA EUROPEA
• Capacità raffinazione europea: manutenzioni stagionali (tipicamente primavera/autunno),
  chiusure per transizione energetica
• Raffinerie italiane (ENI Sannazzaro de' Burgondi, Saras Cagliari/Sarroch, Italiana Petroli/API Falconara,
  ENI Taranto, Kuwait Petroleum Milazzo): eventuali fermi tecnici o problemi operativi
• Crack spread benzina/gasolio: margini di raffinazione come indicatore di pressione sui prezzi retail
• Stoccaggi prodotti raffinati UE (report AIPe/Euroilstock): livelli vs medie stagionali
• Costi noli petroliere: Baltic Dirty Tanker Index, Baltic Clean Tanker Index

FATTORI MACROECONOMICI E VALUTARI
• EUR/USD: ogni +1% EUR/USD → circa -0.7/1 cent/L in Italia; BCE vs Fed divergenza politica monetaria
• Commodity correlate: gas naturale TTF (impatto costi energetici raffinazione), carbone
• Carbon credits ETS (EU ETS): prezzo CO₂ e impatto su costi raffinazione
• Inflazione e PIL eurozona/Italia: impatto su domanda carburanti

POLITICHE ITALIANE E UE
• Governo italiano: eventuale rinnovo/modifica sconti accise, tetti di prezzo, misure di emergenza
• UE: RED III (quote biocarburanti), fit-for-55, eventuali sanzioni energetiche aggiuntive a Russia
• Transizione energetica: velocità adozione EV in Italia (impatto domanda benzina/diesel a medio termine)
• ARERA e Garante prezzi carburanti: eventuali interventi regolatori

=== OUTPUT RICHIESTO ===
Rispondi SOLO in italiano con questo formato esatto:

**ANALISI** (4-6 frasi):
[Analisi geopolitica e di mercato che spiega i fattori principali {context_verb}.
Integra la tua conoscenza aggiornata con i dati forniti. Sii specifico su quali eventi
concreti stanno influenzando o potrebbero influenzare il prezzo.]

**STIMA 3 GIORNI**:
[Una frase sulla tua stima del prezzo tra 3 giorni considerando tutti i fattori sopra.
Poi su una riga separata SOLO il tag: [PREZZO_3G:X.XXX] con il prezzo stimato in EUR/L (es. [PREZZO_3G:1.752])]

**PREVISIONE 7 GIORNI**:
[Una frase sulla tua previsione dei prezzi nei prossimi 7 giorni.
Poi su una riga separata SOLO il tag con 7 prezzi in EUR/L separati da virgola:
[PREZZI_7G:X.XXX,X.XXX,X.XXX,X.XXX,X.XXX,X.XXX,X.XXX]
(giorno +1, +2, +3, +4, +5, +6, +7 — usa il punto come separatore decimale, es. [PREZZI_7G:1.750,1.748,1.745,1.743,1.742,1.741,1.740])]

**RISCHIO RINCARI 2 SETTIMANE**:
[Una frase sul rischio.
Poi su una riga separata SOLO il tag: [RISCHIO:basso] oppure [RISCHIO:medio] oppure [RISCHIO:alto]]

**SINTESI**:
[Una sola frase (max 12 parole) che riassume il quadro attuale, da mostrare come stato del sensore.
Es: "Brent stabile, OPEC+ invariato, prezzi locali in linea con media."
Poi su una riga separata SOLO il tag: [SINTESI:testo max 12 parole]]

Nessuna formula di cortesia. Solo analisi diretta e concisa."""


def _build_market_section(market_context: MarketContext | None) -> str:
    """Build the real-time market data block for the AI prompt.

    Returns an empty string when *market_context* is None so the caller's
    f-string simply produces a blank line — no hard failure.
    """
    if market_context is None:
        return (
            "=== DATI MERCATO IN TEMPO REALE ===\n"
            "  N/D — dati di mercato non disponibili; usa la tua conoscenza aggiornata."
        )

    from datetime import timezone as _tz
    import zoneinfo as _zi

    try:
        rome = _zi.ZoneInfo("Europe/Rome")
        fetch_local = market_context.fetched_at.astimezone(rome).strftime("%H:%M")
    except Exception:
        fetch_local = market_context.fetched_at.strftime("%H:%M UTC")

    lines: list[str] = [f"=== DATI MERCATO IN TEMPO REALE (aggiornati alle {fetch_local}) ==="]

    if market_context.brent_usd is not None:
        chg = f"  ({market_context.brent_change_pct:+.1f}% 24h)" if market_context.brent_change_pct is not None else ""
        lines.append(f"• Brent crude:  {market_context.brent_usd:.2f} USD/bbl{chg}")
        if market_context.brent_eur is not None:
            cost_floor = market_context.brent_eur / 159  # 1 bbl ≈ 159 L
            lines.append(
                f"  = {market_context.brent_eur:.2f} EUR/bbl"
                f"  (~{cost_floor:.4f} EUR/L costo grezzo teorico pre-raffinazione)"
            )

    if market_context.ttf_eur is not None:
        chg_ttf = f"  ({market_context.ttf_change_pct:+.1f}% 24h)" if market_context.ttf_change_pct is not None else ""
        lines.append(f"• Gas TTF:       {market_context.ttf_eur:.2f} EUR/MWh{chg_ttf}  ← costo energetico raffinerie")

    if market_context.ets_eur is not None:
        lines.append(f"• CO₂ ETS:       {market_context.ets_eur:.2f} EUR/ton  ← costo emissioni raffinazione")

    if market_context.eurusd is not None:
        lines.append(f"• EUR/USD:        {market_context.eurusd:.4f}  (ogni +1% EUR/USD ≈ -0.7/1 cent/L prezzi IT)")

    if market_context.news_headlines:
        lines.append("• Notizie recenti (Google News):")
        for h in market_context.news_headlines:
            lines.append(f"  - {h}")

    lines.append(
        "NOTA: Questi dati sono AGGIORNATI A OGGI. "
        "Basati su questi numeri reali come fonte primaria, non su valori del tuo training."
    )
    return "\n".join(lines)


def _parse_risk_level(text: str) -> str | None:
    """Extract risk level from the AI response tag [RISCHIO:xxx]."""
    match = _RISK_PATTERN.search(text)
    if match:
        return match.group(1).lower()
    return None


def _parse_price_3d(text: str) -> float | None:
    """Extract 3-day price estimate from the AI response tag [PREZZO_3G:X.XXX]."""
    match = _PRICE_3D_PATTERN.search(text)
    if match:
        try:
            return round(float(match.group(1).replace(",", ".")), 4)
        except ValueError:
            pass
    return None


def _parse_brief(text: str) -> str | None:
    """Extract the one-line AI summary.

    First tries the structured [SINTESI:...] tag. Falls back to extracting the
    first non-empty sentence that follows the **SINTESI**: section header, in
    case the model outputs the section text but omits the tag markers.
    """
    # Primary: tag-based extraction
    match = _SINTESI_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    # Fallback: look for the SINTESI section and grab the first meaningful line
    sintesi_section = re.search(
        r"\*\*SINTESI\*\*[:\s]*\n(.+?)(?:\n|$)", text, re.IGNORECASE
    )
    if sintesi_section:
        candidate = sintesi_section.group(1).strip()
        # Strip any residual markdown or bracket artifacts and limit length
        candidate = re.sub(r"[\[\]`*_]", "", candidate).strip()
        if candidate:
            return candidate[:150]

    return None


def _parse_prices_7d(text: str) -> list[float] | None:
    """Extract 7-day price forecast from [PREZZI_7G:1.750,1.748,...] tag.

    Expects exactly 7 dot-separated prices in EUR/L, comma-delimited.
    Returns None if the tag is absent or any value is outside a plausible range.
    """
    match = _PRICES_7D_PATTERN.search(text)
    if not match:
        return None
    try:
        values = [round(float(v), 4) for v in match.group(1).split(",")]
        if len(values) == 7 and all(_MIN_PLAUSIBLE_PRICE <= v <= _MAX_PLAUSIBLE_PRICE for v in values):
            return values
    except ValueError:
        pass
    return None


def _weekly_price_pattern(history: list[DailySnapshot]) -> str | None:
    """Return average cheapest price per day-of-week from history, or None.

    With ≥14 days of history each weekday should have at least 2 observations,
    giving a reliable intra-week pattern the AI can use to recognise whether
    today is a typically cheap or expensive day for re-fuelling.
    """
    day_prices: dict[int, list[float]] = {i: [] for i in range(7)}
    for s in history:
        if s.cheapest is None:
            continue
        try:
            d = date.fromisoformat(s.date)
            day_prices[d.weekday()].append(s.cheapest)
        except (ValueError, AttributeError):
            continue

    if not any(day_prices.values()):
        return None

    day_names = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    parts: list[str] = []
    for i, name in enumerate(day_names):
        prices = day_prices[i]
        if prices:
            parts.append(f"{name}={mean(prices):.3f}")
        else:
            parts.append(f"{name}=N/D")
    return "  ".join(parts)


def _ai_system_message() -> str:
    """Return the system message for AI calls, including today's date."""
    today = date.today().isoformat()
    return (
        f"Sei un analista senior del mercato energetico italiano. "
        f"La data di oggi è {today}. "
        "Usa i dati di mercato in tempo reale forniti nel prompt come fonte primaria "
        "per il prezzo del Brent, TTF, ETS e EUR/USD. "
        "La tua conoscenza storica è complementare, non sostitutiva, dei dati attuali. "
        "Rispondi SEMPRE in italiano e SOLO nel formato strutturato richiesto."
    )


async def _call_claude(
    session: aiohttp.ClientSession,
    api_key: str,
    prompt: str,
    model: str | None = None,
) -> str | None:
    """Call the Anthropic Claude API."""
    from .const import DEFAULT_AI_MODEL_CLAUDE
    import aiohttp as _aiohttp

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model or DEFAULT_AI_MODEL_CLAUDE,
        "max_tokens": 2500,
        "system": _ai_system_message(),
        "messages": [{"role": "user", "content": prompt}],
    }
    async with session.post(
        _CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=45),
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
    model: str | None = None,
) -> str | None:
    """Call the OpenAI Chat Completions API."""
    from .const import DEFAULT_AI_MODEL_OPENAI
    import aiohttp as _aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or DEFAULT_AI_MODEL_OPENAI,
        "max_tokens": 3000,
        "messages": [
            {"role": "system", "content": _ai_system_message()},
            {"role": "user", "content": prompt},
        ],
    }
    async with session.post(
        _OPENAI_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=45),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip() or None
    return None
