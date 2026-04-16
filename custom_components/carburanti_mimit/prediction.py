"""Pure-Python price trend analysis and 7-day forecast with AI geopolitical enrichment.

Algorithm (ensemble)
--------------------
1. Extract price series from the last 90 days of HistoryStorage.
2. Interpolate small gaps (≤ 3 consecutive missing days) linearly.
3. Fit Exponentially Weighted OLS (EWOLS) over the last 21 points — recent
   prices get exponentially higher weight (alpha=0.15).
4. Run Holt's Double Exponential Smoothing (level + trend, α=0.3, β=0.1).
5. Run AR(1) on first-differenced series — models short-term price-change
   persistence; each daily change is autocorrelated with the previous one.
6. Ensemble (dynamic, based on available data and EWOLS R²):
   - R²≥0.6 AND ≥14 pts AND ≥6 pts for AR1:
       35% EWOLS + 35% Holt + 30% AR1  → ``ensemble_ols_holt_ar1``
   - R²≥0.6 AND ≥14 pts, AR1 unavailable:
       60% EWOLS + 40% Holt            → ``ensemble_ols_holt``
   - R²<0.6 AND ≥5 pts AND ≥6 pts for AR1:
       55% Holt + 45% AR1              → ``ensemble_holt_ar1``
   - R²<0.6 AND ≥5 pts, AR1 unavailable:
       Holt only                        → ``holt_exponential_smoothing``
   - Otherwise: linearly-weighted moving average (WMA) fallback.
7. Mean-reversion adjustment: when the current price deviates >4 % from the
   long-term mean (all available history), a gentle linear pull is applied
   before clamping — prevents unbounded trend extrapolation.
8. Volatility-adaptive clamping:
   [max(0.5×μ₇, μ₇-3σ), min(2.0×μ₇, μ₇+3σ)]
9. Confidence: high (≥30 pts, R²≥0.7) | medium (≥14 pts) | low (< 14 pts).

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
  • Up to 90-day local price history
  • Statistical indicators (trend, volatility, momentum, acceleration)
  • Seasonal demand context
  • Italian excise-tax (accise) and VAT structure

The AI receives a dedicated *system* message with today's date and analyst role.
A separate *user* message carries all the data.  Max tokens: 2000 (Claude/OpenAI)
to prioritize richer analysis over token savings.

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
_EWOLS_FIT_WINDOW = 21  # use up to 21 recent points for EWOLS (was 14)
_HOLT_ALPHA  = 0.3    # Holt level smoothing
_HOLT_BETA   = 0.1    # Holt trend smoothing

# 2-method ensemble weights (EWOLS + Holt, when AR1 unavailable and R²≥threshold)
_ENSEMBLE_WLS_WEIGHT  = 0.6
_ENSEMBLE_HOLT_WEIGHT = 0.4
# 3-method ensemble weights (EWOLS + Holt + AR1)
_ENSEMBLE3_WLS_WEIGHT  = 0.35
_ENSEMBLE3_HOLT_WEIGHT = 0.35
_ENSEMBLE3_AR1_WEIGHT  = 0.30
# 2-method ensemble weights (Holt + AR1, when R²<threshold)
_ENSEMBLE_HOLT_AR1_HOLT_WEIGHT = 0.55
_ENSEMBLE_HOLT_AR1_AR1_WEIGHT  = 0.45

# AR(1) on first differences
_AR1_MIN_POINTS = 6        # need ≥6 prices to estimate 5 differences stably
_AR1_PHI_MAX    = 0.90     # clamp phi to [-0.90, 0.90] to prevent explosion

# Mean-reversion nudge (applied when current price deviates from long-term mean)
_MEAN_REVERSION_THRESHOLD = 0.04   # 4 % deviation triggers the nudge
_MEAN_REVERSION_DAILY_RATE = 0.004 # max 0.4 % of mean pulled back per day

# AI API endpoints
_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Pattern used to extract risk level from AI response
_RISK_PATTERN = re.compile(r"\[RISCHIO:(basso|medio|alto)\]", re.IGNORECASE)
# Pattern used to extract AI 3-day price estimate  [PREZZO_3G:1.750]
_PRICE_3D_PATTERN = re.compile(r"\[PREZZO_3G:([\d]+[.,][\d]+)\]", re.IGNORECASE)
# Pattern used to extract the one-line AI summary  [SINTESI:testo breve]
_SINTESI_PATTERN = re.compile(r"\[SINTESI:([^\]]{1,200})\]", re.IGNORECASE)
# Max historical points injected into AI prompt (daily snapshots from storage).
_AI_PROMPT_MAX_HISTORY_DAYS = 90
_AI_PROMPT_DETAILED_RECENT_DAYS = 30
_AI_PROMPT_WEEKLY_BLOCK = 7
_AI_MAX_RESPONSE_TOKENS = 2000
_AI_MIN_RESPONSE_TOKENS = 900

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

    # Statistical 3-day and 7-day forecasts (already in predicted_prices)
    predicted_price_3d: float | None   = None
    predicted_price_7d: float | None   = None   # predicted_prices[6]

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

    # --- Ensemble: EWOLS + Holt + AR(1) on differences ----------------------
    fit_window = prices[-_EWOLS_FIT_WINDOW:]   # up to 21 recent points
    n = len(fit_window)
    xs = list(range(n))

    # Method 1: Exponentially Weighted OLS (recent prices get higher weight)
    slope_ew, intercept_ew, r2 = _ewols_regression(xs, fit_window, _EWOLS_ALPHA)

    # Method 2: Holt's Double EMA (level + trend) — trained on all history
    holt_pred: list[float] | None = None
    if len(prices) >= _MIN_POINTS_HOLT:
        holt_pred = _holt_exponential_smoothing(prices, _HOLT_ALPHA, _HOLT_BETA, 7)

    # Method 3: AR(1) on first differences — captures change-direction persistence
    ar1_pred: list[float] | None = None
    if len(prices) >= _AR1_MIN_POINTS:
        ar1_pred = _ar1_diff_forecast(prices, 7)

    # Ensemble selection (dynamic weights based on available methods and R²)
    if r2 >= _R2_THRESHOLD and len(prices) >= _MIN_POINTS_MEDIUM:
        ewols_f = [intercept_ew + slope_ew * (n + i) for i in range(7)]
        if holt_pred is not None and ar1_pred is not None:
            method = "ensemble_ols_holt_ar1"
            predicted = [
                _ENSEMBLE3_WLS_WEIGHT * ew
                + _ENSEMBLE3_HOLT_WEIGHT * h
                + _ENSEMBLE3_AR1_WEIGHT * a
                for ew, h, a in zip(ewols_f, holt_pred, ar1_pred)
            ]
        elif holt_pred is not None:
            method = "ensemble_ols_holt"
            predicted = [
                _ENSEMBLE_WLS_WEIGHT * ew + _ENSEMBLE_HOLT_WEIGHT * h
                for ew, h in zip(ewols_f, holt_pred)
            ]
        else:
            method = "moving_average"
            predicted = _weighted_moving_average_forecast(prices, slope_ew)
        slope = slope_ew
    elif holt_pred is not None:
        if ar1_pred is not None:
            method = "ensemble_holt_ar1"
            predicted = [
                _ENSEMBLE_HOLT_AR1_HOLT_WEIGHT * h + _ENSEMBLE_HOLT_AR1_AR1_WEIGHT * a
                for h, a in zip(holt_pred, ar1_pred)
            ]
        else:
            method = "holt_exponential_smoothing"
            predicted = holt_pred
        slope = (predicted[-1] - predicted[0]) / 6.0 if len(predicted) == 7 else 0.0
    else:
        method = "moving_average"
        slope = slope_ew
        predicted = _weighted_moving_average_forecast(prices, slope)

    # Mean-reversion nudge (only when current price far from long-term mean)
    predicted = _mean_reversion_adjustment(predicted, prices)

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
        predicted_price_7d=predicted[6] if len(predicted) >= 7 else None,
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
# AR(1) on first differences
# ---------------------------------------------------------------------------

def _ar1_diff_forecast(
    prices: list[float],
    n_forecast: int = 7,
) -> list[float]:
    """AR(1) on first-differenced price series (ARIMA(1,1,0) style).

    Models short-term persistence of daily price *changes*:
    Δp[t] = μ_Δ + φ · (Δp[t-1] − μ_Δ) + ε[t]

    The AR coefficient φ is estimated via the lag-1 Yule-Walker equation on
    the de-meaned differences.  We clamp |φ| ≤ 0.90 to prevent explosive
    long-horizon extrapolation — fuel prices are bounded by market forces.

    Why differences rather than levels?
    • Fuel prices are non-stationary (trending); AR on levels would conflate
      the long-run mean with the current level and produce biased estimates.
    • Daily *changes* are approximately stationary: small, with weak memory.
    • A positive φ means "if price rose yesterday it tends to rise again today"
      (momentum); φ near 0 means changes are unpredictable from history alone.

    Returns *n_forecast* predicted price levels (not changes).
    """
    n = len(prices)
    if n < _AR1_MIN_POINTS:
        return [round(prices[-1], 4)] * n_forecast

    diffs = [prices[i] - prices[i - 1] for i in range(1, n)]
    nd = len(diffs)
    mu_d = mean(diffs)

    # De-mean differences for Yule-Walker
    yd = [d - mu_d for d in diffs]
    if nd >= 2:
        cov = sum(yd[i] * yd[i - 1] for i in range(1, nd)) / (nd - 1)
        var = sum(yi ** 2 for yi in yd) / nd
        phi = cov / var if var > 1e-12 else 0.0
        phi = max(-_AR1_PHI_MAX, min(_AR1_PHI_MAX, phi))
    else:
        phi = 0.0

    # Iterate forecasts from the last observed price
    last_level = prices[-1]
    last_innov = diffs[-1] - mu_d   # last de-meaned change

    result: list[float] = []
    innov = last_innov
    level = last_level
    for _ in range(n_forecast):
        innov = phi * innov           # AR(1) update on de-meaned change
        delta = mu_d + innov          # predicted change = mean + AR innovation
        level = level + delta
        result.append(round(level, 4))
    return result


# ---------------------------------------------------------------------------
# Mean-reversion nudge
# ---------------------------------------------------------------------------

def _mean_reversion_adjustment(
    predicted: list[float],
    prices: list[float],
) -> list[float]:
    """Apply a gentle pull toward the long-term mean when prices deviate far.

    Activation: only when the current price deviates more than
    *_MEAN_REVERSION_THRESHOLD* (4 %) from the mean of all available history.

    The correction is linear and capped: at most *_MEAN_REVERSION_DAILY_RATE*
    (0.4 %) of the mean per day, so the total 7-day pull is ≤ 2.8 %.

    This prevents the trend-following models from extrapolating indefinitely
    during unusual price spikes or troughs, while having zero effect during
    normal price fluctuations within ±4 % of the historical mean.

    The correction is applied before clamping so the clamping bounds still
    provide the hard safety net.
    """
    n = len(prices)
    if n < _MIN_POINTS_MEDIUM:
        return predicted

    long_mean = mean(prices)
    current = prices[-1]
    if long_mean == 0:
        return predicted

    deviation = (current - long_mean) / long_mean
    if abs(deviation) <= _MEAN_REVERSION_THRESHOLD:
        return predicted  # within normal range — no adjustment

    # Daily pull magnitude (capped per day)
    pull_per_day = min(abs(deviation) * 0.10, _MEAN_REVERSION_DAILY_RATE) * long_mean
    direction = -1.0 if deviation > 0 else 1.0  # pull toward mean

    result: list[float] = []
    for i, p in enumerate(predicted):
        correction = direction * pull_per_day * (i + 1)
        result.append(round(p + correction, 4))
    return result


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
) -> tuple[str | None, str | None, float | None, str | None]:
    """Call an LLM for a geopolitical and market analysis of the price trend.

    Works from day 1: when *prediction* is ``None`` the prompt focuses on pure
    geopolitical/market context for the current price; as history accumulates
    the prompt is progressively enriched with statistical indicators.

    *market_context* (from ``market.py``) injects real-time Brent/TTF/ETS/EUR/USD
    prices and news headlines directly into the prompt so the AI reasons from
    today's actual market data, not its training-data cutoff.

    Returns ``(ai_analysis, ai_risk_level, ai_price_3d, ai_brief)`` or
    ``(None, None, None, None)`` on error.
    - ai_analysis:    full geopolitical analysis text
    - ai_risk_level:  "basso" | "medio" | "alto" parsed from [RISCHIO:...] tag
    - ai_price_3d:    AI-estimated price in 3 days parsed from [PREZZO_3G:...] tag
    - ai_brief:       one-sentence summary parsed from [SINTESI:...] tag
    """
    from .const import AI_PROVIDER_CLAUDE, AI_PROVIDER_OPENAI  # avoid circular at top

    prompt = _build_geopolitical_prompt(
        history, fuel_type, prediction, current_price, national_average, market_context
    )

    try:
        if provider == AI_PROVIDER_CLAUDE:
            text = await _call_claude(session, api_key, prompt, claude_model)
        elif provider == AI_PROVIDER_OPENAI:
            text = await _call_openai(session, api_key, prompt, openai_model)
        else:
            return None, None, None, None

        if not text:
            _LOGGER.warning("AI call returned empty response")
            return None, None, None, None

        risk = _parse_risk_level(text)
        price_3d = _parse_price_3d(text)
        brief = _parse_brief(text)
        if brief is None:
            _LOGGER.warning(
                "AI response missing [SINTESI:] tag — response preview: %.300s",
                text,
            )
        else:
            _LOGGER.debug("AI parsed — risk=%s price_3d=%s brief=%s", risk, price_3d, brief)
        return text.strip(), risk, price_3d, brief

    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("AI prediction call failed (%s: %s) — skipping", type(exc).__name__, exc)
    return None, None, None, None


def _build_geopolitical_prompt(
    history: list[DailySnapshot],
    fuel_type: str,
    prediction: PredictionResult | None,
    current_price: float | None = None,
    national_average: float | None = None,
    market_context: MarketContext | None = None,
) -> str:
    """Build an adaptive prompt for geopolitical + market analysis.

    Enrichment levels:
    - No history  → pure geopolitical/market context for the current price
    - Some history → adds observed price trend
    - Full stats   → adds 7-day forecast, volatility, momentum, acceleration

    When *market_context* is provided the prompt leads with real-time Brent,
    TTF, ETS, EUR/USD prices and news headlines so the AI reasons from today's
    market data, not its training-data cutoff.
    """
    today = date.today().isoformat()
    seasonal = _seasonal_context()
    accisa = _ACCISE.get(fuel_type, 0.0)

    # ---- Real-time market data section (Brent, TTF, ETS, EUR/USD, news) -----
    market_section = _build_market_section(market_context)

    # ---- Price / history section ----------------------------------------
    recent = history[-_AI_PROMPT_MAX_HISTORY_DAYS:] if history else []
    if recent:
        history_text = _format_history_for_prompt(recent)
        data_days = len([s for s in recent if s.cheapest is not None])
        price_section = (
            f"=== DATI STORICI LOCALI — {fuel_type} "
            f"(ultimi {data_days} giorni disponibili, max {_AI_PROMPT_MAX_HISTORY_DAYS}, fino al {today}) ===\n"
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

    return f"""=== DATA ANALISI: {today} ===
{market_section}
{price_section}
{stats_section}
{nat_section}

=== STRUTTURA FISCALE {fuel_type.upper()} (IT) ===
• Accisa: {accisa:.4f} EUR/L  •  IVA: 22%  •  Componente fiscale totale: ~65% del prezzo finale

=== CONTESTO STAGIONALE ===
{seasonal}

=== CONTESTO AGGIUNTIVO DISPONIBILE (massimo dettaglio entro budget token) ===
• Storico locale fino a 90 giorni: dettaglio giornaliero recente + riepilogo settimanale parte più vecchia
• Dati market real-time (Brent/TTF/ETS/EURUSD) + headline recenti per ridurre allucinazioni temporali
• Obiettivo: privilegiare accuratezza previsionale restando nel limite token
• In caso di conflitto, priorità ai dati numerici forniti nel prompt rispetto a stime generiche

=== PRIORITÀ CAUSALE (breve periodo, prezzo alla pompa) ===
Pesa esplicitamente i driver in quest'ordine:
1) Geopolitica internazionale e rischio supply shock (guerre, sanzioni, stretto di Hormuz, Mar Rosso)
2) Decisioni governi / regolatori (accise, sussidi, misure emergenziali, policy UE)
3) Variabili mercato-finanza (Brent, crack spread, EUR/USD, TTF, ETS)
4) Dinamiche locali/statistiche (trend ultimi giorni, stagionalità, rumore locale)
Se i fattori 1-2 sono in forte movimento, prevalgono sull'inerzia statistica.

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
concreti stanno influenzando o potrebbero influenzare il prezzo.
Nelle prime 2 frasi dai priorità a geopolitica e decisioni governative/regolatorie.]

**STIMA 3 GIORNI**:
[Una frase sulla tua stima del prezzo tra 3 giorni considerando tutti i fattori sopra.
Poi su una riga separata SOLO il tag: [PREZZO_3G:X.XXX] con il prezzo stimato in EUR/L (es. [PREZZO_3G:1.752])]

**RISCHIO RINCARI 2 SETTIMANE**:
[Una frase sul rischio.
Poi su una riga separata SOLO il tag: [RISCHIO:basso] oppure [RISCHIO:medio] oppure [RISCHIO:alto]]

**SINTESI**:
[Una sola frase (max 12 parole) che riassume il quadro attuale, da mostrare come stato del sensore.
Es: "Brent stabile, OPEC+ invariato, prezzi locali in linea con media."
Poi su una riga separata SOLO il tag: [SINTESI:testo max 12 parole]]

**JSON FACOLTATIVO (una sola riga, senza markdown)**:
[{{
  "driver_scores": {{"geopolitica": 0-100, "policy": 0-100, "mercato_fx": 0-100, "statistica_locale": 0-100}},
  "scenario": {{"base": "up|down|stable", "upside_risk": "basso|medio|alto", "downside_risk": "basso|medio|alto"}}
}}]

Nessuna formula di cortesia. Solo analisi diretta e concisa."""


def _format_history_for_prompt(recent: list[DailySnapshot]) -> str:
    """Format history for prompt with token-aware compaction.

    Keeps daily detail for recent days and compresses older days into weekly
    summary blocks to stay within token limits while preserving context.
    """
    if len(recent) <= _AI_PROMPT_DETAILED_RECENT_DAYS:
        return "\n".join(
            f"  {s.date}: {s.cheapest:.4f} EUR/L" if s.cheapest is not None else f"  {s.date}: N/D"
            for s in recent
        )

    split = len(recent) - _AI_PROMPT_DETAILED_RECENT_DAYS
    older = recent[:split]
    newest = recent[split:]

    lines: list[str] = ["  -- RIEPILOGO STORICO (parte meno recente, blocchi settimanali) --"]
    for i in range(0, len(older), _AI_PROMPT_WEEKLY_BLOCK):
        chunk = older[i:i + _AI_PROMPT_WEEKLY_BLOCK]
        values = [s.cheapest for s in chunk if s.cheapest is not None]
        if values:
            lines.append(
                f"  {chunk[0].date}→{chunk[-1].date}: "
                f"avg={mean(values):.4f} min={min(values):.4f} max={max(values):.4f}"
            )
        else:
            lines.append(f"  {chunk[0].date}→{chunk[-1].date}: N/D")

    lines.append("  -- DETTAGLIO GIORNALIERO (ultimi 30 giorni) --")
    lines.extend(
        f"  {s.date}: {s.cheapest:.4f} EUR/L" if s.cheapest is not None else f"  {s.date}: N/D"
        for s in newest
    )
    return "\n".join(lines)


def _response_token_budget(prompt: str) -> int:
    """Return a dynamic max_tokens budget based on prompt size.

    We keep responses detailed when prompt is moderate, but reduce the response
    token cap when prompt is very long to avoid unnecessary token pressure.
    """
    chars = len(prompt)
    if chars < 10_000:
        return _AI_MAX_RESPONSE_TOKENS
    if chars < 16_000:
        return 1_500
    if chars < 22_000:
        return 1_200
    return _AI_MIN_RESPONSE_TOKENS


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
        "max_tokens": _response_token_budget(prompt),
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
        "max_tokens": _response_token_budget(prompt),
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
