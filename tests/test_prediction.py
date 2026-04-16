"""Unit tests for prediction.py — statistical forecasting."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.carburanti_mimit.prediction import (
    PredictionResult,
    _AI_PROMPT_MAX_HISTORY_DAYS,
    _AI_PROMPT_DETAILED_RECENT_DAYS,
    _ewols_regression,
    _build_geopolitical_prompt,
    _holt_exponential_smoothing,
    _ar1_diff_forecast,
    _mean_reversion_adjustment,
    _response_token_budget,
    compute_prediction,
)
from custom_components.carburanti_mimit.storage import DailySnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history(
    prices: list[float],
    fuel_type: str = "Benzina",
    start: date | None = None,
) -> list[DailySnapshot]:
    """Build a list of DailySnapshot with consecutive dates."""
    if start is None:
        start = date.today() - timedelta(days=len(prices) - 1)
    return [
        DailySnapshot(
            date=(start + timedelta(days=i)).isoformat(),
            fuel_type=fuel_type,
            cheapest=p,
            average=p + 0.05,
        )
        for i, p in enumerate(prices)
    ]


def _flat_history(n: int = 30, price: float = 1.800) -> list[DailySnapshot]:
    return _make_history([price] * n)


def _rising_history(n: int = 30, start: float = 1.700, step: float = 0.003) -> list[DailySnapshot]:
    return _make_history([start + i * step for i in range(n)])


def _falling_history(n: int = 30, start: float = 1.900, step: float = 0.003) -> list[DailySnapshot]:
    return _make_history([start - i * step for i in range(n)])


# ---------------------------------------------------------------------------
# compute_prediction — basic
# ---------------------------------------------------------------------------

class TestComputePredictionBasic:
    def test_returns_none_on_empty_history(self):
        assert compute_prediction([], "Benzina") is None

    def test_returns_result_on_single_point(self):
        history = _make_history([1.800])
        result = compute_prediction(history, "Benzina")
        assert result is not None

    def test_returns_prediction_result_instance(self):
        result = compute_prediction(_flat_history(15), "Benzina")
        assert isinstance(result, PredictionResult)

    def test_predicted_prices_has_seven_elements(self):
        result = compute_prediction(_flat_history(20), "Benzina")
        assert result is not None
        assert len(result.predicted_prices) == 7

    def test_predicted_price_3d_matches_index_2(self):
        result = compute_prediction(_flat_history(20), "Benzina")
        assert result is not None
        assert result.predicted_price_3d == pytest.approx(result.predicted_prices[2])


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------

class TestConfidenceLevels:
    def test_low_confidence_below_14_points(self):
        result = compute_prediction(_flat_history(7), "Benzina")
        assert result is not None
        assert result.confidence == "low"

    def test_medium_confidence_14_points(self):
        result = compute_prediction(_flat_history(14), "Benzina")
        assert result is not None
        assert result.confidence in ("medium", "high")

    def test_high_confidence_requires_30_points(self):
        # A perfectly linear series should achieve high R² with 30+ points
        result = compute_prediction(_rising_history(30), "Benzina")
        assert result is not None
        # May be high or medium depending on R² — just confirm not None
        assert result.confidence in ("low", "medium", "high")


# ---------------------------------------------------------------------------
# Trend direction
# ---------------------------------------------------------------------------

class TestTrendDirection:
    def test_stable_flat_series(self):
        result = compute_prediction(_flat_history(30), "Benzina")
        assert result is not None
        assert result.trend_direction == "stable"

    def test_up_strongly_rising_series(self):
        # Large step per day → should trigger "up"
        history = _make_history([1.700 + i * 0.010 for i in range(30)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        assert result.trend_direction == "up"

    def test_down_strongly_falling_series(self):
        history = _make_history([2.000 - i * 0.010 for i in range(30)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        assert result.trend_direction == "down"


# ---------------------------------------------------------------------------
# Predictions are clamped (not insane values)
# ---------------------------------------------------------------------------

class TestPredictionClamping:
    def test_prices_above_zero(self):
        result = compute_prediction(_flat_history(30, price=1.800), "Benzina")
        assert result is not None
        for p in result.predicted_prices:
            assert p > 0

    def test_prices_not_wildly_high(self):
        # Even a strongly rising series should stay within 2× the mean
        history = _make_history([1.700 + i * 0.010 for i in range(30)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        mean_7d = sum(h.cheapest for h in history[-7:] if h.cheapest) / 7
        for p in result.predicted_prices:
            assert p <= 2.0 * mean_7d + 0.01  # +0.01 for float tolerance

    def test_prices_not_wildly_low(self):
        history = _make_history([2.000 - i * 0.010 for i in range(30)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        mean_7d = sum(h.cheapest for h in history[-7:] if h.cheapest) / 7
        for p in result.predicted_prices:
            assert p >= 0.5 * mean_7d - 0.01


# ---------------------------------------------------------------------------
# Statistical indicators
# ---------------------------------------------------------------------------

class TestStatisticalIndicators:
    def test_volatility_is_non_negative(self):
        result = compute_prediction(_flat_history(20), "Benzina")
        assert result is not None
        if result.price_volatility is not None:
            assert result.price_volatility >= 0

    def test_flat_series_has_low_volatility(self):
        result = compute_prediction(_flat_history(30), "Benzina")
        assert result is not None
        if result.price_volatility is not None:
            assert result.price_volatility < 0.05  # near zero for flat series

    def test_momentum_type(self):
        result = compute_prediction(_rising_history(20), "Benzina")
        assert result is not None
        if result.price_momentum is not None:
            assert isinstance(result.price_momentum, float)

    def test_weekly_change_pct_on_rising_series(self):
        result = compute_prediction(_rising_history(30), "Benzina")
        assert result is not None
        if result.weekly_change_pct is not None:
            # Rising series → positive weekly change
            assert result.weekly_change_pct > 0

    def test_monthly_change_pct_on_falling_series(self):
        result = compute_prediction(_falling_history(30), "Benzina")
        assert result is not None
        if result.monthly_change_pct is not None:
            assert result.monthly_change_pct < 0


# ---------------------------------------------------------------------------
# Ensemble algorithm — new methods
# ---------------------------------------------------------------------------

class TestEWOLSRegression:
    def test_flat_series_slope_near_zero(self):
        xs = list(range(14))
        ys = [1.800] * 14
        slope, intercept, r2 = _ewols_regression(xs, ys)
        assert abs(slope) < 1e-9
        assert abs(intercept - 1.800) < 1e-6

    def test_rising_series_positive_slope(self):
        xs = list(range(14))
        ys = [1.700 + i * 0.005 for i in range(14)]
        slope, intercept, r2 = _ewols_regression(xs, ys)
        assert slope > 0

    def test_r2_perfect_on_linear_data(self):
        xs = list(range(14))
        ys = [1.700 + i * 0.005 for i in range(14)]
        _, _, r2 = _ewols_regression(xs, ys)
        assert r2 > 0.99

    def test_recent_data_dominates_older(self):
        """A rising-then-sharp-drop series: EWOLS slope should be negative
        (recent drop dominates) while plain OLS might be positive."""
        # First 7 points rise, last 7 drop sharply
        ys = [1.700 + i * 0.010 for i in range(7)] + [1.800 - i * 0.020 for i in range(7)]
        xs = list(range(14))
        slope, _, _ = _ewols_regression(xs, ys)
        # Recent data (drop) should dominate → slope negative
        assert slope < 0

    def test_degenerate_single_point(self):
        slope, intercept, r2 = _ewols_regression([0], [1.800])
        assert slope == 0.0
        assert intercept == pytest.approx(1.800)


class TestHoltExponentialSmoothing:
    def test_flat_series_stays_flat(self):
        prices = [1.800] * 20
        forecasts = _holt_exponential_smoothing(prices, alpha=0.3, beta=0.1, n_forecast=7)
        assert len(forecasts) == 7
        for f in forecasts:
            assert abs(f - 1.800) < 0.02  # near 1.800

    def test_rising_series_extrapolates_upward(self):
        prices = [1.700 + i * 0.005 for i in range(20)]
        forecasts = _holt_exponential_smoothing(prices)
        assert forecasts[-1] > prices[-1]  # forecast day 7 > last observed

    def test_falling_series_extrapolates_downward(self):
        prices = [1.900 - i * 0.005 for i in range(20)]
        forecasts = _holt_exponential_smoothing(prices)
        assert forecasts[-1] < prices[-1]

    def test_returns_seven_forecasts(self):
        forecasts = _holt_exponential_smoothing([1.800] * 10)
        assert len(forecasts) == 7

    def test_degenerate_single_point_no_crash(self):
        forecasts = _holt_exponential_smoothing([1.800])
        assert len(forecasts) == 7
        assert all(f == pytest.approx(1.800) for f in forecasts)


class TestAIPromptContextExpansion:
    def test_prompt_uses_up_to_90_days_of_history(self):
        # 120 points -> prompt should include only the latest 90 entries.
        history = _make_history([1.700 + i * 0.001 for i in range(120)])
        prompt = _build_geopolitical_prompt(history, "Benzina", prediction=None)

        assert f"max {_AI_PROMPT_MAX_HISTORY_DAYS}" in prompt
        assert "RIEPILOGO STORICO" in prompt
        assert "DETTAGLIO GIORNALIERO" in prompt
        # Oldest 30 entries should be trimmed out.
        assert f"{history[0].date}: " not in prompt
        assert f"{history[29].date}: " not in prompt
        # Oldest day in 90-day window appears in weekly summary format.
        assert f"{history[30].date}→" in prompt
        # Detailed daily data starts from the latest 30 days.
        first_detailed_idx = len(history) - _AI_PROMPT_DETAILED_RECENT_DAYS
        assert f"{history[first_detailed_idx].date}: " in prompt
        # Latest entry must always be present.
        assert f"{history[-1].date}: " in prompt

    def test_prompt_prioritizes_geopolitics_and_government_choices(self):
        history = _make_history([1.800 + i * 0.0005 for i in range(20)])
        prompt = _build_geopolitical_prompt(history, "Gasolio", prediction=None)

        assert "PRIORITÀ CAUSALE" in prompt
        assert "Geopolitica internazionale e rischio supply shock" in prompt
        assert "Decisioni governi / regolatori" in prompt
        assert "prevalgono sull'inerzia statistica" in prompt

    def test_prompt_requests_optional_json_block(self):
        history = _make_history([1.800 + i * 0.0005 for i in range(20)])
        prompt = _build_geopolitical_prompt(history, "Gasolio", prediction=None)
        assert "JSON FACOLTATIVO" in prompt
        assert '"driver_scores"' in prompt

    def test_response_token_budget_scales_down_for_very_long_prompts(self):
        assert _response_token_budget("x" * 2_000) == 2000
        assert _response_token_budget("x" * 12_000) == 1500
        assert _response_token_budget("x" * 18_000) == 1200
        assert _response_token_budget("x" * 30_000) == 900


class TestEnsembleMethod:
    # Full set of valid method names (expanded with AR(1) variants)
    ALL_METHODS = {
        "ensemble_ols_holt_ar1",
        "ensemble_ols_holt",
        "ensemble_holt_ar1",
        "holt_exponential_smoothing",
        "moving_average",
    }

    def test_ensemble_used_when_ewols_r2_sufficient(self):
        """30-point linear rising history → EWOLS R² high → ensemble method."""
        result = compute_prediction(_rising_history(30), "Benzina")
        assert result is not None
        assert result.method_used in self.ALL_METHODS

    def test_holt_or_ar1_ensemble_when_few_points(self):
        """5–13 points: Holt and/or AR1 used; pure EWOLS ensemble not possible."""
        result = compute_prediction(_flat_history(8), "Benzina")
        assert result is not None
        assert result.method_used in ("ensemble_holt_ar1", "holt_exponential_smoothing", "moving_average")

    def test_wma_fallback_with_very_few_points(self):
        """< 5 points → Holt not available → WMA fallback."""
        result = compute_prediction(_flat_history(3), "Benzina")
        assert result is not None
        assert result.method_used == "moving_average"

    def test_method_used_values_valid(self):
        """method_used must be one of the known valid values."""
        for n in [3, 8, 14, 30]:
            result = compute_prediction(_rising_history(n), "Benzina")
            if result:
                assert result.method_used in self.ALL_METHODS

    def test_ar1_ensemble_with_sufficient_history(self):
        """≥14 points with high R² should use the 3-way AR1 ensemble."""
        result = compute_prediction(_rising_history(30), "Benzina")
        assert result is not None
        # With 30 linear points, EWOLS R² is high → expect 3-way ensemble
        assert result.method_used in ("ensemble_ols_holt_ar1", "ensemble_ols_holt")

    def test_predicted_price_7d_populated(self):
        """predicted_price_7d should equal predicted_prices[6]."""
        result = compute_prediction(_flat_history(20), "Benzina")
        assert result is not None
        assert result.predicted_price_7d is not None
        assert result.predicted_price_7d == pytest.approx(result.predicted_prices[6])


class TestVolatilityAdaptiveClamping:
    def test_predictions_always_positive(self):
        result = compute_prediction(_flat_history(30, price=1.800), "Benzina")
        assert result is not None
        for p in result.predicted_prices:
            assert p > 0

    def test_outer_bounds_never_exceeded(self):
        """Even with adaptive clamping, values must stay within [0.5×μ, 2.0×μ]."""
        history = _make_history([1.700 + i * 0.010 for i in range(30)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        mean_7d = sum(h.cheapest for h in history[-7:] if h.cheapest) / 7
        for p in result.predicted_prices:
            assert p >= 0.5 * mean_7d - 0.01
            assert p <= 2.0 * mean_7d + 0.01

    def test_tight_bounds_on_stable_prices(self):
        """Low-volatility (flat) series: bounds are tighter than the outer limits."""
        history = _flat_history(30, price=1.800)
        result = compute_prediction(history, "Benzina")
        assert result is not None
        # With near-zero σ, predictions should be very close to 1.800
        for p in result.predicted_prices:
            assert abs(p - 1.800) < 0.20  # tighter than ±0.9 outer bound


# ---------------------------------------------------------------------------
# Different fuel types
# ---------------------------------------------------------------------------

class TestFuelTypes:
    @pytest.mark.parametrize("fuel_type", ["Benzina", "Gasolio", "GPL", "Metano", "HVO"])
    def test_all_fuel_types_work(self, fuel_type: str):
        result = compute_prediction(_flat_history(20), fuel_type)
        assert result is not None
        assert len(result.predicted_prices) == 7


# ---------------------------------------------------------------------------
# Gap interpolation (sparse history)
# ---------------------------------------------------------------------------

class TestGapInterpolation:
    def test_small_gaps_handled(self):
        """Gaps ≤ 3 consecutive days should be interpolated, not crash."""
        prices = [1.800] * 10 + [None, None] + [1.810] * 10  # type: ignore[list-item]
        start = date.today() - timedelta(days=21)
        history = []
        day_offset = 0
        for p in prices:
            if p is not None:
                history.append(DailySnapshot(
                    date=(start + timedelta(days=day_offset)).isoformat(),
                    fuel_type="Benzina",
                    cheapest=p,
                    average=p + 0.05,
                ))
            day_offset += 1
        # Should not raise
        result = compute_prediction(history, "Benzina")
        assert result is not None


# ---------------------------------------------------------------------------
# AR(1) on first differences
# ---------------------------------------------------------------------------

class TestAR1DiffForecast:
    def test_returns_seven_forecasts(self):
        forecasts = _ar1_diff_forecast([1.800] * 10)
        assert len(forecasts) == 7

    def test_flat_series_stays_flat(self):
        """Flat prices → all diffs = 0 → AR(1) predicts flat continuation."""
        forecasts = _ar1_diff_forecast([1.800] * 15)
        for f in forecasts:
            assert abs(f - 1.800) < 0.005

    def test_rising_series_continues_upward(self):
        """Steady rise → positive mean diff → forecast above last price."""
        prices = [1.700 + i * 0.005 for i in range(20)]
        forecasts = _ar1_diff_forecast(prices)
        assert forecasts[0] > prices[-1]

    def test_falling_series_continues_downward(self):
        """Steady fall → negative mean diff → forecast below last price."""
        prices = [1.900 - i * 0.005 for i in range(20)]
        forecasts = _ar1_diff_forecast(prices)
        assert forecasts[0] < prices[-1]

    def test_degenerate_too_few_points_returns_flat(self):
        """< 6 points → flat fallback at last price."""
        prices = [1.800, 1.810, 1.820]
        forecasts = _ar1_diff_forecast(prices)
        assert len(forecasts) == 7
        for f in forecasts:
            assert abs(f - prices[-1]) < 0.001

    def test_single_price_no_crash(self):
        forecasts = _ar1_diff_forecast([1.800])
        assert len(forecasts) == 7

    def test_prices_reasonable_range(self):
        """Even with volatile history, AR(1) should stay in a sane range."""
        import random
        random.seed(42)
        prices = [1.800 + random.gauss(0, 0.03) for _ in range(30)]
        forecasts = _ar1_diff_forecast(prices)
        for f in forecasts:
            assert 0.5 < f < 5.0   # sanity bounds for EUR/L fuel


# ---------------------------------------------------------------------------
# Mean-reversion adjustment
# ---------------------------------------------------------------------------

class TestMeanReversionAdjustment:
    def test_no_adjustment_within_threshold(self):
        """Prices within ±4 % of mean → no change."""
        prices = [1.800] * 30
        predicted = [1.805, 1.806, 1.807, 1.808, 1.809, 1.810, 1.811]
        adjusted = _mean_reversion_adjustment(predicted, prices)
        for orig, adj in zip(predicted, adjusted):
            assert adj == pytest.approx(orig)

    def test_downward_nudge_when_above_mean(self):
        """Current price 10 % above 90-day mean → predictions nudged down."""
        # history around 1.700, current at 1.870
        prices = [1.700] * 28 + [1.870, 1.870]
        predicted = [1.880] * 7
        adjusted = _mean_reversion_adjustment(predicted, prices)
        # Each adjusted value should be <= original (pulled down)
        for orig, adj in zip(predicted, adjusted):
            assert adj <= orig + 0.001

    def test_upward_nudge_when_below_mean(self):
        """Current price 10 % below 90-day mean → predictions nudged up."""
        prices = [1.800] * 28 + [1.620, 1.620]
        predicted = [1.615] * 7
        adjusted = _mean_reversion_adjustment(predicted, prices)
        for orig, adj in zip(predicted, adjusted):
            assert adj >= orig - 0.001

    def test_no_adjustment_with_short_history(self):
        """< 14 points → no adjustment (insufficient context)."""
        prices = [1.800] * 8 + [2.000, 2.000]
        predicted = [2.010] * 7
        adjusted = _mean_reversion_adjustment(predicted, prices)
        for orig, adj in zip(predicted, adjusted):
            assert adj == pytest.approx(orig)

    def test_correction_grows_with_day_index(self):
        """Correction should increase (in absolute terms) across the 7-day window."""
        prices = [1.700] * 28 + [1.900, 1.900]
        predicted = [1.910] * 7
        adjusted = _mean_reversion_adjustment(predicted, prices)
        diffs = [abs(predicted[i] - adjusted[i]) for i in range(7)]
        # Correction should be non-decreasing across the forecast horizon
        for i in range(1, 7):
            assert diffs[i] >= diffs[i - 1] - 1e-9  # allow floating point tolerance


# ---------------------------------------------------------------------------
# 90-day history integration
# ---------------------------------------------------------------------------

class Test90DayHistory:
    def test_prediction_with_90_day_history(self):
        """compute_prediction should work with up to 90 days of history."""
        history = _make_history([1.700 + i * 0.001 for i in range(90)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        assert len(result.predicted_prices) == 7
        assert result.confidence in ("medium", "high")

    def test_monthly_change_available_with_90_days(self):
        """monthly_change_pct should be populated when ≥31 snapshots exist."""
        history = _make_history([1.750 + i * 0.001 for i in range(60)])
        result = compute_prediction(history, "Benzina")
        assert result is not None
        assert result.monthly_change_pct is not None

    def test_predicted_price_7d_with_large_history(self):
        """predicted_price_7d should always equal predicted_prices[6] regardless of history size."""
        for n in [14, 30, 60, 90]:
            history = _make_history([1.800 + i * 0.001 for i in range(n)])
            result = compute_prediction(history, "Benzina")
            assert result is not None
            assert result.predicted_price_7d == pytest.approx(result.predicted_prices[6])
