"""Abstract base entity for Carburanti MIMIT integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CarburantiMimitCoordinator


class CarburantiMimitEntity(CoordinatorEntity[CarburantiMimitCoordinator]):
    """Base entity that wires up the coordinator and sets shared device info."""

    _attr_has_entity_name = True
    _attr_attribution = "Dati da MIMIT – Ministero delle Imprese e del Made in Italy (IODL 2.0)"

    def __init__(
        self,
        coordinator: CarburantiMimitCoordinator,
        config_entry: ConfigEntry,
        fuel_type: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._fuel_type = fuel_type

        # All sensors for the same config entry share one HA device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"Carburanti – {config_entry.title}",
            manufacturer="MIMIT",
            model="Osservatorio Prezzi Carburanti",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.mimit.gov.it/it/mercato-e-consumatori/prezzi/mercati-dei-carburanti/osservatorio-carburanti",
        )
