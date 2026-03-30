"""Constants for the Carburanti MIMIT integration."""
from __future__ import annotations

DOMAIN = "carburanti_mimit"
PLATFORMS = ["sensor"]

# ---------------------------------------------------------------------------
# MIMIT data URLs
# ---------------------------------------------------------------------------
URL_PRICES = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
URL_REGISTRY = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
URL_API_BASE = "https://carburanti.mise.gov.it"
URL_API_POSITION = f"{URL_API_BASE}/ospzApi/ricerca/position"

# ---------------------------------------------------------------------------
# Config / options keys
# ---------------------------------------------------------------------------
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_RADIUS_KM = "radius_km"
CONF_FUEL_TYPES = "fuel_types"
CONF_TOP_N = "top_n"
CONF_UPDATE_INTERVAL_H = "update_interval_h"
CONF_INCLUDE_SELF = "include_self"
CONF_INCLUDE_SERVITO = "include_servito"
CONF_AI_PROVIDER = "ai_provider"
CONF_AI_API_KEY = "ai_api_key"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_RADIUS_KM = 10
DEFAULT_TOP_N = 5
DEFAULT_UPDATE_INTERVAL_H = 24
DEFAULT_FUEL_TYPES = ["Benzina", "Gasolio"]
DEFAULT_INCLUDE_SELF = True
DEFAULT_INCLUDE_SERVITO = True

# ---------------------------------------------------------------------------
# Fuel types (exactly as they appear in descCarburante column of MIMIT CSV)
# ---------------------------------------------------------------------------
FUEL_TYPE_BENZINA = "Benzina"
FUEL_TYPE_GASOLIO = "Gasolio"
FUEL_TYPE_GPL = "GPL"
FUEL_TYPE_METANO = "Metano"
FUEL_TYPE_HVO = "HVO"
FUEL_TYPE_GASOLIO_RISCALDAMENTO = "Gasolio Riscaldamento"

ALL_FUEL_TYPES: list[str] = [
    FUEL_TYPE_BENZINA,
    FUEL_TYPE_GASOLIO,
    FUEL_TYPE_GPL,
    FUEL_TYPE_METANO,
    FUEL_TYPE_HVO,
    FUEL_TYPE_GASOLIO_RISCALDAMENTO,
]

FUEL_UNITS: dict[str, str] = {
    FUEL_TYPE_BENZINA: "EUR/L",
    FUEL_TYPE_GASOLIO: "EUR/L",
    FUEL_TYPE_GPL: "EUR/L",
    FUEL_TYPE_METANO: "EUR/kg",
    FUEL_TYPE_HVO: "EUR/L",
    FUEL_TYPE_GASOLIO_RISCALDAMENTO: "EUR/L",
}

FUEL_ICONS: dict[str, str] = {
    FUEL_TYPE_BENZINA: "mdi:gas-station",
    FUEL_TYPE_GASOLIO: "mdi:gas-station",
    FUEL_TYPE_GPL: "mdi:propane-tank",
    FUEL_TYPE_METANO: "mdi:molecule",
    FUEL_TYPE_HVO: "mdi:leaf",
    FUEL_TYPE_GASOLIO_RISCALDAMENTO: "mdi:home-thermometer",
}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
STORAGE_VERSION = 1
STORAGE_KEY = "carburanti_mimit_history"
HISTORY_DAYS = 90

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
STATISTICS_SOURCE = DOMAIN

# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------
AI_PROVIDER_NONE = "none"
AI_PROVIDER_CLAUDE = "claude"
AI_PROVIDER_OPENAI = "openai"

AI_PROVIDERS = [AI_PROVIDER_NONE, AI_PROVIDER_CLAUDE, AI_PROVIDER_OPENAI]

# ---------------------------------------------------------------------------
# Sensor suffix tokens
# ---------------------------------------------------------------------------
SENSOR_CHEAPEST = "cheapest"
SENSOR_AVERAGE = "average"
SENSOR_TREND = "trend"
SENSOR_PREDICTION = "prediction"

# ---------------------------------------------------------------------------
# MIMIT update time (Europe/Rome)
# ---------------------------------------------------------------------------
MIMIT_UPDATE_HOUR = 8
MIMIT_UPDATE_MINUTE = 15  # 15 min after MIMIT publishes at 08:00

# ---------------------------------------------------------------------------
# Registry cache TTL
# ---------------------------------------------------------------------------
REGISTRY_CACHE_DAYS = 7

# ---------------------------------------------------------------------------
# HTTP timeouts (seconds)
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_VALIDATION = 10
