"""Inject fuel price data into Home Assistant long-term statistics."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN, FUEL_UNITS, SENSOR_AVERAGE, SENSOR_CHEAPEST, STATISTICS_SOURCE

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.recorder.models import StatisticMeanType

_MEAN_TYPE = StatisticMeanType.ARITHMETIC


def _fuel_slug(fuel_type: str) -> str:
    """Convert a fuel type string to a safe identifier slug."""
    return fuel_type.lower().replace(" ", "_")


def build_statistic_id(entry_id: str, fuel_type: str, metric: str) -> str:
    """Build the external statistic_id for a given fuel type and metric.

    Format: ``carburanti_mimit:<entry8>_<fuel_slug>_<metric>``
    e.g.  ``carburanti_mimit:ab12cd34_benzina_cheapest``
    """
    entry_prefix = entry_id[:8].replace("-", "").lower()
    slug = _fuel_slug(fuel_type)
    return f"{STATISTICS_SOURCE}:{entry_prefix}_{slug}_{metric}"


def _build_metadata(
    statistic_id: str,
    name: str,
    fuel_type: str,
) -> StatisticMetaData:
    """Build a StatisticMetaData object."""
    return StatisticMetaData(
        has_sum=False,
        mean_type=_MEAN_TYPE,
        name=name,
        source=STATISTICS_SOURCE,
        statistic_id=statistic_id,
        unit_class=None,
        unit_of_measurement=FUEL_UNITS.get(fuel_type, "EUR/L"),
    )


async def async_push_price_statistics(
    hass: HomeAssistant,
    entry_id: str,
    fuel_type: str,
    cheapest_price: float | None,
    average_price: float | None,
    timestamp: datetime,
) -> None:
    """Push one StatisticData point per metric into the HA recorder.

    Uses ``async_add_external_statistics`` which upserts idempotently —
    safe to call on each coordinator update.

    The ``start`` field is truncated to the current hour so that repeated
    calls within the same hour do not create duplicate rows.
    """
    if cheapest_price is None and average_price is None:
        return

    # Truncate to hour boundary (recorder requirement)
    start = timestamp.replace(minute=0, second=0, microsecond=0)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    recorder = get_instance(hass)

    if cheapest_price is not None:
        stat_id = build_statistic_id(entry_id, fuel_type, SENSOR_CHEAPEST)
        metadata = _build_metadata(
            stat_id,
            f"Carburanti {fuel_type} – Minimo",
            fuel_type,
        )
        stat = StatisticData(start=start, mean=cheapest_price)
        try:
            async_add_external_statistics(hass, metadata, [stat])
            _LOGGER.debug(
                "Pushed statistic %s = %.4f @ %s", stat_id, cheapest_price, start
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to push cheapest statistic for %s (id=%s): %s",
                fuel_type, stat_id, exc,
            )

    if average_price is not None:
        stat_id = build_statistic_id(entry_id, fuel_type, SENSOR_AVERAGE)
        metadata = _build_metadata(
            stat_id,
            f"Carburanti {fuel_type} – Media",
            fuel_type,
        )
        stat = StatisticData(start=start, mean=average_price)
        try:
            async_add_external_statistics(hass, metadata, [stat])
            _LOGGER.debug(
                "Pushed statistic %s = %.4f @ %s", stat_id, average_price, start
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to push average statistic for %s (id=%s): %s",
                fuel_type, stat_id, exc,
            )
