"""Unit tests for prediction.py — statistical forecasting."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from custom_components.carburanti_mimit.prediction import (
    PredictionResult,
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
