"""Tests du plan d'ordres et de la connexion.

Ce module est le dernier avant l'argent réel. Les erreurs qu'il doit rendre
impossibles ne sont donc pas des plantages, ce sont des ordres :

1. Poser sur UNE SEULE branche d'un marché à deux branches. Ce n'est pas une
   demi-position, c'est un pari directionnel nu déguisé en tenue de marché —
   et c'est exactement ce que la stratégie prétend éviter.
2. Arrondir un prix VERS LE HAUT. Un tick de trop, sur deux branches, sur
   chaque marché, mange la marge sans jamais lever d'erreur.
3. Dépasser le capital. `engaged_usd` suppose bid_yes + bid_no ≈ 1 $ ; le prix
   réellement retenu peut s'en écarter de quelques ticks.
4. Laisser fuir un secret. Un objet qui contient une clé finit un jour dans un
   journal, une page ou un message d'erreur.
"""

from __future__ import annotations

import pytest

from donmarket.analysis.rewards import RewardCandidate
from donmarket.api.clob import Book, Level
from donmarket.execute.credentials import (
    Credentials,
    connection_status,
    load_credentials,
    short_address,
)
from donmarket.execute.orders import (
    OrderPlan,
    plan_orders,
    plan_portfolio,
    round_down_to_tick,
)

SECRET_VARS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_ADDRESS",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


def _candidate(*, engaged: float = 50.0, band: float = 0.03) -> RewardCandidate:
    return RewardCandidate(
        condition_id="0xabc",
        question="Will X happen?",
        slug="will-x",
        daily_pool=150.0,
        competing_q=250.0,
        own_q=25.0,
        engaged_usd=engaged,
        gross_yield=10.0,
        drift=2.0,
        oscillation=12.0,
        hours_left=96.0,
        token_ids=("111", "222"),
        max_spread=band,
    )


def _book(token_id: str, bid: float, ask: float) -> Book:
    return Book(
        token_id=token_id,
        bids=(Level(bid, 500.0),),
        asks=(Level(ask, 500.0),),
        tick_size=0.001,
        min_order_size=5.0,
    )


def _books(**prices: tuple[float, float]) -> dict[str, Book]:
    return {token: _book(token, bid, ask) for token, (bid, ask) in prices.items()}


class TestTickRounding:
    def test_rounds_down_never_up(self) -> None:
        # 0,4567 doit devenir 0,456 — jamais 0,457.
        assert round_down_to_tick(0.4567, 0.001) == pytest.approx(0.456)

    def test_leaves_an_exact_tick_alone(self) -> None:
        # Sans la tolérance, l'arithmétique flottante ferait descendre
        # 0,456 à 0,455 : un tick perdu sur chaque ordre déjà aligné.
        assert round_down_to_tick(0.456, 0.001) == pytest.approx(0.456)

    def test_a_zero_tick_changes_nothing(self) -> None:
        assert round_down_to_tick(0.4567, 0.0) == 0.4567


class TestPlanOrders:
    def test_quotes_both_branches_or_neither(self) -> None:
        orders = plan_orders(
            _candidate(), _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        )
        assert [order.token_id for order in orders] == ["111", "222"]

    def test_a_missing_book_cancels_the_whole_market(self) -> None:
        # Une seule branche cotée = position directionnelle nue.
        assert plan_orders(_candidate(), _books(**{"111": (0.40, 0.42)})) == ()

    def test_joins_the_queue_when_the_best_bid_qualifies(self) -> None:
        # Milieu 0,41 ; bande 0,03 ; meilleur bid 0,40 → déjà dans la bande.
        orders = plan_orders(
            _candidate(), _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        )
        assert orders[0].price == pytest.approx(0.40)
        assert "rejoint la file" in orders[0].reason

    def test_improves_the_book_when_the_best_bid_is_outside_the_band(self) -> None:
        """On vise le MILIEU de la bande, pas son bord.

        Milieu 0,45 ; bande 0,01 ; meilleur bid 0,30 → hors bande. L'ancienne
        règle postait à 0,44, soit le bord exact : `((v−s)/v)²` y vaut ZÉRO
        (voir `test_scoring`), donc tout le risque d'inventaire pour aucune
        récompense — et un arrondi au tick suffisait à sortir de la bande. On
        poste donc à 0,445, à mi-bande, où le score vaut 25 % du maximum.
        """
        candidate = _candidate(band=0.01)
        orders = plan_orders(
            candidate, _books(**{"111": (0.30, 0.60), "222": (0.30, 0.60)})
        )
        assert orders[0].price == pytest.approx(0.445)
        assert "premier servi" in orders[0].reason

    def test_size_is_the_reward_minimum(self) -> None:
        orders = plan_orders(
            _candidate(engaged=100.0),
            _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)}),
        )
        assert all(order.size == 100.0 for order in orders)

    def test_every_order_carries_the_reason_for_its_price(self) -> None:
        # Un prix sans motif est un prix qu'on ne peut pas contester.
        orders = plan_orders(
            _candidate(), _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        )
        assert all(order.reason for order in orders)

    def test_refuses_to_quote_below_the_minimum_price(self) -> None:
        # Marché à 0,0005 avec un pas de 0,001 : le prix qualifiant s'arrondit
        # à zéro. Mieux vaut ne rien poser qu'un ordre à prix nul.
        assert (
            plan_orders(
                _candidate(),
                _books(**{"111": (0.0005, 0.0006), "222": (0.0005, 0.0006)}),
            )
            == ()
        )


class TestPortfolio:
    def test_stops_when_the_capital_runs_out(self) -> None:
        books = _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        # Un marché coûte 0,40×50 + 0,58×50 = 49 $. Deux ne tiennent pas dans
        # 60 $, même si chacun tient isolément.
        plan = plan_portfolio([_candidate(), _candidate()], books, bankroll=60.0)
        assert plan.markets == 1
        assert plan.fits

    def test_says_why_a_market_was_left_out(self) -> None:
        books = _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        plan = plan_portfolio([_candidate(), _candidate()], books, bankroll=60.0)
        assert len(plan.skipped) == 1
        assert "restants" in plan.skipped[0]

    def test_notional_is_computed_on_the_prices_actually_retained(self) -> None:
        books = _books(**{"111": (0.40, 0.42), "222": (0.58, 0.60)})
        plan = plan_portfolio([_candidate()], books, bankroll=100.0)
        assert plan.notional == pytest.approx(0.40 * 50 + 0.58 * 50)

    def test_an_empty_plan_still_fits(self) -> None:
        assert OrderPlan(orders=(), bankroll=10.0).fits


class TestCredentials:
    def test_reports_disconnected_when_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in SECRET_VARS:
            monkeypatch.delenv(name, raising=False)
        assert load_credentials().is_connected is False

    def test_a_blank_variable_does_not_count_as_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "   ")
        assert load_credentials().has_private_key is False

    def test_the_object_never_carries_the_secret(self) -> None:
        # Ce qui n'est pas stocké ne peut pas fuir dans un journal.
        assert not hasattr(Credentials(None, True, True), "private_key")

    def test_status_never_leaks_a_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # La valeur cherchée ne doit ressembler à AUCUN nom de variable :
        # « SECRET » se trouve dans POLYMARKET_API_SECRET, qui est un nom
        # légitimement affiché dans la liste des éléments manquants.
        canary = "0xCANARI_NE_DOIT_PAS_SORTIR"
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", canary)
        monkeypatch.setenv("POLYMARKET_API_KEY", canary)
        monkeypatch.setenv(
            "POLYMARKET_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678"
        )
        rendered = repr(connection_status())
        assert "CANARI" not in rendered
        # L'adresse publique, elle, est affichée — mais tronquée.
        assert "0x1234…5678" in rendered

    def test_address_is_shown_truncated(self) -> None:
        assert short_address("0x1234567890abcdef1234") == "0x1234…1234"

    def test_execution_stays_impossible_even_when_connected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Avoir une clé ne pose aucun ordre : la signature n'existe pas encore,
        # et l'armement appartient au propriétaire du compte.
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xdeadbeef")
        assert connection_status()["can_execute"] is False
