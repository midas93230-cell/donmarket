"""Tests de la stratégie de récompenses — dont trois régressions payées cher.

Chacune de ces trois erreurs a produit un chiffre faux et crédible, ce qui est
bien pire qu'un plantage :

1. `rewardsMaxSpread` lu comme des dollars au lieu de pourcents : tout le
   carnet qualifie, la concurrence est surestimée, le rendement s'effondre.
2. Marché déjà résolu mais encore `closed=false` : pool intact, liquidité
   nulle, rendement fantôme mesuré à 118 %/jour.
3. Classement par rendement BRUT : conduit droit aux marchés qui dérivent le
   plus, donc à la perte (net médian −1,60 %/jour sur les 60 meilleurs au brut,
   mesuré le 2026-07-28 ; seuls 16 sur 60 couvrent leur propre risque).

Une quatrième, découverte en réparant ces tests : la dérive et le rendement
doivent être dans la MÊME unité pour être soustraits. La dérive se rapporte au
capital engagé (jeu complet = 1 $), pas au prix moyen.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from donmarket.analysis.opportunities import Mode
from donmarket.analysis.rewards import (
    judged_on_average,
    reranked_on_average,
    RewardCandidate,
    RewardThresholds,
    allocate,
    daily_pool,
    evaluate_reward_market,
    path_stats,
    rank_reward_markets,
)
from donmarket.api.clob import Book, Level
from donmarket.model import parse_gamma_market

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


def _market(
    *,
    end_hours: float = 96.0,
    daily_rate: float = 100.0,
    min_size: float = 50.0,
    max_spread: float = 3.0,
    condition_id: str = "0xabc",
    tokens: tuple[str, str] = ("111", "222"),
):
    raw = {
        "conditionId": condition_id,
        "question": "Will X happen?",
        "slug": "will-x-happen",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{tokens[0]}", "{tokens[1]}"]',
        "outcomePrices": '["0.40", "0.60"]',
        "volume24hr": 100_000.0,
        "orderMinSize": 5,
        "endDate": (NOW + timedelta(hours=end_hours)).isoformat().replace("+00:00", "Z"),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "clobRewards": [{"rewardsDailyRate": daily_rate}],
        "rewardsMinSize": min_size,
        "rewardsMaxSpread": max_spread,
    }
    market = parse_gamma_market(raw)
    assert market is not None
    return market


def _book(token_id: str, bid: float, ask: float, size: float = 100.0) -> Book:
    return Book(
        token_id=token_id,
        bids=(Level(price=bid, size=size),),
        asks=(Level(price=ask, size=size),),
        tick_size=0.001,
        min_order_size=5.0,
    )


def _books(size: float = 100.0) -> dict[str, Book]:
    return {
        "111": _book("111", 0.40, 0.42, size),
        "222": _book("222", 0.58, 0.60, size),
    }


def _flat(n: int = 60, price: float = 0.40) -> list[float]:
    return [price] * n


def _thresholds(mode: Mode = Mode.SERIEUX, bankroll: float = 100.0):
    return RewardThresholds.for_mode(mode, bankroll=bankroll)


class TestDailyPool:
    def test_sums_all_reward_programs(self):
        # Arrange
        market = _market()
        market.raw["clobRewards"] = [
            {"rewardsDailyRate": 60.0},
            {"rewardsDailyRate": 40.0},
        ]

        # Act
        pool = daily_pool(market)

        # Assert
        assert pool == 100.0

    def test_ignores_unreadable_entries_instead_of_failing(self):
        # Arrange
        market = _market()
        market.raw["clobRewards"] = [
            {"rewardsDailyRate": "25"},
            {"rewardsDailyRate": None},
            "pas un objet",
        ]

        # Act / Assert
        assert daily_pool(market) == 25.0

    def test_returns_zero_when_no_reward_programme(self):
        # Arrange
        market = _market()
        market.raw["clobRewards"] = []

        # Act / Assert
        assert daily_pool(market) == 0.0


class TestBandUnit:
    """La conversion pourcent → dollars de `rewardsMaxSpread`, en un seul endroit.

    La forme du score vit dans `test_scoring` ; ce qui se joue ICI est l'unité.
    C'est `evaluate_reward_market` qui divise par 100, et rien d'autre ne peut
    rattraper l'erreur en aval.
    """

    def test_max_spread_is_a_percentage_not_a_price(self):
        """RÉGRESSION : `rewardsMaxSpread` vaut 3.0 pour 3 cents, pas 3 dollars.

        Sans la division par 100, la bande couvre tout le carnet : un mur posté
        à 11 cents du milieu — qui ne marque rien et n'est donc PAS de la
        concurrence — se mettrait à peser dans le dénominateur, et écraserait
        le rendement d'un marché en réalité désert.
        """
        # Arrange : le même marché, avec et sans un mur à 0,30 $ (0,11 du milieu).
        # Il ne change pas le meilleur bid, donc pas le milieu : seul son
        # comptage dans la bande peut faire varier le résultat.
        near = _books(size=100.0)
        far = _books(size=100.0)
        far["111"] = Book(
            token_id="111",
            bids=(Level(price=0.30, size=10_000.0), Level(price=0.40, size=100.0)),
            asks=(Level(price=0.42, size=100.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )

        # Act
        with_wall = evaluate_reward_market(
            _market(), far, prices=_flat(), thresholds=_thresholds(), now=NOW
        )
        without_wall = evaluate_reward_market(
            _market(), near, prices=_flat(), thresholds=_thresholds(), now=NOW
        )

        # Assert
        assert with_wall is not None and without_wall is not None
        assert with_wall.competing_q == pytest.approx(without_wall.competing_q)
        # Et le test n'est pas creux : il y a bien de la concurrence à compter.
        assert without_wall.competing_q > 0.0

    def test_returns_none_on_one_sided_book(self):
        """Un carnet sans les deux côtés n'a pas de milieu, donc pas de distance.

        Rendre 0 laisserait croire à un pool désert — exactement la situation
        qu'on cherche — alors qu'on ne sait simplement pas lire le carnet.
        """
        # Arrange
        books = _books()
        books["111"] = Book(
            token_id="111",
            bids=(Level(price=0.40, size=100.0),),
            asks=(),
            tick_size=0.001,
            min_order_size=5.0,
        )

        # Act / Assert
        assert (
            evaluate_reward_market(
                _market(), books, prices=_flat(), thresholds=_thresholds(), now=NOW
            )
            is None
        )


class TestPathStats:
    def test_drift_is_net_move_and_oscillation_is_total_travel(self):
        """Un aller-retour ne dérive pas : il oscille. C'est toute la nuance."""
        # Arrange : 0,40 → 0,50 → 0,40
        prices = [0.40] * 20 + [0.50] * 20 + [0.40] * 20

        # Act
        stats = path_stats(prices)

        # Assert
        assert stats is not None
        assert stats.drift == 0.0  # même prix au début et à la fin
        assert stats.oscillation > 0.0  # mais le prix a bougé deux fois

    def test_trend_produces_drift(self):
        # Arrange : 0,400 → 0,459, soit 5,9 cents de mouvement net
        prices = [0.40 + 0.001 * i for i in range(60)]

        # Act
        stats = path_stats(prices)

        # Assert : la dérive est en % du CAPITAL, pas du prix. Un jeu complet
        # coûte 1 $, donc 5,9 cents de mouvement = 5,9 % du capital perdu.
        # Rapportée au prix moyen (0,4295 $) elle afficherait 13,7 %, un chiffre
        # qu'on ne peut pas soustraire d'un rendement exprimé en % du capital.
        assert stats is not None
        assert stats.drift == pytest.approx(5.9, abs=0.01)

    def test_returns_none_when_history_too_short_to_mean_anything(self):
        # Act / Assert
        assert path_stats([0.4, 0.41, 0.42]) is None
        assert path_stats([]) is None


class TestEvaluateRewardMarket:
    def test_rejects_market_resolving_too_soon(self):
        """RÉGRESSION : un marché résolu garde son pool et perd sa liquidité.

        C'est ce qui a produit un rendement fantôme de 118 %/jour.
        """
        # Arrange : échéance dépassée de 3 heures, encore `closed=false`
        market = _market(end_hours=-3.0)

        # Act
        candidate = evaluate_reward_market(
            market, _books(), prices=_flat(), thresholds=_thresholds(), now=NOW
        )

        # Assert
        assert candidate is not None
        assert not candidate.is_actionable
        assert any("échéance" in reason for reason in candidate.rejected_by)

    def test_rejects_ticket_above_bankroll(self):
        # Arrange : ticket de 200 parts ≈ 200 $, capital de 100 $
        market = _market(min_size=200.0)

        # Act
        candidate = evaluate_reward_market(
            market,
            _books(),
            prices=_flat(),
            thresholds=_thresholds(bankroll=100.0),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert any("ticket" in reason for reason in candidate.rejected_by)

    def test_rejects_high_yield_market_that_drifts(self):
        """RÉGRESSION : le rendement brut ne suffit pas à décider.

        Un marché peut payer beaucoup et dériver davantage : c'est une perte.
        """
        # Arrange : pool de 7 $/jour face à 20 $ de liquidité concurrente et
        # 50 $ engagés → 10 %/jour brut, ce qui est déjà dans la queue haute de
        # ce qui existe réellement. En face, le prix monte de 0,40 à 0,636 :
        # 23,6 % du capital pour qui est rempli du mauvais côté. Le brut est
        # attractif, le net est une perte — c'est tout l'objet du test.
        market = _market(daily_rate=7.0)
        drifting = [0.40 + 0.004 * i for i in range(60)]

        # Act
        candidate = evaluate_reward_market(
            market,
            _books(size=10.0),
            prices=drifting,
            thresholds=_thresholds(),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert candidate.gross_yield > 1.0  # attractif en apparence
        assert candidate.net_yield < candidate.gross_yield
        assert any("net" in reason for reason in candidate.rejected_by)

    def test_accepts_stable_market_with_underpopulated_pool(self):
        # Arrange : pool de 10 $/jour, 40 $ de concurrence, prix plat → 11 %/jour
        market = _market(daily_rate=10.0)

        # Act
        candidate = evaluate_reward_market(
            market,
            _books(size=20.0),
            prices=_flat(),
            thresholds=_thresholds(),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert candidate.is_actionable, candidate.rejected_by
        assert candidate.drift == 0.0
        assert candidate.daily_usd > 0.0

    def test_serious_mode_refuses_to_decide_without_history(self):
        """Sans historique, la dérive est inconnue — donc le risque aussi."""
        # Act
        candidate = evaluate_reward_market(
            _market(),
            _books(),
            prices=None,
            thresholds=_thresholds(Mode.SERIEUX),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert any("risque" in reason for reason in candidate.rejected_by)

    def test_a_round_trip_costs_even_when_the_drift_is_zero(self):
        """Le trou qui a fait céder le majorant, en un test.

        Prix 0,40 → 0,46 → 0,40 répété : le prix finit où il a commencé, donc
        `drift` vaut ZÉRO et l'ancien modèle annonçait un risque nul. La cote
        est à ±1,5 cent (bande 3 %), chaque mouvement de 6 cents la traverse,
        et chaque aller-retour se paie. Le coût retenu doit être strictement
        négatif.
        """
        # Arrange
        prices = [0.40, 0.46] * 30 + [0.40]  # le retour final annule la dérive

        # Act
        candidate = evaluate_reward_market(
            _market(),
            _books(),
            prices=prices,
            thresholds=_thresholds(Mode.NORMAL),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert candidate.drift == pytest.approx(0.0)
        assert candidate.replay_cost is not None
        assert candidate.inventory_cost < 0.0
        assert candidate.net_yield < candidate.gross_yield

    def test_a_slow_trend_is_still_caught_by_the_drift(self):
        """Le trou SYMÉTRIQUE : le rejeu ne voit pas les tendances lentes.

        Une dérive de 0,4 cent par minute ne touche jamais une cote posée à
        1,5 cent, puisqu'on recote chaque minute autour du nouveau prix : le
        rejeu mesure un coût nul sur un marché qui a pourtant parcouru 23
        cents. C'est `drift` qui doit alors l'emporter, sans quoi ce marché
        remonterait en tête du classement.
        """
        # Arrange
        prices = [0.40 + 0.004 * i for i in range(60)]

        # Act
        candidate = evaluate_reward_market(
            _market(),
            _books(),
            prices=prices,
            thresholds=_thresholds(Mode.NORMAL),
            now=NOW,
        )

        # Assert
        assert candidate is not None
        assert candidate.replay_cost == pytest.approx(0.0)  # aucun remplissage
        assert candidate.drift > 20.0
        assert candidate.inventory_cost == pytest.approx(-candidate.drift)

    def test_the_retained_cost_is_the_worse_of_the_two(self):
        """Ni l'une ni l'autre ne majore : on prend la pire, toujours."""
        # Arrange
        cheap = RewardCandidate(
            condition_id="0x1",
            question="?",
            slug="s",
            daily_pool=10.0,
            competing_q=1.0,
            own_q=1.0,
            engaged_usd=50.0,
            gross_yield=5.0,
            drift=2.0,
            oscillation=0.0,
            hours_left=48.0,
            replay_cost=-9.0,
        )

        # Act / Assert
        assert cheap.inventory_cost == pytest.approx(-9.0)  # le rejeu est pire
        assert replace(cheap, replay_cost=-0.5).inventory_cost == pytest.approx(-2.0)
        assert replace(cheap, replay_cost=None).inventory_cost == pytest.approx(-2.0)

    def test_returns_none_when_market_has_no_reward_pool(self):
        # Arrange
        market = _market(daily_rate=0.0)

        # Act / Assert
        assert (
            evaluate_reward_market(
                market, _books(), prices=_flat(), thresholds=_thresholds(), now=NOW
            )
            is None
        )

    def test_returns_none_when_a_book_is_missing(self):
        """Une seule branche cotée ne permet pas de coter des deux côtés."""
        # Act / Assert
        assert (
            evaluate_reward_market(
                _market(),
                {"111": _book("111", 0.40, 0.42)},
                prices=_flat(),
                thresholds=_thresholds(),
                now=NOW,
            )
            is None
        )


class TestAllocation:
    """Le capital est partagé : six tickets de 50 $ ne tiennent pas dans 100 $."""

    def _candidate(self, name: str, *, engaged: float, net: float):
        # `net_yield` est dérivé de `gross_yield − drift` : on le fabrique en
        # posant une dérive nulle, plutôt qu'en contournant la propriété.
        return RewardCandidate(
            condition_id=name,
            question=name,
            slug=name,
            daily_pool=100.0,
            competing_q=100.0,
            own_q=25.0,
            engaged_usd=engaged,
            gross_yield=net,
            drift=0.0,
            oscillation=0.0,
            hours_left=96.0,
        )

    def test_averaging_a_crowded_book_demotes_a_flattering_snapshot(self):
        """Le cas mesuré le 29/07 : l'instantané flatte, la moyenne corrige.

        Deux candidats. Le premier a été surpris à un instant creux
        (concurrence 100) mais tient en moyenne 900 ; le second est stable. Au
        balayage le premier passe devant ; à la moyenne il tombe derrière.
        """
        flattering = self._candidate("creux", engaged=50.0, net=50.0)
        steady = self._candidate("stable", engaged=50.0, net=20.0)

        ranked = reranked_on_average(
            [flattering, steady], {"creux": 900.0, "stable": 100.0}
        )

        assert [c.condition_id for c in ranked] == ["stable", "creux"]

    def test_a_candidate_without_an_average_keeps_its_snapshot(self):
        """L'absence de mesure n'est pas une mauvaise mesure."""
        unseen = self._candidate("jamais-vu", engaged=50.0, net=30.0)

        ranked = reranked_on_average([unseen], {})

        assert ranked[0].gross_yield == pytest.approx(30.0)
        assert ranked[0].competing_q == pytest.approx(100.0)

    def test_averaging_never_touches_our_own_score(self):
        """Notre score dépend de notre prix et de notre taille : il est à nous."""
        candidate = self._candidate("x", engaged=50.0, net=40.0)

        judged = judged_on_average(candidate, 400.0)

        assert judged.own_q == pytest.approx(candidate.own_q)
        assert judged.competing_q == pytest.approx(400.0)
        assert judged.gross_yield < candidate.gross_yield

    def test_stops_at_the_bankroll_instead_of_summing_everything(self):
        # Arrange : trois positions à 50 $ chacune, 100 $ en caisse
        candidates = [
            self._candidate("a", engaged=50.0, net=10.0),
            self._candidate("b", engaged=50.0, net=8.0),
            self._candidate("c", engaged=50.0, net=6.0),
        ]

        # Act
        held = allocate(candidates, bankroll=100.0)

        # Assert : deux positions, pas trois — et les deux meilleures
        assert [c.condition_id for c in held] == ["a", "b"]
        assert sum(c.engaged_usd for c in held) == 100.0

    def test_prefers_yield_per_dollar_over_absolute_gain(self):
        """Un gros ticket au faible rendement mange le capital pour rien."""
        # Arrange : 100 $ à 2 %/j rapporte 2 $/j ; 50 $ à 10 %/j en rapporte 5.
        gros = self._candidate("gros", engaged=100.0, net=2.0)
        efficace = self._candidate("efficace", engaged=50.0, net=10.0)

        # Act
        held = allocate([gros, efficace], bankroll=100.0)

        # Assert
        assert [c.condition_id for c in held] == ["efficace"]

    def test_skips_what_does_not_fit_and_keeps_looking(self):
        # Arrange : le deuxième ne tient plus, le troisième si
        candidates = [
            self._candidate("gros", engaged=80.0, net=9.0),
            self._candidate("moyen", engaged=50.0, net=5.0),
            self._candidate("petit", engaged=20.0, net=3.0),
        ]

        # Act
        held = allocate(candidates, bankroll=100.0)

        # Assert : on n'abandonne pas au premier refus de budget
        assert [c.condition_id for c in held] == ["gros", "petit"]

    def test_empty_bankroll_holds_nothing(self):
        # Act / Assert
        assert allocate([self._candidate("a", engaged=50.0, net=10.0)], bankroll=0.0) == []


class TestRanking:
    def test_ranks_by_net_yield_not_gross_yield(self):
        """Le cœur de la stratégie : c'est le net qui classe, pas le brut."""
        # Arrange : le « riche » paie 10 %/jour brut mais dérive de 23,6 % ;
        # le « calme » ne paie que 5,6 %/jour et ne dérive pas. Le brut classe
        # le riche premier, le net le classe dernier — et c'est le net qui paie.
        rich = _market(daily_rate=7.0, condition_id="0xrich", tokens=("111", "222"))
        calm = _market(daily_rate=5.0, condition_id="0xcalm", tokens=("333", "444"))
        books = {
            "111": _book("111", 0.40, 0.42, 10.0),
            "222": _book("222", 0.58, 0.60, 10.0),
            "333": _book("333", 0.40, 0.42, 20.0),
            "444": _book("444", 0.58, 0.60, 20.0),
        }
        histories = {
            "111": [0.40 + 0.004 * i for i in range(60)],  # dérive
            "333": _flat(),  # stable
        }

        # Act
        ranked = rank_reward_markets(
            [rich, calm], books, histories, bankroll=100.0, now=NOW
        )

        # Assert
        assert [c.condition_id for c in ranked] == ["0xcalm", "0xrich"]
        assert ranked[0].gross_yield < ranked[1].gross_yield  # le brut dit l'inverse
