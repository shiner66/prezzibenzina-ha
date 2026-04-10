"""Local JSON history storage for Carburanti MIMIT integration."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import HISTORY_DAYS, STORAGE_KEY, STORAGE_VERSION

if TYPE_CHECKING:
    from .coordinator import CoordinatorData

_LOGGER = logging.getLogger(__name__)


@dataclass
class DailySnapshot:
    """Daily price snapshot for one fuel type."""

    date: str           # ISO "YYYY-MM-DD"
    fuel_type: str
    cheapest: float | None
    average: float | None
    national_average: float | None = None  # national average price (from MIMIT regional CSV)


class HistoryStorage:
    """Persists a rolling 90-day window of daily price snapshots.

    Stored in HA's `.storage/` directory via the official Store helper,
    so it survives restarts and is included in HA backups.

    Data structure in JSON:
    {
      "<fuel_type>": [
        {"date": "2026-01-01", "fuel_type": "Benzina", "cheapest": 1.789, "average": 1.842},
        ...
      ]
    }
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}_{entry_id}",
        )
        # keyed by fuel_type → list of DailySnapshot (chronological, oldest first)
        self._data: dict[str, list[DailySnapshot]] = {}

    async def async_load(self) -> None:
        """Load persisted data from disk. Call once at integration startup."""
        raw: dict[str, Any] | None = await self._store.async_load()
        if raw is None:
            _LOGGER.debug("No persisted history found — starting fresh")
            return
        try:
            for fuel_type, snapshots in raw.items():
                self._data[fuel_type] = [
                    DailySnapshot(
                        date=s["date"],
                        fuel_type=s["fuel_type"],
                        cheapest=s.get("cheapest"),
                        average=s.get("average"),
                        national_average=s.get("national_average"),
                    )
                    for s in snapshots
                    if isinstance(s, dict)
                ]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Failed to restore history (%s) — starting fresh", exc)
            self._data = {}

    async def async_record_snapshot(self, coordinator_data: CoordinatorData) -> None:
        """Upsert today's price snapshot for each fuel type and prune old data."""
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()

        for fuel_type, area in coordinator_data.by_fuel.items():
            snapshot = DailySnapshot(
                date=today,
                fuel_type=fuel_type,
                cheapest=area.cheapest_price,
                average=area.average_price,
                national_average=area.national_average,
            )
            if fuel_type not in self._data:
                self._data[fuel_type] = []
            snapshots = self._data[fuel_type]

            # Upsert: replace today's entry if already present
            replaced = False
            for i, s in enumerate(snapshots):
                if s.date == today:
                    snapshots[i] = snapshot
                    replaced = True
                    break
            if not replaced:
                snapshots.append(snapshot)

            # Prune entries older than HISTORY_DAYS
            self._data[fuel_type] = [s for s in snapshots if s.date >= cutoff]

        await self._async_save()

    def get_history(self, fuel_type: str, days: int = 30) -> list[DailySnapshot]:
        """Return the last *days* snapshots for *fuel_type* (oldest → newest).

        Returns an empty list if no data is available.
        """
        snapshots = self._data.get(fuel_type, [])
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return [s for s in snapshots if s.date >= cutoff]

    def get_all_history(self, fuel_type: str) -> list[DailySnapshot]:
        """Return all stored snapshots for *fuel_type* (oldest → newest)."""
        return list(self._data.get(fuel_type, []))

    async def async_clear(self, fuel_type: str | None = None) -> None:
        """Clear stored history.

        If *fuel_type* is given, only that type's history is cleared.
        Otherwise all history is cleared.
        """
        if fuel_type is not None:
            self._data.pop(fuel_type, None)
        else:
            self._data.clear()
        await self._async_save()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _async_save(self) -> None:
        """Flush in-memory data to disk."""
        serializable = {
            fuel_type: [asdict(s) for s in snapshots]
            for fuel_type, snapshots in self._data.items()
        }
        await self._store.async_save(serializable)
