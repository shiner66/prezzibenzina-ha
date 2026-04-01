"""CSV parsing for MIMIT fuel price data."""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# MIMIT CSV field names — anagrafica_impianti_attivi.csv
# idImpianto|gestore|Bandiera|Tipo|nome|indirizzo|comune|provincia|latitudine|longitudine
_REGISTRY_FIELDS = (
    "idImpianto",
    "gestore",
    "Bandiera",
    "Tipo",
    "nome",
    "indirizzo",
    "comune",
    "provincia",
    "latitudine",
    "longitudine",
)

# MIMIT CSV field names — prezzo_alle_8.csv
# idImpianto|descCarburante|prezzo|isSelf|dtComu
_PRICES_FIELDS = (
    "idImpianto",
    "descCarburante",
    "prezzo",
    "isSelf",
    "dtComu",
)

_DATE_FORMAT = "%d/%m/%Y %H:%M:%S"


@dataclass
class Station:
    """A fuel station from the MIMIT registry."""

    id: int
    gestore: str
    bandiera: str
    tipo: str
    nome: str
    indirizzo: str
    comune: str
    provincia: str
    lat: float
    lon: float


@dataclass
class PriceRecord:
    """A price report for a specific fuel type at a station."""

    station_id: int
    fuel_type: str
    price: float
    is_self: bool
    reported_at: datetime


@dataclass
class EnrichedStation:
    """A price record merged with its station registry data."""

    station: Station
    fuel_type: str
    price: float
    is_self: bool
    reported_at: datetime
    distance_km: float = field(default=0.0)

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for sensor attributes)."""
        return {
            "id": self.station.id,
            "name": self.station.nome,
            "address": self.station.indirizzo,
            "comune": self.station.comune,
            "provincia": self.station.provincia,
            "bandiera": self.station.bandiera,
            "price": self.price,
            "is_self_service": self.is_self,
            "distance_km": round(self.distance_km, 2),
            "reported_at": self.reported_at.isoformat(),
            "lat": self.station.lat,
            "lon": self.station.lon,
        }


def parse_registry_csv(raw_csv: str) -> dict[int, Station]:
    """Parse the MIMIT station registry CSV.

    Returns a mapping of station_id → Station.
    Rows with invalid coordinates or missing IDs are silently skipped.
    """
    registry: dict[int, Station] = {}
    reader = csv.reader(io.StringIO(raw_csv), delimiter="|", skipinitialspace=True)
    # Skip header row
    try:
        next(reader)
    except StopIteration:
        return registry

    for row_num, row in enumerate(reader, start=2):
        if len(row) < len(_REGISTRY_FIELDS):
            _LOGGER.debug("registry row %d: too few fields (%d), skipping", row_num, len(row))
            continue
        try:
            station_id = int(row[0])
            lat = float(row[8].replace(",", "."))
            lon = float(row[9].replace(",", "."))
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise ValueError("out of range")
        except (ValueError, IndexError) as exc:
            _LOGGER.debug("registry row %d: invalid data (%s), skipping", row_num, exc)
            continue

        registry[station_id] = Station(
            id=station_id,
            gestore=row[1].strip(),
            bandiera=row[2].strip(),
            tipo=row[3].strip(),
            nome=row[4].strip(),
            indirizzo=row[5].strip(),
            comune=row[6].strip(),
            provincia=row[7].strip(),
            lat=lat,
            lon=lon,
        )

    _LOGGER.debug("Parsed %d stations from registry", len(registry))
    return registry


def parse_prices_csv(raw_csv: str) -> list[PriceRecord]:
    """Parse the MIMIT daily prices CSV.

    Returns a list of PriceRecord. Rows with invalid data are skipped.
    """
    records: list[PriceRecord] = []
    reader = csv.reader(io.StringIO(raw_csv), delimiter="|", skipinitialspace=True)
    # Skip header
    try:
        next(reader)
    except StopIteration:
        return records

    for row_num, row in enumerate(reader, start=2):
        # Strip trailing empty fields caused by trailing pipe
        while row and row[-1] == "":
            row.pop()
        if len(row) < len(_PRICES_FIELDS):
            _LOGGER.debug("prices row %d: too few fields (%d), skipping", row_num, len(row))
            continue
        try:
            station_id = int(row[0])
            fuel_type = row[1].strip()
            price = float(row[2].replace(",", "."))
            is_self = row[3].strip() == "1"
            reported_at = datetime.strptime(row[4].strip(), _DATE_FORMAT)
            if price <= 0:
                raise ValueError("non-positive price")
        except (ValueError, IndexError) as exc:
            _LOGGER.debug("prices row %d: invalid data (%s), skipping", row_num, exc)
            continue

        records.append(
            PriceRecord(
                station_id=station_id,
                fuel_type=fuel_type,
                price=price,
                is_self=is_self,
                reported_at=reported_at,
            )
        )

    _LOGGER.debug("Parsed %d price records", len(records))
    return records


def parse_regional_csv(raw_csv: str) -> dict[str, float]:
    """Parse the MIMIT MediaRegionaleStradale.csv for national average prices.

    Returns a mapping of fuel_type → national average price (EUR/L or EUR/kg).
    Returns an empty dict on any parse failure (graceful degradation).

    MIMIT publishes this in a few different layouts over time, so we try
    multiple delimiter/field strategies and accept the first that yields
    recognisable fuel names and valid prices.
    """
    result: dict[str, float] = {}

    # Normalise known fuel names in MIMIT labels to our canonical names
    _LABEL_MAP: dict[str, str] = {
        "benzina": "Benzina",
        "gasolio": "Gasolio",
        "gasolio autotrazione": "Gasolio",
        "gpl": "GPL",
        "metano": "Metano",
        "hvo": "HVO",
        "gasolio riscaldamento": "Gasolio Riscaldamento",
    }

    def _try_price(value: str) -> float | None:
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None

    # Try different delimiters
    for delimiter in ("|", ";", ",", "\t"):
        try:
            reader = csv.reader(io.StringIO(raw_csv), delimiter=delimiter)
            rows = [r for r in reader if any(c.strip() for c in r)]
        except Exception:
            continue

        if len(rows) < 2:
            continue

        # Strategy A: first column = fuel name, second column = national average
        # e.g. "Benzina | 1.747 | ..."
        candidate: dict[str, float] = {}
        for row in rows[1:]:  # skip header
            if len(row) < 2:
                continue
            label = row[0].strip().lower()
            fuel = _LABEL_MAP.get(label)
            if fuel is None:
                continue
            # Find the first parseable price in the row (skip the label column)
            for cell in row[1:]:
                p = _try_price(cell)
                if p is not None and 0.3 < p < 5.0:
                    candidate[fuel] = p
                    break

        if len(candidate) >= 2:
            result = candidate
            break

        # Strategy B: first row = headers with fuel names, subsequent rows = regions
        # Find a "Media" or "Nazionale" or "Italia" row as the national figure
        header = [h.strip().lower() for h in rows[0]]
        fuel_cols: dict[int, str] = {}
        for col_idx, hdr in enumerate(header):
            fuel = _LABEL_MAP.get(hdr)
            if fuel:
                fuel_cols[col_idx] = fuel

        if fuel_cols:
            for row in rows[1:]:
                row_label = row[0].strip().lower() if row else ""
                if any(kw in row_label for kw in ("media", "nazional", "italia", "totale")):
                    for col_idx, fuel in fuel_cols.items():
                        if col_idx < len(row):
                            p = _try_price(row[col_idx])
                            if p is not None and 0.3 < p < 5.0:
                                result[fuel] = p
                    if len(result) >= 2:
                        break

        if len(result) >= 2:
            break

    if result:
        _LOGGER.debug("Parsed national averages: %s", result)
    else:
        _LOGGER.debug("Could not parse regional CSV — national averages unavailable")

    return result


def parse_ospzapi_distributori(
    distributori: list[dict[str, Any]],
    registry: dict[int, Station],
    user_lat: float,
    user_lon: float,
) -> list[EnrichedStation]:
    """Convert the ospzApi ``distributori`` list into EnrichedStation objects.

    The ospzApi is an unofficial MIMIT REST endpoint that queries the live
    database (potentially fresher than the 08:00 CSV snapshot).  Its response
    format has varied slightly across versions, so this parser uses multiple
    field-name fallbacks and discards malformed entries.

    Returns an empty list on total failure; partial results are returned when
    only some entries are parseable.
    """
    from .geo import haversine_km  # local import to avoid circular

    results: list[EnrichedStation] = []

    for d in distributori:
        if not isinstance(d, dict):
            continue

        # ---- Station ID ----
        station_id: int | None = None
        for id_field in ("id", "idImpianto", "idDistributore"):
            if id_field in d:
                try:
                    station_id = int(d[id_field])
                    break
                except (ValueError, TypeError):
                    pass
        if station_id is None:
            continue

        # ---- Coordinates ----
        lat: float | None = None
        lon: float | None = None
        for lat_field in ("lat", "latitudine"):
            if lat_field in d:
                try:
                    lat = float(str(d[lat_field]).replace(",", "."))
                    break
                except (ValueError, TypeError):
                    pass
        for lon_field in ("lon", "longitudine"):
            if lon_field in d:
                try:
                    lon = float(str(d[lon_field]).replace(",", "."))
                    break
                except (ValueError, TypeError):
                    pass

        # ---- Price list ----
        prices_list: list[dict] | None = None
        for pf in ("prezzo", "carburanti", "prezzi", "fuel"):
            val = d.get(pf)
            if isinstance(val, list):
                prices_list = val
                break
        if not prices_list:
            continue

        # ---- Station object (prefer registry, fall back to inline data) ----
        station = registry.get(station_id)
        if station is None:
            if lat is None or lon is None:
                continue  # can't use without coordinates
            station = Station(
                id=station_id,
                gestore=str(d.get("gestore", "")),
                bandiera=str(d.get("bandiera", "")),
                tipo=str(d.get("tipoImpianto", d.get("tipo", ""))),
                nome=str(d.get("nome", f"Impianto {station_id}")),
                indirizzo=str(d.get("indirizzo", "")),
                comune=str(d.get("comune", "")),
                provincia=str(d.get("provincia", "")),
                lat=lat,
                lon=lon,
            )

        s_lat = station.lat if station.lat else (lat or 0.0)
        s_lon = station.lon if station.lon else (lon or 0.0)
        distance = haversine_km(user_lat, user_lon, s_lat, s_lon) if s_lat and s_lon else 0.0

        for item in prices_list:
            if not isinstance(item, dict):
                continue

            # Fuel type
            fuel_type: str | None = None
            for ff in ("carburante", "descCarburante", "fuel", "tipo"):
                if ff in item:
                    fuel_type = str(item[ff]).strip()
                    break
            if not fuel_type:
                continue

            # Price value
            price: float | None = None
            for pf in ("prezzo", "price", "valore", "prezzo_self", "prezzo_servito"):
                if pf in item:
                    try:
                        price = float(str(item[pf]).replace(",", "."))
                        break
                    except (ValueError, TypeError):
                        pass
            if price is None or price <= 0:
                continue

            # Self-service flag
            is_self = False
            for sf in ("self", "isSelf", "is_self", "selfService"):
                if sf in item:
                    val = item[sf]
                    is_self = val in (True, 1, "1", "true", "True", "SI", "si")
                    break

            # Reported timestamp
            reported_at = datetime.now()
            for df in ("dtComu", "data", "dt", "reported_at", "dataOra"):
                if df in item:
                    try:
                        reported_at = datetime.strptime(str(item[df]), "%d/%m/%Y %H:%M:%S")
                        break
                    except (ValueError, TypeError):
                        pass

            results.append(
                EnrichedStation(
                    station=station,
                    fuel_type=fuel_type,
                    price=price,
                    is_self=is_self,
                    reported_at=reported_at,
                    distance_km=distance,
                )
            )

    _LOGGER.debug("Parsed %d price records from ospzApi", len(results))
    return results


def merge_prices_with_registry(
    prices: list[PriceRecord],
    registry: dict[int, Station],
) -> list[EnrichedStation]:
    """Join price records with registry on station_id.

    Price records with no matching registry entry are dropped.
    """
    enriched: list[EnrichedStation] = []
    missing = 0

    for record in prices:
        station = registry.get(record.station_id)
        if station is None:
            missing += 1
            continue
        enriched.append(
            EnrichedStation(
                station=station,
                fuel_type=record.fuel_type,
                price=record.price,
                is_self=record.is_self,
                reported_at=record.reported_at,
            )
        )

    if missing:
        _LOGGER.debug(
            "Dropped %d price records with no registry entry (normal if registry cache is old)",
            missing,
        )

    return enriched
