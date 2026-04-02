"""Constants for the Carburanti MIMIT integration."""
from __future__ import annotations

DOMAIN = "carburanti_mimit"
PLATFORMS = ["sensor"]

# ---------------------------------------------------------------------------
# MIMIT data URLs
# ---------------------------------------------------------------------------
URL_PRICES = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
URL_REGISTRY = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
URL_REGIONAL_AVERAGES = "https://www.mimit.gov.it/images/stories/carburanti/MediaRegionaleStradale.csv"

# ---------------------------------------------------------------------------
# PrezzibenzinaIT — web scraping (nessuna API, prezzi pubblici)
# ---------------------------------------------------------------------------
URL_PB_HOMEPAGE = "https://www.prezzibenzina.it/"
URL_PB_STATION = "https://www.prezzibenzina.it/distributori/{station_id}"
URL_PB_SEARCH_HANDLER = (
    "https://www.prezzibenzina.it/www2/develop/tech/handlers/search_handler.php"
)

PB_SCRAPE_DELAY_S = 1.0       # pausa tra richieste consecutive (cortesia)
PB_SCRAPE_TIMEOUT_S = 10      # timeout per singola pagina stazione
PB_MAX_STATIONS = 10          # massimo stazioni da scrapare per ciclo
PB_DISCOVERY_MATCH_KM = 0.3   # soglia GPS per abbinare stazione PB → MIMIT

# Mapping: testo "service" HTML → is_self / is_user_reported
COMMUNITY_SERVICE_SELF: frozenset[str] = frozenset({"Self service", "Self ril. utente"})
COMMUNITY_SERVICE_USER_RPT: frozenset[str] = frozenset({"Serv. ril. utente", "Self ril. utente"})

# Mapping: label carburante prezzibenzina.it → descCarburante MIMIT
# None = carburante non gestito dall'integrazione (es. AdBlue)
FUEL_MAP_PB_TO_MIMIT: dict[str, str | None] = {
    "Benzina":          "Benzina",
    "Diesel":           "Gasolio",
    "Gasolio":          "Gasolio",
    "Diesel speciale":  "Gasolio",
    "Benzina speciale": "Benzina",
    "GPL":              "GPL",
    "Metano":           "Metano",
    "GNL":              "Metano",
    "HVO":              "HVO",
    "AdBlue":           None,
}

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
CONF_USE_COMMUNITY_PRICES = "use_community_prices"
CONF_UPDATE_INTERVAL_COMMUNITY_MIN = "update_interval_community_min"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_RADIUS_KM = 10
DEFAULT_TOP_N = 5
DEFAULT_UPDATE_INTERVAL_H = 24
DEFAULT_FUEL_TYPES = ["Benzina", "Gasolio"]
DEFAULT_INCLUDE_SELF = True
DEFAULT_INCLUDE_SERVITO = True
DEFAULT_USE_COMMUNITY_PRICES = True
DEFAULT_UPDATE_INTERVAL_COMMUNITY_MIN = 30

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
SENSOR_PREDICTION_3D = "prediction_3d"
SENSOR_AI_INSIGHT = "ai_insight"

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
