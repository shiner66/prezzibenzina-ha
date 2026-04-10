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

# MIMIT OsservaPrezzi real-time API (carburanti.mise.gov.it/ospzApi)
# Stations are legally required to update within 6 h of a price change;
# this endpoint reflects those updates immediately (intraday).
URL_OSPZ_SEARCH_ZONE = "https://carburanti.mise.gov.it/ospzApi/search/zone"

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

# Mapping: ospzApi fuelId → integration fuel type name (None = skip)
# Only standard fuel IDs are mapped; premium/brand variants (Blue Diesel,
# Supreme Diesel, etc.) are intentionally excluded to avoid overwriting
# the standard Gasolio/Benzina entry with a brand-specific variant price.
FUEL_ID_TO_MIMIT: dict[int, str | None] = {
    1: "Benzina",
    2: "Gasolio",
    3: "Metano",
    4: "GPL",
    394: "HVO",   # HVOlution (ENI HVO diesel)
    424: "HVO",   # HVO (generic)
}

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
CONF_FAVORITE_STATION_IDS = "favorite_station_ids"  # legacy key (pre-0.x) — kept for migration
CONF_FAVORITE_STATIONS = "favorite_stations"

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
DEFAULT_FAVORITE_STATION_IDS: list[int] = []  # legacy
DEFAULT_FAVORITE_STATIONS: list[str] = []

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
# AI model selection
# ---------------------------------------------------------------------------
CONF_AI_MODEL = "ai_model"

# Default models (good quality / budget balance)
DEFAULT_AI_MODEL_OPENAI = "gpt-4.1-mini"
DEFAULT_AI_MODEL_CLAUDE = "claude-haiku-4-5-20251001"

# Available OpenAI models (versioned IDs as of 2025-04 — use short alias names)
# Listed in order: recommended first
OPENAI_MODELS: list[str] = [
    "gpt-4.1-mini",   # 2.5 M free token/day  ← recommended default
    "gpt-4.1-nano",   # 2.5 M free token/day  — cheapest
    "gpt-4o-mini",    # 2.5 M free token/day  — previous gen
    "gpt-5-mini",     # 2.5 M free token/day  — latest gen mini
    "gpt-4.1",        # 1 M free token/day (250 K tier 1-2) — high quality
    "gpt-4o",         # 1 M free token/day (250 K tier 1-2) — proven
    "gpt-5",          # 1 M free token/day (250 K tier 1-2) — best quality
]

CLAUDE_MODELS: list[str] = [
    "claude-haiku-4-5-20251001",  # cheapest  ← default
    "claude-sonnet-4-6",           # balanced
]

# Free daily token limit per OpenAI model group.
# OpenAI billing rule: if a SINGLE request would exceed the daily limit,
# the ENTIRE request is billed at standard rates (not just the overage).
OPENAI_FREE_TIER_DAILY: dict[str, int] = {
    "gpt-4.1-mini":   2_500_000,
    "gpt-4.1-nano":   2_500_000,
    "gpt-4o-mini":    2_500_000,
    "gpt-5-mini":     2_500_000,
    "gpt-5-nano":     2_500_000,
    "gpt-4.1":          250_000,
    "gpt-4o":           250_000,
    "gpt-5":            250_000,
    "o3":               250_000,
    "o1":               250_000,
}

# Estimated tokens consumed per AI call with "expanded context" mode:
# prompt up to ~4 500 + response up to ~2 000.
AI_TOKENS_PER_CALL_EST = 6_500

# ---------------------------------------------------------------------------
# Market data (real-time, no API key required)
# ---------------------------------------------------------------------------
MARKET_DATA_CACHE_SECONDS = 3_600   # refresh at most once per hour

# ---------------------------------------------------------------------------
# Sensor suffix tokens
# ---------------------------------------------------------------------------
SENSOR_CHEAPEST = "cheapest"
SENSOR_AVERAGE = "average"
SENSOR_TREND = "trend"
SENSOR_PREDICTION = "prediction"
SENSOR_PREDICTION_3D = "prediction_3d"
SENSOR_AI_INSIGHT = "ai_insight"
SENSOR_STATION = "station"

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
