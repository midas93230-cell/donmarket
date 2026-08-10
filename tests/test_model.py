"""Tests du parsing — la couche qui casse en silence quand l'API change."""

from __future__ import annotations

from datetime import datetime, timezone

from donmarket.model import parse_gamma_market, parse_iso, to_float, to_list


class TestToFloat:
    def test_converts_api_string_numbers(self):
        # Arrange / Act / Assert — l'API renvoie ses nombres en chaînes.
        assert to_float("0.0195") == 0.0195

    def test_returns_default_on_empty_string(self):
        assert to_float("", default=0.0) == 0.0

    def test_returns_default_on_garbage(self):
        assert to_float("n/a", default=None) is None

    def test_rejects_booleans_which_python_treats_as_numbers(self):
        # Sans garde explicite, True deviendrait 1.0 et fausserait un prix.
        assert to_float(True, default=None) is None


class TestToList:
    def test_decodes_json_string_lists(self):
        # `outcomes` arrive en '["Yes", "No"]', pas en vraie liste.
        assert to_list('["Yes", "No"]') == ["Yes", "No"]

    def test_passes_through_real_lists(self):
        assert to_list(["Yes", "No"]) == ["Yes", "No"]

    def test_returns_empty_on_malformed_json(self):
        assert to_list("[not json") == []


class TestParseIso:
    def test_parses_z_suffix_as_utc(self):
        parsed = parse_iso("2026-12-31T00:00:00Z")
        assert parsed == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_returns_none_on_missing_date(self):
        assert parse_iso(None) is None


def _raw_market(**overrides):
    """Un marché Gamma minimal, calqué sur la forme réelle de l'API."""
    raw = {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.40", "0.60"]',
        "volumeNum": 1000.0,
        "volume24hr": 500.0,
        "liquidityNum": 250.0,
        "bestBid": 0.39,
        "bestAsk": 0.41,
        "spread": 0.02,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.001,
        "endDate": "2026-12-31T00:00:00Z",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    raw.update(overrides)
    return raw


class TestParseGammaMarket:
    def test_builds_outcomes_from_parallel_json_strings(self):
        market = parse_gamma_market(_raw_market())
        assert market is not None
        assert [o.name for o in market.outcomes] == ["Yes", "No"]
        assert market.token_ids == ("111", "222")
        assert market.outcomes[0].price == 0.40

    def test_returns_none_without_condition_id(self):
        assert parse_gamma_market(_raw_market(conditionId="")) is None

    def test_closed_market_is_not_tradable(self):
        market = parse_gamma_market(_raw_market(closed=True))
        assert market is not None and not market.is_tradable

    def test_market_refusing_orders_is_not_tradable(self):
        market = parse_gamma_market(_raw_market(acceptingOrders=False))
        assert market is not None and not market.is_tradable

    def test_notional_min_order_uses_ask_and_min_size(self):
        # 5 parts à 0,41 $ = 2,05 $ : le vrai ticket d'entrée.
        market = parse_gamma_market(_raw_market())
        assert market is not None
        assert market.notional_min_order() == 5 * 0.41
