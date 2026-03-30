"""CSV parsing for MIMIT fuel price data."""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime

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
