"""Le rejeu de tenue de marché, vérifié au cent près sur des chemins choisis.

Chaque attendu est calculé À LA MAIN dans la docstring du test. Un backtest
dont on relit la sortie pour en faire l'assertion ne teste rien : il enregistre
le bug. Les chemins sont donc courts exprès, pour rester calculables de tête.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from donmarket.backtest.replay import ReplayResult, replay_quotes
from donmarket.backtest.runner import (
    MAX_PER_EVENT,
    MIN_REPLAYS,
    BacktestReport,
    MarketReplay,
    diversified_head,
    event_key,
)

SIZE = 100.0
HALF_SPREAD = 0.01


class TestNothingHappens:
    def test_flat_price_never_fills(self):
        """Cote 0,49/0,51 sur un prix collé à 0,50 : rien ne touche."""
        result = replay_quotes([0.50] * 5, half_spread=HALF_SPREAD, size=SIZE)

        assert result.buys == 0
        assert result.sells == 0
        assert result.total_pnl == pytest.approx(0.0)

    def test_a_single_price_cannot_fill(self):
        """Il faut un prix pour coter et le suivant pour savoir s'il touche."""
        result = replay_quotes([0.50], half_spread=HALF_SPREAD, size=SIZE)

        assert result.steps == 1
        assert result.total_pnl == pytest.approx(0.0)

    def test_empty_series_is_not_a_crash(self):
        result = replay_quotes([], half_spread=HALF_SPREAD, size=SIZE)

        assert result.steps == 0
        assert result.final_price == 0.0

    def test_zero_size_engages_nothing(self):
        result = replay_quotes([0.50, 0.60], half_spread=HALF_SPREAD, size=0.0)

        assert result.engaged_usd == 0.0
        assert result.pnl_pct == 0.0


class TestAdverseSelection:
    def test_requoting_after_a_fill_turns_a_winner_into_a_loser(self):
        """Chemin 0,50 → 0,53 → 0,50, cote ±0,01 sur 100 parts.

        Pas 1 : coté 0,49/0,51 autour de 0,50 ; le prix monte à 0,53, notre
        vente part à 0,51. Inventaire −100, caisse +51.
        Pas 2 : recoté 0,52/0,54 autour de 0,53 ; le prix retombe à 0,50, notre
        achat part à 0,52. Inventaire 0, caisse 51 − 52 = −1.

        Un aller-retour complet, et il perd 1 $ — alors que la fourchette
        théorique promettait +2 $. C'est ça, recoter autour du prix qui vient
        de vous remplir.
        """
        result = replay_quotes([0.50, 0.53, 0.50], half_spread=HALF_SPREAD, size=SIZE)

        assert (result.buys, result.sells) == (1, 1)
        assert result.round_trips == 1
        assert result.total_pnl == pytest.approx(-1.0)
        assert result.spread_capture == pytest.approx(2.0)
        assert result.inventory_cost == pytest.approx(-3.0)

    def test_a_trend_loads_one_side_until_the_cap(self):
        """Chemin 0,50 → 0,52 → 0,54 → 0,56 → 0,58, plafond 2 × 100 parts.

        Vendu à 0,51 puis à 0,53 : inventaire −200, caisse +104. Le plafond est
        atteint, la vente n'est plus postée, les deux derniers pas ne font
        rien. Valorisé à 0,58 : 104 − 200 × 0,58 = −12 $.

        Le plafond est ce qui sépare une perte de 12 $ d'une perte sans fond :
        sans lui, le rejeu afficherait un désastre que personne n'aurait subi.
        """
        result = replay_quotes(
            [0.50, 0.52, 0.54, 0.56, 0.58],
            half_spread=HALF_SPREAD,
            size=SIZE,
            max_inventory=2.0,
        )

        assert (result.buys, result.sells) == (0, 2)
        assert result.inventory == pytest.approx(-200.0)
        assert result.total_pnl == pytest.approx(-12.0)
        assert result.round_trips == 0
        assert result.spread_capture == pytest.approx(0.0)
        assert result.pnl_pct == pytest.approx(-12.0)

    def test_the_cap_still_allows_unwinding(self):
        """Au plafond, le côté qui DÉBOUCLE doit rester posté.

        Même chemin haussier jusqu'au plafond, puis le prix redescend à 0,50 :
        l'achat doit partir. Bloquer les deux côtés au plafond ferait porter la
        position jusqu'à la fin de la série sans jamais la déboucler.
        """
        result = replay_quotes(
            [0.50, 0.52, 0.54, 0.50],
            half_spread=HALF_SPREAD,
            size=SIZE,
            max_inventory=2.0,
        )

        assert result.buys == 1
        assert result.inventory == pytest.approx(-100.0)


class TestMeanReversion:
    def test_a_reverting_path_pays_but_less_than_the_spread(self):
        """Chemin 0,50 → 0,51 → 0,50 → 0,49.

        Vendu à 0,51 (caisse +51, inv −100), racheté à 0,50 (caisse +1, inv 0),
        puis acheté à 0,49 (caisse −48, inv +100). Valorisé à 0,49 :
        −48 + 49 = +1 $.

        Un aller-retour rapporte 1 $ là où la fourchette en promettait 2 : même
        sur un chemin favorable, le recotage en rend la moitié.
        """
        result = replay_quotes(
            [0.50, 0.51, 0.50, 0.49], half_spread=HALF_SPREAD, size=SIZE
        )

        assert (result.buys, result.sells) == (2, 1)
        assert result.total_pnl == pytest.approx(1.0)
        assert result.spread_capture == pytest.approx(2.0)
        assert result.inventory_cost == pytest.approx(-1.0)


class TestUnits:
    def test_pnl_is_a_share_of_the_complete_set_not_of_the_price(self):
        """Piège n° 9 du README : l'unité est le jeu complet à 1 $.

        100 parts cotées engagent 100 $, pas 100 × le prix moyen. Rapporter le
        résultat au prix (≈ 0,50 $) doublerait le pourcentage affiché et le
        rendrait incomparable au rendement de `analysis/rewards`.
        """
        result = replay_quotes([0.50, 0.53, 0.50], half_spread=HALF_SPREAD, size=SIZE)

        assert result.engaged_usd == pytest.approx(100.0)
        assert result.pnl_pct == pytest.approx(-1.0)

    def test_result_is_immutable(self):
        result = replay_quotes([0.50, 0.51], half_spread=HALF_SPREAD, size=SIZE)

        with pytest.raises(Exception):
            result.cash = 999.0  # type: ignore[misc]

    def test_is_a_replay_result(self):
        assert isinstance(
            replay_quotes([0.5, 0.5], half_spread=HALF_SPREAD, size=SIZE),
            ReplayResult,
        )


def _market(slug: str, question: str = "?"):
    """Un marché réduit à ce que le regroupement par événement regarde."""
    raw = {
        "conditionId": slug or "0xsansslug",
        "question": question,
        "slug": slug,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["1", "2"]',
        "outcomePrices": '["0.50", "0.50"]',
        "volume24hr": 1.0,
        "orderMinSize": 5,
        "endDate": "2027-01-01T00:00:00Z",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "clobRewards": [{"rewardsDailyRate": 100.0}],
        "rewardsMinSize": 50.0,
        "rewardsMaxSpread": 3.0,
    }
    from donmarket.model import parse_gamma_market

    market = parse_gamma_market(raw)
    assert market is not None
    return market


class TestEventKey:
    def test_two_dates_of_the_same_series_share_a_key(self):
        """Le cas qui a invalidé le premier rejeu réel.

        Trois des quatre marchés survivants étaient le même cessez-le-feu
        Israël/Iran à des échéances différentes. Ils doivent se regrouper.
        """
        august = _market("israel-x-iran-ceasefire-continues-through-august-31")
        september = _market("israel-x-iran-ceasefire-continues-through-september-30")

        assert event_key(august) == event_key(september)

    def test_a_different_series_keeps_its_own_key(self):
        """Regrouper trop serait aussi une erreur : on perdrait des observations."""
        israel = _market("israel-x-iran-ceasefire-continues-through-august-31")
        united_states = _market("us-x-iran-effective-ceasefire-by-august-31")

        assert event_key(israel) != event_key(united_states)

    def test_a_missing_slug_falls_back_on_the_question(self):
        market = _market("", question="Will BTC close above 100k?")

        assert event_key(market) == "will-btc-close-above"


class TestDiversifiedHead:
    def test_a_single_event_cannot_fill_the_sample(self):
        """Dix marchés du même événement, plafond 2 : il en reste 2.

        C'est tout l'objet du plafond. Sans lui, `alive[:60]` trié par pool
        laisse l'actualité du jour occuper la tête et la médiane mesure une
        dépêche.
        """
        markets = [_market(f"israel-x-iran-ceasefire-v{i}") for i in range(10)]

        head = diversified_head(markets, limit=60)

        assert len(head) == MAX_PER_EVENT

    def test_order_is_preserved_so_the_biggest_pools_survive(self):
        """L'entrée est déjà triée par pool : on garde les premiers de chaque groupe."""
        markets = [
            _market("aaa-bbb-ccc-ddd-1"),
            _market("aaa-bbb-ccc-ddd-2"),
            _market("aaa-bbb-ccc-ddd-3"),
            _market("eee-fff-ggg-hhh-1"),
        ]

        head = diversified_head(markets, limit=60)

        assert [m.slug for m in head] == [
            "aaa-bbb-ccc-ddd-1",
            "aaa-bbb-ccc-ddd-2",
            "eee-fff-ggg-hhh-1",
        ]

    def test_the_limit_still_caps_a_diverse_universe(self):
        markets = [_market(f"event-{i}-alpha-beta") for i in range(30)]

        assert len(diversified_head(markets, limit=5)) == 5

    def test_an_empty_universe_is_not_a_crash(self):
        assert diversified_head([], limit=10) == []


def _replay(event: str, assumed: float = 0.0, realized: float = 0.0) -> MarketReplay:
    """Un rejeu réduit aux champs que le rapport agrège."""
    result = replay_quotes([0.50, 0.50], half_spread=HALF_SPREAD, size=SIZE)
    stub = MarketReplay(
        condition_id=event,
        question=event,
        event_key=event,
        half_spread=HALF_SPREAD,
        size=SIZE,
        points=2,
        result=result,
        assumed_pnl_pct=assumed,
        oscillation_pct=0.0,
    )
    # `realized_pnl_pct` dérive de `result` : on le règle par le résultat.
    return replace(
        stub,
        result=replace(
            result, total_pnl=realized / 100.0 * SIZE, engaged_usd=SIZE
        ),
    )


def _report(replays, *, requested: int) -> BacktestReport:
    return BacktestReport(
        markets_seen=2000,
        rewarded=600,
        histories_fetched=len(replays),
        histories_requested=requested,
        duration_seconds=1.0,
        replays=tuple(replays),
    )


class TestSampleComplaints:
    """Le rapport doit savoir dire qu'il est invalide.

    Le premier rejeu réel a rendu 4 marchés sur 60 demandés, dont 3 du même
    événement, et l'a présenté exactement comme il aurait présenté un résultat
    valide. C'est ce silence-là qui est corrigé ici.
    """

    def test_an_amputated_sample_is_reported(self):
        report = _report([_replay(f"e{i}") for i in range(4)], requested=60)

        assert not report.is_readable
        assert any("4/60" in complaint for complaint in report.sample_complaints)

    def test_a_sample_too_small_is_reported(self):
        few = MIN_REPLAYS - 1
        report = _report([_replay(f"e{i}") for i in range(few)], requested=few)

        assert any(str(few) in complaint for complaint in report.sample_complaints)

    def test_correlated_observations_are_reported(self):
        """20 marchés pour 2 événements : n = 20 est un mensonge."""
        replays = [_replay("guerre") for _ in range(10)]
        replays += [_replay("election") for _ in range(10)]
        report = _report(replays, requested=20)

        assert report.events_covered == 2
        assert any(
            "indépendantes" in complaint for complaint in report.sample_complaints
        )

    def test_a_healthy_sample_raises_nothing(self):
        replays = [_replay(f"evenement-{i}") for i in range(MIN_REPLAYS + 8)]
        report = _report(replays, requested=len(replays))

        assert report.sample_complaints == ()
        assert report.is_readable

    def test_coverage_without_a_request_is_zero_not_a_crash(self):
        report = BacktestReport(
            markets_seen=0, rewarded=0, histories_fetched=0, duration_seconds=0.0
        )

        assert report.coverage == 0.0
        assert report.events_covered == 0
