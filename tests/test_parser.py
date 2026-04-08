"""Unit tests for parser.py — CSV parsing and data models."""
from __future__ import annotations

import textwrap
from datetime import datetime

import pytest

from custom_components.carburanti_mimit.parser import (
    EnrichedStation,
    Station,
    merge_prices_with_registry,
    parse_prices_csv,
    parse_registry_csv,
)


# ---------------------------------------------------------------------------
# Sample CSV data
# ---------------------------------------------------------------------------

_REGISTRY_CSV = textwrap.dedent("""\
    idImpianto|gestore|Bandiera|Tipo|nome|indirizzo|comune|provincia|latitudine|longitudine
    1|Gestore Uno|ENI|Stradale|Stazione ENI|Via Roma 1|Milano|MI|45.4654|9.1859
    2|Gestore Due|Q8|Stradale|Stazione Q8|Via Dante 2|Roma|RM|41.9028|12.4964
    3|Gestore Tre|IP|Autostradale|Stazione IP|A1 KM 100|Bologna|BO|44.4949|11.3426
""")

_PRICES_CSV = textwrap.dedent("""\
    idImpianto|descCarburante|prezzo|isSelf|dtComu
    1|Benzina|1.789|1|15/01/2024 08:00:00
    1|Gasolio|1.650|1|15/01/2024 08:00:00
    2|Benzina|1.810|0|15/01/2024 08:00:00
    3|Benzina|1.795|1|15/01/2024 08:00:00
    3|GPL|0.780|1|15/01/2024 08:00:00
""")

_PRICES_CSV_MALFORMED = textwrap.dedent("""\
    idImpianto|descCarburante|prezzo|isSelf|dtComu
    999|Benzina|abc|1|15/01/2024 08:00:00
    1|Benzina|1.789|1|15/01/2024 08:00:00
""")


# ---------------------------------------------------------------------------
# Station dataclass
# ---------------------------------------------------------------------------

class TestStation:
    def test_fields(self):
        s = Station(
            id=1, gestore="g", bandiera="ENI", tipo="Stradale",
            nome="Test", indirizzo="Via 1", comune="Milano", provincia="MI",
            lat=45.0, lon=9.0,
        )
        assert s.id == 1
        assert s.bandiera == "ENI"
        assert s.lat == 45.0


# ---------------------------------------------------------------------------
# EnrichedStation.to_dict
# ---------------------------------------------------------------------------

class TestEnrichedStationToDict:
    def test_to_dict_keys(self):
        from tests.conftest import make_enriched
        es = make_enriched(price=1.800)
        d = es.to_dict()
        assert "id" in d
        assert "name" in d
        assert "price" in d
        assert "distance_km" in d
        assert "is_self_service" in d

    def test_to_dict_values(self):
        from tests.conftest import make_enriched, make_station
        st = make_station(station_id=42, nome="ENI Test", comune="Torino")
        es = make_enriched(price=1.750, station=st, distance_km=2.5)
        d = es.to_dict()
        assert d["id"] == 42
        assert d["name"] == "ENI Test"
        assert d["price"] == 1.750
        assert d["distance_km"] == 2.5

    def test_to_dict_community_updated_at_none(self):
        from tests.conftest import make_enriched
        es = make_enriched()
        d = es.to_dict()
        assert d["community_updated_at"] is None

    def test_to_dict_community_updated_at_isoformat(self):
        from tests.conftest import make_enriched
        ts = datetime(2024, 1, 16, 10, 30, 0)
        es = make_enriched()
        es.community_updated_at = ts
        d = es.to_dict()
        assert d["community_updated_at"] == ts.isoformat()


# ---------------------------------------------------------------------------
# parse_registry_csv
# ---------------------------------------------------------------------------

class TestParseRegistryCsv:
    """parse_registry_csv returns dict[int, Station]."""

    def test_returns_correct_count(self):
        result = parse_registry_csv(_REGISTRY_CSV)
        assert len(result) == 3

    def test_returns_dict_keyed_by_int(self):
        result = parse_registry_csv(_REGISTRY_CSV)
        assert isinstance(result, dict)
        assert all(isinstance(k, int) for k in result)

    def test_known_ids_present(self):
        result = parse_registry_csv(_REGISTRY_CSV)
        assert 1 in result
        assert 2 in result
        assert 3 in result

    def test_first_station_fields(self):
        result = parse_registry_csv(_REGISTRY_CSV)
        s = result[1]
        assert s.nome == "Stazione ENI"
        assert s.bandiera == "ENI"
        assert s.lat == pytest.approx(45.4654)
        assert s.lon == pytest.approx(9.1859)

    def test_station_ids_match_keys(self):
        result = parse_registry_csv(_REGISTRY_CSV)
        for station_id, station in result.items():
            assert station.id == station_id

    def test_empty_input_returns_empty(self):
        result = parse_registry_csv("idImpianto|gestore|Bandiera|Tipo|nome|indirizzo|comune|provincia|latitudine|longitudine\n")
        assert len(result) == 0


# ---------------------------------------------------------------------------
# parse_prices_csv
# ---------------------------------------------------------------------------

class TestParsePricesCsv:
    def test_returns_correct_count(self):
        result = parse_prices_csv(_PRICES_CSV)
        assert len(result) == 5

    def test_price_is_float(self):
        result = parse_prices_csv(_PRICES_CSV)
        assert all(isinstance(r.price, float) for r in result)

    def test_is_self_is_bool(self):
        result = parse_prices_csv(_PRICES_CSV)
        assert all(isinstance(r.is_self, bool) for r in result)

    def test_reported_at_is_datetime(self):
        result = parse_prices_csv(_PRICES_CSV)
        assert all(isinstance(r.reported_at, datetime) for r in result)

    def test_skips_malformed_price(self):
        result = parse_prices_csv(_PRICES_CSV_MALFORMED)
        # Row with "abc" as price should be skipped
        prices = [r.price for r in result]
        assert all(isinstance(p, float) for p in prices)

    def test_fuel_type_preserved(self):
        result = parse_prices_csv(_PRICES_CSV)
        types = {r.fuel_type for r in result}
        assert "Benzina" in types
        assert "Gasolio" in types
        assert "GPL" in types

    def test_empty_input_returns_empty(self):
        result = parse_prices_csv("idImpianto|descCarburante|prezzo|isSelf|dtComu\n")
        assert result == []


# ---------------------------------------------------------------------------
# merge_prices_with_registry
# ---------------------------------------------------------------------------

class TestMergePricesWithRegistry:
    """merge_prices_with_registry takes (list[PriceRecord], dict[int, Station])."""

    def _registry(self) -> dict:
        return parse_registry_csv(_REGISTRY_CSV)

    def _prices(self):
        return parse_prices_csv(_PRICES_CSV)

    def test_returns_list_of_enriched(self):
        result = merge_prices_with_registry(self._prices(), self._registry())
        assert all(isinstance(r, EnrichedStation) for r in result)

    def test_unknown_station_id_excluded(self):
        extra = "idImpianto|descCarburante|prezzo|isSelf|dtComu\n9999|Benzina|1.800|1|15/01/2024 08:00:00\n"
        prices = parse_prices_csv(extra)
        result = merge_prices_with_registry(prices, self._registry())
        assert all(r.station.id != 9999 for r in result)

    def test_station_data_is_attached(self):
        result = merge_prices_with_registry(self._prices(), self._registry())
        for r in result:
            assert r.station is not None
            assert r.station.nome != ""

    def test_all_known_stations_present(self):
        result = merge_prices_with_registry(self._prices(), self._registry())
        # Stations 1, 2, 3 are in the registry; prices exist for all three
        merged_ids = {r.station.id for r in result}
        assert 1 in merged_ids
        assert 2 in merged_ids
        assert 3 in merged_ids

    def test_empty_prices_returns_empty(self):
        result = merge_prices_with_registry([], self._registry())
        assert result == []

    def test_empty_registry_returns_empty(self):
        result = merge_prices_with_registry(self._prices(), {})
        assert result == []
