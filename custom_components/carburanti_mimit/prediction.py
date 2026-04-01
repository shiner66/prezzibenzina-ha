"""Pure-Python price trend analysis and 7-day forecast with AI geopolitical enrichment.

Algorithm
---------
1. Extract price series from the last 30 days of HistoryStorage.
2. Interpolate small gaps (≤ 3 consecutive missing days) linearly.
3. Compute OLS linear regression over the last 14 points.
4. If R² > 0.6 → use linear extrapolation for the 7-day forecast.
   Otherwise → use a linearly-weighted moving average (WMA).
5. Clamp predictions to [0.5 × mean_7d, 2.0 × mean_7d] to avoid nonsense.
6. Confidence: high (≥30 pts, R²≥0.7) | medium (≥14 pts) | low (< 14 pts).

Additional statistical indicators
----------------------------------
- price_volatility: normalised standard deviation of recent prices (σ/μ)
- price_momentum:   recent-7d mean minus prior-7d mean, as % of prior-7d mean
  Positive → accelerating upward; negative → accelerating downward.
- price_acceleration: second-order change (slope of slope), in EUR/day²

No external dependencies — only the standard library is used.

Optional AI enrichment with geopolitical context
-------------------------------------------------
When ``CONF_AI_PROVIDER`` is configured, ``async_ai_prediction()`` calls the
selected LLM API with a rich prompt that includes:

  • 30-day price history
  • Statistical indicators (trend, volatility, momentum, acceleration)
  • Seasonal demand context (summer/winter driving, heating season)
  • Explicit request to analyse geopolitical factors known to the model:
    - Brent/WTI crude price dynamics
    - OPEC+ production decisions
    - Geopolitical tensions affecting supply routes
    - EUR/USD exchange rate impact on import costs
    - Italian excise-tax (accise) and VAT components
    - Refinery capacity and maintenance cycles

The LLM response is expected to contain a risk-level tag ``[RISCHIO:basso]``,
``[RISCHIO:medio]``, or ``[RISCHIO:alto]`` which is parsed and stored in
``ai_risk_level``; the full text goes into ``ai_analysis``.

Errors fall back silently to ``None``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from statistics import mean, stdev
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
_MIN_POINTS_LOW = 1
_MIN_POINTS_MEDIUM = 14
_MIN_POINTS_HIGH = 30
_MAX_GAP_INTERPOLATE = 3

# AI API endpoints
_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Pattern used to extract risk level from AI response
_RISK_PATTERN = re.compile(r"\[RISCHIO:(basso|medio|alto)\]", re.IGNORECASE)


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

    # AI enrichment
    ai_analysis: str | None           = field(default=None)
    ai_risk_level: str | None         = field(default=None)  # "basso" | "medio" | "alto"


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
            confidence="low",
            method_used="moving_average",
            weekly_change_pct=None,
            monthly_change_pct=None,
            price_volatility=None,
            price_momentum=None,
            price_acceleration=None,
        )

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
) -> tuple[str | None, str | None]:
    """Call an LLM for a geopolitical and market analysis of the price trend.

    Works from day 1: when *prediction* is ``None`` (insufficient history) the
    prompt focuses on pure geopolitical/market context for the current price.
    As history accumulates the prompt is progressively enriched with statistical
    indicators and the 7-day forecast.

    Returns ``(ai_analysis, ai_risk_level)`` or ``(None, None)`` on error.
    """
    from .const import AI_PROVIDER_CLAUDE, AI_PROVIDER_OPENAI  # avoid circular at top

    prompt = _build_geopolitical_prompt(history, fuel_type, prediction, current_price, national_average)

    try:
        if provider == AI_PROVIDER_CLAUDE:
            text = await _call_claude(session, api_key, prompt)
        elif provider == AI_PROVIDER_OPENAI:
            text = await _call_openai(session, api_key, prompt)
        else:
            return None, None

        if not text:
            return None, None

        risk = _parse_risk_level(text)
        return text.strip(), risk

    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("AI prediction call failed (%s) — skipping", exc)
    return None, None


def _build_geopolitical_prompt(
    history: list[DailySnapshot],
    fuel_type: str,
    prediction: PredictionResult | None,
    current_price: float | None = None,
    national_average: float | None = None,
) -> str:
    """Build an adaptive prompt for geopolitical + market analysis.

    The prompt is enriched progressively:
    - No history  → pure geopolitical/market context for the current price
    - Some history → adds observed price trend
    - Full stats   → adds 7-day forecast, volatility, momentum, acceleration
    """
    today = date.today().isoformat()
    seasonal = _seasonal_context()

    # ---- Price / history section ----------------------------------------
    recent = history[-30:] if history else []
    if recent:
        history_lines = [
            f"  {s.date}: {s.cheapest:.4f} EUR/L" if s.cheapest is not None else f"  {s.date}: N/D"
            for s in recent
        ]
        history_text = "\n".join(history_lines)
        data_days = len([s for s in recent if s.cheapest is not None])
        price_section = (
            f"=== DATI STORICI — {fuel_type} (ultimi {data_days} giorni fino al {today}) ===\n"
            f"{history_text}"
        )
    elif current_price is not None:
        price_section = (
            f"=== PREZZO CORRENTE — {fuel_type} (rilevato il {today}) ===\n"
            f"  Prezzo odierno: {current_price:.4f} EUR/L\n"
            f"  (Storico non ancora disponibile — primo giorno di monitoraggio)"
        )
    else:
        price_section = f"=== CARBURANTE: {fuel_type} — data: {today} ==="

    # ---- National average context ----
    if national_average is not None:
        ref_price = current_price or (history[-1].cheapest if history and history[-1].cheapest else None)
        if ref_price is not None:
            diff_pct = (ref_price - national_average) / national_average * 100
            sign = "sopra" if diff_pct >= 0 else "sotto"
            nat_section = (
                f"\n=== CONTESTO NAZIONALE ===\n"
                f"• Media nazionale {fuel_type}: {national_average:.4f} EUR/L\n"
                f"• Prezzo locale vs media nazionale: {abs(diff_pct):.1f}% {sign} la media"
            )
        else:
            nat_section = (
                f"\n=== CONTESTO NAZIONALE ===\n"
                f"• Media nazionale {fuel_type}: {national_average:.4f} EUR/L"
            )
    else:
        nat_section = ""

    # ---- Statistical indicators section (only when prediction exists) ----
    if prediction is not None:
        volatility_text = (
            f"{prediction.price_volatility:.4f} (coefficiente di variazione)"
            if prediction.price_volatility is not None else "N/D"
        )
        momentum_text = (
            f"{prediction.price_momentum:+.2f}% (media 7gg vs settimana precedente)"
            if prediction.price_momentum is not None else "N/D"
        )
        acceleration_text = (
            f"{prediction.price_acceleration:+.6f} EUR/giorno²"
            if prediction.price_acceleration is not None else "N/D"
        )
        weekly_text = (
            f"{prediction.weekly_change_pct:+.2f}%"
            if prediction.weekly_change_pct is not None else "N/D"
        )
        monthly_text = (
            f"{prediction.monthly_change_pct:+.2f}%"
            if prediction.monthly_change_pct is not None else "N/D"
        )
        stats_section = f"""
=== INDICATORI STATISTICI ===
• Tendenza (modello):     {prediction.trend_direction} ({prediction.trend_pct_7d:+.2f}% in 7 giorni)
• Confidenza modello:     {prediction.confidence} (metodo: {prediction.method_used})
• Volatilità:             {volatility_text}
• Momentum prezzi:        {momentum_text}
• Accelerazione:          {acceleration_text}
• Variazione settimanale: {weekly_text}
• Variazione mensile:     {monthly_text}
• Previsione domani:      {prediction.predicted_prices[0]:.4f} EUR/L"""
        analysis_note = (
            "1. Spieghi i fattori di mercato e geopolitici che giustificano la tendenza osservata,"
        )
    else:
        stats_section = (
            "\n=== INDICATORI STATISTICI ===\n"
            "  Dati statistici non ancora disponibili (storico in costruzione)."
        )
        analysis_note = (
            "1. Spieghi i fattori di mercato e geopolitici che determinano il livello di prezzo attuale,"
        )

    return f"""Sei un analista di mercato energetico specializzato nel mercato italiano dei carburanti.

{price_section}
{stats_section}
{nat_section}
=== CONTESTO STAGIONALE ===
{seasonal}

=== RICHIESTA DI ANALISI ===
Fornisci un'analisi concisa (3-5 frasi) in italiano che:

{analysis_note}
   considerando in particolare:
   - Andamento del prezzo del petrolio Brent/WTI e decisioni recenti OPEC+
   - Tensioni geopolitiche che impattano le forniture (Russia, Medio Oriente, Nord Africa)
   - Effetto del tasso di cambio EUR/USD sui costi d'importazione italiani
   - Componente fiscale italiana (accise + IVA ≈ 65% del prezzo finale)
   - Stagionalità della domanda e cicli di manutenzione delle raffinerie europee

2. Valuti il rischio di ulteriori rincari nelle prossime 2 settimane.

3. Concluda con UNA sola riga contenente esattamente il tag di rischio nel formato:
   [RISCHIO:basso] oppure [RISCHIO:medio] oppure [RISCHIO:alto]

Rispondi SOLO in italiano. Sii diretto e informativo, senza formule di cortesia."""


def _parse_risk_level(text: str) -> str | None:
    """Extract risk level from the AI response tag [RISCHIO:xxx]."""
    match = _RISK_PATTERN.search(text)
    if match:
        return match.group(1).lower()
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
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with session.post(
        _CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=30),
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
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with session.post(
        _OPENAI_API_URL,
        headers=headers,
        json=payload,
        timeout=_aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip() or None
    return None
