"""Config flow for Carburanti MIMIT integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MimitApiClient
from .const import (
    AI_PROVIDER_NONE,
    AI_PROVIDERS,
    ALL_FUEL_TYPES,
    CONF_AI_API_KEY,
    CONF_AI_PROVIDER,
    CONF_FAVORITE_STATION_IDS,
    CONF_FUEL_TYPES,
    CONF_INCLUDE_SELF,
    CONF_INCLUDE_SERVITO,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_RADIUS_KM,
    CONF_TOP_N,
    CONF_UPDATE_INTERVAL_COMMUNITY_MIN,
    CONF_UPDATE_INTERVAL_H,
    CONF_USE_COMMUNITY_PRICES,
    DEFAULT_FAVORITE_STATION_IDS,
    DEFAULT_FUEL_TYPES,
    DEFAULT_INCLUDE_SELF,
    DEFAULT_INCLUDE_SERVITO,
    DEFAULT_RADIUS_KM,
    DEFAULT_TOP_N,
    DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN,
    DEFAULT_UPDATE_INTERVAL_H,
    DEFAULT_USE_COMMUNITY_PRICES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Human-readable labels for AI providers
_AI_PROVIDER_LABELS = {
    "none": "Nessuno (solo statistica)",
    "claude": "Claude (Anthropic)",
    "openai": "OpenAI (ChatGPT)",
}


class CarburantiMimitConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1 — Location + radius
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            lat = user_input[CONF_LATITUDE]
            lon = user_input[CONF_LONGITUDE]

            # Validate connectivity
            session = async_get_clientsession(self.hass)
            client = MimitApiClient(session)
            ok = await client.async_validate_connectivity()
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                # Prevent duplicate entries for the same location
                unique_id = f"{lat:.3f}_{lon:.3f}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                self._data[CONF_LATITUDE] = lat
                self._data[CONF_LONGITUDE] = lon
                self._options[CONF_RADIUS_KM] = user_input[CONF_RADIUS_KM]
                self._options["entry_title"] = user_input.get("entry_title", "Casa")
                return await self.async_step_fuel_types()

        default_lat = self.hass.config.latitude
        default_lon = self.hass.config.longitude

        schema = vol.Schema(
            {
                vol.Required(CONF_LATITUDE, default=default_lat): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-90, max=90, step="any", mode="box")
                ),
                vol.Required(CONF_LONGITUDE, default=default_lon): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=-180, max=180, step="any", mode="box")
                ),
                vol.Required(CONF_RADIUS_KM, default=DEFAULT_RADIUS_KM): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=100, step=1, mode="slider", unit_of_measurement="km")
                ),
                vol.Optional("entry_title", default="Casa"): selector.TextSelector(),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — Fuel types
    # ------------------------------------------------------------------

    async def async_step_fuel_types(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._options[CONF_FUEL_TYPES] = user_input[CONF_FUEL_TYPES]
            self._options[CONF_INCLUDE_SELF] = user_input.get(CONF_INCLUDE_SELF, DEFAULT_INCLUDE_SELF)
            self._options[CONF_INCLUDE_SERVITO] = user_input.get(CONF_INCLUDE_SERVITO, DEFAULT_INCLUDE_SERVITO)
            return await self.async_step_advanced()

        schema = vol.Schema(
            {
                vol.Required(CONF_FUEL_TYPES, default=DEFAULT_FUEL_TYPES): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": ft, "label": ft} for ft in ALL_FUEL_TYPES],
                        multiple=True,
                    )
                ),
                vol.Required(CONF_INCLUDE_SELF, default=DEFAULT_INCLUDE_SELF): selector.BooleanSelector(),
                vol.Required(CONF_INCLUDE_SERVITO, default=DEFAULT_INCLUDE_SERVITO): selector.BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="fuel_types",
            data_schema=schema,
        )

    # ------------------------------------------------------------------
    # Step 3 — Advanced
    # ------------------------------------------------------------------

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            self._options[CONF_TOP_N] = int(user_input[CONF_TOP_N])
            self._options[CONF_UPDATE_INTERVAL_H] = int(user_input[CONF_UPDATE_INTERVAL_H])
            self._options[CONF_AI_PROVIDER] = user_input.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE)
            self._options[CONF_AI_API_KEY] = user_input.get(CONF_AI_API_KEY, "")
            self._options[CONF_USE_COMMUNITY_PRICES] = user_input.get(
                CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
            )
            self._options[CONF_UPDATE_INTERVAL_COMMUNITY_MIN] = int(
                user_input.get(CONF_UPDATE_INTERVAL_COMMUNITY_MIN, DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN)
            )
            # Favorite stations are configured post-setup via Options (stations step)
            self._options.setdefault(CONF_FAVORITE_STATION_IDS, DEFAULT_FAVORITE_STATION_IDS)

            title = self._options.pop("entry_title", "Carburanti")
            return self.async_create_entry(
                title=title,
                data=self._data,
                options=self._options,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_TOP_N, default=DEFAULT_TOP_N): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=20, step=1, mode="slider")
                ),
                vol.Required(CONF_UPDATE_INTERVAL_H, default=DEFAULT_UPDATE_INTERVAL_H): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=24, step=1, mode="slider", unit_of_measurement="h")
                ),
                vol.Required(
                    CONF_USE_COMMUNITY_PRICES, default=DEFAULT_USE_COMMUNITY_PRICES
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_UPDATE_INTERVAL_COMMUNITY_MIN,
                    default=DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=60, step=5, mode="slider", unit_of_measurement="min")
                ),
                vol.Optional(CONF_AI_PROVIDER, default=AI_PROVIDER_NONE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": p, "label": _AI_PROVIDER_LABELS.get(p, p)}
                            for p in AI_PROVIDERS
                        ],
                        multiple=False,
                    )
                ),
                vol.Optional(CONF_AI_API_KEY, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=schema,
        )

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CarburantiMimitOptionsFlow:
        return CarburantiMimitOptionsFlow(config_entry)


class CarburantiMimitOptionsFlow(config_entries.OptionsFlow):
    """Menu-based options flow: general settings | favourite station picker."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    # ------------------------------------------------------------------
    # Root menu
    # ------------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "stations"],
        )

    # ------------------------------------------------------------------
    # Menu option A — general settings
    # ------------------------------------------------------------------

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        opts = self._config_entry.options

        if user_input is not None:
            new_options = {
                **opts,  # preserve any keys not in this form (e.g. favorite_station_ids)
                CONF_RADIUS_KM: int(user_input[CONF_RADIUS_KM]),
                CONF_FUEL_TYPES: user_input[CONF_FUEL_TYPES],
                CONF_INCLUDE_SELF: user_input[CONF_INCLUDE_SELF],
                CONF_INCLUDE_SERVITO: user_input[CONF_INCLUDE_SERVITO],
                CONF_TOP_N: int(user_input[CONF_TOP_N]),
                CONF_UPDATE_INTERVAL_H: int(user_input[CONF_UPDATE_INTERVAL_H]),
                CONF_USE_COMMUNITY_PRICES: user_input.get(
                    CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES
                ),
                CONF_UPDATE_INTERVAL_COMMUNITY_MIN: int(
                    user_input.get(CONF_UPDATE_INTERVAL_COMMUNITY_MIN, DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN)
                ),
                CONF_AI_PROVIDER: user_input.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE),
                CONF_AI_API_KEY: user_input.get(CONF_AI_API_KEY, ""),
            }
            new_options.setdefault(CONF_FAVORITE_STATION_IDS, DEFAULT_FAVORITE_STATION_IDS)
            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_RADIUS_KM,
                    default=opts.get(CONF_RADIUS_KM, DEFAULT_RADIUS_KM),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=100, step=1, mode="slider", unit_of_measurement="km")
                ),
                vol.Required(
                    CONF_FUEL_TYPES,
                    default=opts.get(CONF_FUEL_TYPES, DEFAULT_FUEL_TYPES),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[{"value": ft, "label": ft} for ft in ALL_FUEL_TYPES],
                        multiple=True,
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_SELF,
                    default=opts.get(CONF_INCLUDE_SELF, DEFAULT_INCLUDE_SELF),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_INCLUDE_SERVITO,
                    default=opts.get(CONF_INCLUDE_SERVITO, DEFAULT_INCLUDE_SERVITO),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_TOP_N,
                    default=opts.get(CONF_TOP_N, DEFAULT_TOP_N),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=20, step=1, mode="slider")
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL_H,
                    default=opts.get(CONF_UPDATE_INTERVAL_H, DEFAULT_UPDATE_INTERVAL_H),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=24, step=1, mode="slider", unit_of_measurement="h")
                ),
                vol.Required(
                    CONF_USE_COMMUNITY_PRICES,
                    default=opts.get(CONF_USE_COMMUNITY_PRICES, DEFAULT_USE_COMMUNITY_PRICES),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_UPDATE_INTERVAL_COMMUNITY_MIN,
                    default=opts.get(CONF_UPDATE_INTERVAL_COMMUNITY_MIN, DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=60, step=5, mode="slider", unit_of_measurement="min")
                ),
                vol.Optional(
                    CONF_AI_PROVIDER,
                    default=opts.get(CONF_AI_PROVIDER, AI_PROVIDER_NONE),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": p, "label": _AI_PROVIDER_LABELS.get(p, p)}
                            for p in AI_PROVIDERS
                        ],
                        multiple=False,
                    )
                ),
                vol.Optional(
                    CONF_AI_API_KEY,
                    default=opts.get(CONF_AI_API_KEY, ""),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )

        return self.async_show_form(step_id="general", data_schema=schema)

    # ------------------------------------------------------------------
    # Menu option B — favourite station picker
    # ------------------------------------------------------------------

    async def async_step_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user pick which stations within the radius to pin as sensors."""
        opts = self._config_entry.options

        if user_input is not None:
            raw_ids = user_input.get(CONF_FAVORITE_STATION_IDS, [])
            new_options = {
                **opts,
                CONF_FAVORITE_STATION_IDS: [int(x) for x in raw_ids],
            }
            return self.async_create_entry(title="", data=new_options)

        # Build station list from coordinator's last snapshot
        station_options = self._build_station_options()
        current_ids = [
            str(i)
            for i in opts.get(CONF_FAVORITE_STATION_IDS, DEFAULT_FAVORITE_STATION_IDS)
        ]

        # Keep previously selected IDs even if they've temporarily left the radius.
        known_ids = {opt["value"] for opt in station_options}
        extra: list[dict[str, str]] = [
            {"value": sid, "label": f"Stazione ID {sid} (fuori raggio)"}
            for sid in current_ids
            if sid not in known_ids
        ]
        all_options = station_options + extra

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FAVORITE_STATION_IDS,
                    default=current_ids,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=all_options,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        no_data = not station_options
        return self.async_show_form(
            step_id="stations",
            data_schema=schema,
            description_placeholders={
                "station_count": str(len(station_options)),
                "no_data_hint": (
                    " ⚠️ Nessuna stazione disponibile: esegui un aggiornamento manuale e riapri le opzioni."
                    if no_data
                    else ""
                ),
            },
        )

    def _build_station_options(self) -> list[dict[str, str]]:
        """Build SelectSelector options from coordinator's stations snapshot."""
        try:
            coordinator = self._config_entry.runtime_data.coordinator
            stations = coordinator.data.stations_in_radius if coordinator.data else []
        except AttributeError:
            stations = []

        return [
            {
                "value": str(s["id"]),
                "label": (
                    f"{s['bandiera']} | {s['name']}"
                    f" – {s['comune']} ({s['distance_km']:.1f} km)"
                ),
            }
            for s in stations
        ]
