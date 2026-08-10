"""Tests du moteur de décision — dont la régression du spread inter-branches."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from donmarket.analysis.opportunities import (
    Mode,
    Thresholds,
    affordable,
    evaluate_market,
    scan_opportunities,
)
from donmarket.api.clob import Book, Level, parse_book
from donmarket.model import parse_gamma_market

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def _market(end_days: int = 30, volume_24h: float = 100_000.0):
    raw = {
        "conditionId": "0xabc",
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.40", "0.60"]',
        "volume24hr": volume_24h,
        "orderMinSize": 5,
        "endDate": (NOW + timedelta(days=end_days)).isoformat().replace("+00:00", "Z"),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    market = parse_gamma_market(raw)
    assert market is not None
    return market


def _book(token_id: str, bid: float, ask: float, size: float = 1000.0) -> Book:
    return Book(
        token_id=token_id,
        bids=(Level(price=bid, size=size),),
        asks=(Level(price=ask, size=size),),
        tick_size=0.001,
        min_order_size=5.0,
    )


class TestBookOrdering:
    def test_best_prices_ignore_api_level_ordering(self):
        """L'API sert les bids croissants et les asks décroissants.

        Se fier à l'ordre reçu ferait prendre 0.001 pour le meilleur bid.
        """
        book = parse_book(
            {
                "asset_id": "111",
                "bids": [
                    {"price": "0.001", "size": "500"},
                    {"price": "0.019", "size": "100"},
                ],
                "asks": [
                    {"price": "0.999", "size": "500"},
                    {"price": "0.020", "size": "100"},
                ],
            }
        )
        assert book is not None
        assert book.best_bid == 0.019
        assert book.best_ask == 0.020

    def test_ordering_is_enforced_by_the_type_not_by_the_parser(self):
        """Un carnet construit à la main doit dire la vérité, lui aussi.

        Tant que le tri vivait dans `parse_book`, tout `Book(...)` construit
        ailleurs — un test, un backtest, un cache relu — pouvait présenter le
        PIRE prix comme le meilleur, sans jamais lever d'erreur. C'est
        exactement le piège que l'API tend, avec des chiffres crédibles à la
        sortie. L'invariant appartient donc au type.
        """
        book = Book(
            token_id="111",
            bids=(Level(price=0.30, size=1000.0), Level(price=0.40, size=100.0)),
            asks=(Level(price=0.99, size=500.0), Level(price=0.42, size=100.0)),
            tick_size=0.001,
            min_order_size=5.0,
        )

        assert book.best_bid == 0.40
        assert book.best_ask == 0.42
        # `cost_to_buy` consomme les paliers dans l'ordre : mal trié, il aurait
        # facturé 0,99 $ la part au lieu de 0,42 $.
        assert book.cost_to_buy(100.0) == pytest.approx(42.0)

    def test_zero_size_levels_are_dropped(self):
        book = parse_book(
            {"asset_id": "111", "bids": [{"price": "0.5", "size": "0"}], "asks": []}
        )
        assert book is not None and book.best_bid is None

    def test_cost_to_buy_walks_the_book(self):
        book = Book(
            token_id="111",
            bids=(),
            asks=(Level(price=0.40, size=10), Level(price=0.50, size=10)),
            tick_size=0.001,
            min_order_size=5.0,
        )
        # 15 parts = 10 à 0,40 + 5 à 0,50 = 6,50 $
        assert book.cost_to_buy(15) == 6.5

    def test_cost_to_buy_returns_none_when_book_too_thin(self):
        book = Book(
            token_id="111",
            bids=(),
            asks=(Level(price=0.40, size=10),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        assert book.cost_to_buy(50) is None


class TestSpreadRegression:
    def test_spread_is_measured_per_outcome_not_across_outcomes(self):
        """Régression : le spread se calcule branche par branche.

        Avec Yes 0.019/0.020 et No 0.980/0.981, l'ancien calcul
        max(asks) - min(bids) donnait 0.962 et faisait rejeter à tort un
        carnet en réalité très serré (0.001 de chaque côté).
        """
        market = _market()
        books = {"111": _book("111", 0.019, 0.020), "222": _book("222", 0.980, 0.981)}

        results = evaluate_market(market, books, mode=Mode.NORMAL, now=NOW)

        assert results
        assert all(abs(opp.spread - 0.001) < 1e-9 for opp in results)


class TestCompleteSetArbitrage:
    def test_detects_real_buy_side_edge(self):
        # Acheter les deux branches pour 0,95 $ rapporte 1 $ à la résolution.
        market = _market()
        books = {"111": _book("111", 0.44, 0.45), "222": _book("222", 0.49, 0.50)}

        buy = next(
            opp
            for opp in evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)
            if opp.kind == "buy_complete_set"
        )

        assert abs(buy.sum_price - 0.95) < 1e-9
        assert abs(buy.gross_edge - 0.05) < 1e-9
        assert buy.is_actionable

    def test_rejects_the_real_market_state_sum_just_above_one(self):
        """L'état réel mesuré sur les 2100 marchés : somme = 1,001."""
        market = _market()
        books = {"111": _book("111", 0.019, 0.020), "222": _book("222", 0.980, 0.981)}

        buy = next(
            opp
            for opp in evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)
            if opp.kind == "buy_complete_set"
        )

        assert abs(buy.sum_price - 1.001) < 1e-9
        assert not buy.is_actionable
        assert any("edge" in reason for reason in buy.rejected_by)

    def test_cost_buffer_makes_a_razor_thin_edge_unprofitable(self):
        # Marge brute de 0,001 $ : effacée par la réserve de coûts de 0,002 $.
        market = _market()
        books = {"111": _book("111", 0.40, 0.400), "222": _book("222", 0.59, 0.599)}

        buy = next(
            opp
            for opp in evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)
            if opp.kind == "buy_complete_set"
        )

        assert buy.gross_edge > 0
        assert buy.edge < 0

    def test_ignores_market_with_a_missing_book(self):
        # Un arbitrage incomplet est un pari déguisé, pas un arbitrage.
        market = _market()
        assert evaluate_market(market, {"111": _book("111", 0.4, 0.41)}, now=NOW) == []


class TestModes:
    def test_serious_mode_is_strictly_tighter_than_normal(self):
        strict = Thresholds.for_mode(Mode.SERIEUX)
        loose = Thresholds.for_mode(Mode.NORMAL)
        assert strict.min_edge > loose.min_edge
        assert strict.min_depth_usd > loose.min_depth_usd
        assert strict.max_spread < loose.max_spread

    def test_serious_mode_rejects_a_distant_resolution(self):
        # Même marge, mais capital immobilisé 2 ans : refusé en mode sérieux.
        market = _market(end_days=800)
        books = {"111": _book("111", 0.44, 0.45), "222": _book("222", 0.49, 0.50)}

        buy = next(
            opp
            for opp in evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)
            if opp.kind == "buy_complete_set"
        )

        assert not buy.is_actionable
        assert any("résolution" in reason for reason in buy.rejected_by)

    def test_serious_mode_rejects_a_dead_market(self):
        market = _market(volume_24h=10.0)
        books = {"111": _book("111", 0.44, 0.45), "222": _book("222", 0.49, 0.50)}

        buy = next(
            opp
            for opp in evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)
            if opp.kind == "buy_complete_set"
        )

        assert any("volume24h" in reason for reason in buy.rejected_by)

    def test_scan_returns_only_actionable_opportunities(self):
        market = _market()
        books = {"111": _book("111", 0.019, 0.020), "222": _book("222", 0.980, 0.981)}
        assert scan_opportunities([market], books, mode=Mode.SERIEUX, now=NOW) == []


class TestAffordability:
    def test_small_bankroll_cannot_meet_the_minimum_order(self):
        market = _market()
        books = {"111": _book("111", 0.44, 0.45), "222": _book("222", 0.49, 0.50)}
        buy = evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)[0]

        # 5 parts d'un jeu à 0,95 $ = 4,75 $ minimum.
        assert affordable(buy, bankroll=14.47, min_order_size=5.0)
        assert not affordable(buy, bankroll=2.00, min_order_size=5.0)

    def test_zero_bankroll_is_never_affordable(self):
        market = _market()
        books = {"111": _book("111", 0.44, 0.45), "222": _book("222", 0.49, 0.50)}
        buy = evaluate_market(market, books, mode=Mode.SERIEUX, now=NOW)[0]
        assert not affordable(buy, bankroll=0.0, min_order_size=5.0)
