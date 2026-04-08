"""Shared test fixtures and HA module stubs for carburanti_mimit tests.

HA module stubs are injected into sys.modules BEFORE any custom_components
imports so that the integration's source files can be imported without a
full Home Assistant installation.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Minimal Home Assistant stubs
# Must be set up before any `from custom_components.*` import
# ---------------------------------------------------------------------------

class _CoordinatorEntity:
    """Stub for homeassistant.helpers.entity.CoordinatorEntity."""

    def __init__(self, coordinator: object, *args, **kwargs) -> None:
        self.coordinator = coordinator
        self.hass = MagicMock()

    def _handle_coordinator_update(self) -> None:  # noqa: D102
        self.async_write_ha_state()

    def async_write_ha_state(self) -> None:  # noqa: D102
        pass

    async def async_added_to_hass(self) -> None:  # noqa: D102
        pass

    def __class_getitem__(cls, item):
        return cls


class _DataUpdateCoordinator:
    """Stub for homeassistant.helpers.update_coordinator.DataUpdateCoordinator."""

    def __init__(self, *args, **kwargs) -> None:
        self.data = None

    def __class_getitem__(cls, item):
        return cls


class _SensorEntity:
    """Stub for homeassistant.components.sensor.SensorEntity."""
    _attr_unique_id: str | None = None
    _attr_translation_key: str | None = None
    _attr_translation_placeholders: dict = {}
    _attr_native_unit_of_measurement: str | None = None
    _attr_state_class = None
    _attr_device_class = None
    _attr_suggested_display_precision: int | None = None


class _RestoreEntity:
    """Stub for homeassistant.helpers.restore_state.RestoreEntity."""

    async def async_get_last_state(self):  # noqa: D102
        return None

    async def async_added_to_hass(self) -> None:  # noqa: D102
        pass


class _SensorStateClass:
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


class _UpdateFailed(Exception):
    pass


class _ConfigEntry:
    pass


class _HomeAssistant:
    pass


def _make_module(name: str) -> types.ModuleType:
    """Create a ModuleType with __path__ so Python treats it as a package.

    Unknown attribute access falls back to MagicMock() so import statements
    like `from ha_module import SomeClass` succeed even for names we haven't
    explicitly stubbed.
    """
    mod = types.ModuleType(name)
    mod.__path__ = []          # marks it as a package
    mod.__package__ = name
    mod.__spec__ = MagicMock()
    # Fallback: any attribute not explicitly set returns a MagicMock
    # Fallback: any attribute not explicitly set returns a fresh MagicMock.
    # We return the class (not an instance) as a safer default when the
    # attribute might be used as a base class or callable.
    def _fallback(attr: str) -> MagicMock:  # type: ignore[return]
        return MagicMock()
    mod.__getattr__ = _fallback  # type: ignore[assignment]
    return mod


def _make_ha_modules() -> None:
    """Insert minimal HA stubs into sys.modules."""
    def _pkg(name: str, **attrs) -> types.ModuleType:
        mod = _make_module(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        return mod

    class _Store:
        def __init__(self, *a, **kw):
            pass
        async def async_load(self):
            return None
        async def async_save(self, data):
            pass

    stubs: dict[str, types.ModuleType] = {
        "homeassistant":
            _pkg("homeassistant"),
        "homeassistant.core":
            _pkg("homeassistant.core",
                 HomeAssistant=_HomeAssistant,
                 callback=lambda f: f),
        "homeassistant.config_entries":
            _pkg("homeassistant.config_entries",
                 ConfigEntry=_ConfigEntry,
                 OptionsFlow=object,
                 ConfigFlow=object,
                 ConfigFlowResult=dict),
        "homeassistant.helpers":
            _pkg("homeassistant.helpers"),
        "homeassistant.helpers.update_coordinator":
            _pkg("homeassistant.helpers.update_coordinator",
                 DataUpdateCoordinator=_DataUpdateCoordinator,
                 CoordinatorEntity=_CoordinatorEntity,
                 UpdateFailed=_UpdateFailed),
        "homeassistant.helpers.entity":
            _pkg("homeassistant.helpers.entity",
                 CoordinatorEntity=_CoordinatorEntity),
        "homeassistant.helpers.entity_platform":
            _pkg("homeassistant.helpers.entity_platform"),
        "homeassistant.helpers.restore_state":
            _pkg("homeassistant.helpers.restore_state",
                 RestoreEntity=_RestoreEntity),
        "homeassistant.helpers.aiohttp_client":
            _pkg("homeassistant.helpers.aiohttp_client"),
        "homeassistant.helpers.event":
            _pkg("homeassistant.helpers.event"),
        "homeassistant.helpers.storage":
            _pkg("homeassistant.helpers.storage", Store=_Store),
        "homeassistant.helpers.selector":
            _pkg("homeassistant.helpers.selector"),
        "homeassistant.helpers.device_registry":
            _pkg("homeassistant.helpers.device_registry",
                 DeviceEntryType=MagicMock(),
                 DeviceInfo=dict),
        "homeassistant.components":
            _pkg("homeassistant.components"),
        "homeassistant.components.sensor":
            _pkg("homeassistant.components.sensor",
                 SensorEntity=_SensorEntity,
                 SensorStateClass=_SensorStateClass),
        "homeassistant.components.recorder":
            _pkg("homeassistant.components.recorder",
                 get_instance=MagicMock()),
        "homeassistant.components.recorder.models":
            _pkg("homeassistant.components.recorder.models",
                 StatisticData=MagicMock,
                 StatisticMetaData=MagicMock),
        "homeassistant.components.recorder.statistics":
            _pkg("homeassistant.components.recorder.statistics",
                 async_add_external_statistics=AsyncMock()),
        "homeassistant.util":
            _pkg("homeassistant.util"),
        "homeassistant.util.dt":
            _pkg("homeassistant.util.dt"),
        "aiohttp":
            _pkg("aiohttp"),
        "voluptuous":
            _pkg("voluptuous"),
    }
    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)


# Install stubs before any custom_components import
_make_ha_modules()


# ---------------------------------------------------------------------------
# Deferred imports (after stubs are in place)
# ---------------------------------------------------------------------------

from custom_components.carburanti_mimit.coordinator import CoordinatorData, FuelAreaData  # noqa: E402
from custom_components.carburanti_mimit.parser import EnrichedStation, Station  # noqa: E402


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_station(
    station_id: int = 1,
    nome: str = "Test Station",
    indirizzo: str = "Via Roma 1",
    comune: str = "Milano",
    provincia: str = "MI",
    bandiera: str = "ENI",
    lat: float = 45.4654,
    lon: float = 9.1859,
) -> Station:
    """Return a minimal Station for tests."""
    return Station(
        id=station_id,
        gestore="Test Gestore",
        bandiera=bandiera,
        tipo="Stradale",
        nome=nome,
        indirizzo=indirizzo,
        comune=comune,
        provincia=provincia,
        lat=lat,
        lon=lon,
    )


def make_enriched(
    price: float = 1.800,
    fuel_type: str = "Benzina",
    is_self: bool = True,
    distance_km: float = 1.5,
    station: Station | None = None,
    reported_at: datetime | None = None,
) -> EnrichedStation:
    """Return a minimal EnrichedStation for tests."""
    if station is None:
        station = make_station()
    if reported_at is None:
        reported_at = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
    return EnrichedStation(
        station=station,
        fuel_type=fuel_type,
        price=price,
        is_self=is_self,
        reported_at=reported_at,
        distance_km=distance_km,
    )


# ---------------------------------------------------------------------------
# Coordinator / entry mocks
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_coordinator():
    """Return a lightweight mock coordinator."""
    coord = MagicMock()
    coord.data = None
    coord.ai_cache = {}
    return coord


@pytest.fixture
def mock_config_entry():
    """Return a minimal mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {
        "fuel_types": ["Benzina", "Gasolio"],
        "top_n": 3,
        "favorite_stations": [],
        "radius_km": 10,
        "ai_provider": "none",
        "ai_api_key": "",
    }
    return entry
