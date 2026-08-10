"""Le compte de démonstration : ce qu'il crédite, et surtout ce qu'il refuse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from donmarket.api.clob import Book, Level
from donmarket.paper.fills import (
    MarketTrade,
    RestingOrder,
    apply_trade,
    fill_against_trade,
    queue_ahead,
)
from donmarket.paper.ledger import (
    InsufficientCash,
    PaperAccount,
    PaperFill,
    Position,
)
from donmarket.paper.requote import RequotePolicy
from donmarket.paper.session import PaperMarket, PaperSession

NOW = datetime(2026, 8, 1, 22, 0, tzinfo=timezone.utc)


def book(bids: list[tuple[float, float]]) -> Book:
    return Book(
        token_id="T",
        bids=tuple(Level(p, s) for p, s in bids),
        asks=(Level(0.60, 100.0),),
        tick_size=0.001,
        min_order_size=5.0,
    )


def trade(price: float, size: float, side: str = "SELL") -> MarketTrade:
    return MarketTrade(
        token_id="T", price=price, size=size, taker_side=side, traded_at=NOW
    )


# --- le registre ---------------------------------------------------------


def test_opening_account_holds_everything_in_cash():
    account = PaperAccount.opening(10_000.0)

    assert account.cash_usd == 10_000.0
    assert account.positions == ()
    assert account.equity({}) == 10_000.0
    assert account.pnl({}) == 0.0


def test_opening_refuses_a_non_positive_capital():
    with pytest.raises(ValueError):
        PaperAccount.opening(0.0)


def test_a_fill_moves_cash_into_inventory_without_creating_a_loss():
    """Acheter au prix du marché ne doit RIEN changer au solde.

    C'est le garde-fou central du registre : si un simple achat faisait bouger
    le solde, toute la mesure de gain serait décalée dès le premier ordre.
    """
    account = PaperAccount.opening(1_000.0)

    after = account.with_fill(PaperFill("T", 0.45, 100.0, NOW))

    assert after.cash_usd == pytest.approx(955.0)
    assert after.invested_usd == pytest.approx(45.0)
    assert after.equity({"T": 0.45}) == pytest.approx(1_000.0)
    assert after.pnl({"T": 0.45}) == pytest.approx(0.0)


def test_the_balance_moves_only_when_the_price_moves():
    account = PaperAccount.opening(1_000.0).with_fill(PaperFill("T", 0.45, 100.0, NOW))

    assert account.pnl({"T": 0.55}) == pytest.approx(10.0)
    assert account.pnl({"T": 0.35}) == pytest.approx(-10.0)


def test_two_fills_on_one_token_average_the_cost():
    account = (
        PaperAccount.opening(1_000.0)
        .with_fill(PaperFill("T", 0.40, 100.0, NOW))
        .with_fill(PaperFill("T", 0.60, 100.0, NOW))
    )
    held = account.position("T")

    assert held is not None
    assert held.shares == pytest.approx(200.0)
    assert held.average_price == pytest.approx(0.50)


def test_a_missing_mark_values_inventory_at_cost_not_at_zero():
    """Une cotation absente est une ignorance, pas une perte totale."""
    account = PaperAccount.opening(1_000.0).with_fill(PaperFill("T", 0.45, 100.0, NOW))

    assert account.equity({}) == pytest.approx(1_000.0)


def test_a_fill_beyond_available_cash_is_refused():
    account = PaperAccount.opening(10.0)

    with pytest.raises(InsufficientCash):
        account.with_fill(PaperFill("T", 0.50, 100.0, NOW))


def test_rewards_are_the_only_net_inflow():
    account = PaperAccount.opening(1_000.0).with_reward(3.25)

    assert account.cash_usd == pytest.approx(1_003.25)
    assert account.rewards_usd == pytest.approx(3.25)
    assert account.pnl({}) == pytest.approx(3.25)


def test_rewards_are_not_counted_twice_in_equity():
    """Le crédit entre dans le cash ; l'ajouter encore le doublerait."""
    account = PaperAccount.opening(1_000.0).with_reward(10.0)

    assert account.equity({}) == pytest.approx(1_010.0)


def test_the_account_never_mutates():
    account = PaperAccount.opening(1_000.0)

    account.with_fill(PaperFill("T", 0.45, 100.0, NOW))
    account.with_reward(50.0)

    assert account.cash_usd == 1_000.0
    assert account.positions == ()


def test_a_position_without_shares_reports_no_average_price():
    assert Position("T", 0.0, 0.0).average_price == 0.0


# --- le modèle de remplissage -------------------------------------------


def test_queue_ahead_counts_our_own_level_because_we_arrive_last():
    """Un `>` strict au lieu de `>=` nous inventerait de l'ancienneté."""
    depth = queue_ahead(book([(0.50, 30.0), (0.45, 80.0), (0.40, 10.0)]), 0.45)

    assert depth == pytest.approx(110.0)


def test_a_buyer_taker_never_fills_our_resting_buy():
    """Compter toutes les exécutions doublerait les remplissages."""
    order = RestingOrder("T", 0.45, 100.0)

    assert fill_against_trade(order, trade(0.45, 500.0, "BUY"), ahead=0.0) == 0.0


def test_a_seller_above_our_price_never_reaches_us():
    order = RestingOrder("T", 0.45, 100.0)

    assert fill_against_trade(order, trade(0.50, 500.0), ahead=0.0) == 0.0


def test_the_queue_absorbs_the_trade_before_we_get_anything():
    order = RestingOrder("T", 0.45, 100.0)

    assert fill_against_trade(order, trade(0.45, 40.0), ahead=120.0) == 0.0


def test_we_only_take_what_overflows_the_queue():
    order = RestingOrder("T", 0.45, 100.0)

    assert fill_against_trade(order, trade(0.45, 150.0), ahead=120.0) == pytest.approx(
        30.0
    )


def test_a_fill_never_exceeds_what_we_still_want():
    order = RestingOrder("T", 0.45, 100.0, filled=90.0)

    assert fill_against_trade(order, trade(0.40, 10_000.0), ahead=0.0) == pytest.approx(
        10.0
    )


def test_a_completed_order_takes_nothing_more():
    order = RestingOrder("T", 0.45, 100.0, filled=100.0)

    assert fill_against_trade(order, trade(0.40, 500.0), ahead=0.0) == 0.0


def test_a_trade_on_another_token_is_ignored():
    order = RestingOrder("OTHER", 0.45, 100.0)

    assert fill_against_trade(order, trade(0.40, 500.0), ahead=0.0) == 0.0


def test_apply_trade_fills_at_our_limit_not_at_the_takers_price():
    """Un ordre à cours limité ne profite jamais d'une amélioration passive."""
    order = RestingOrder("T", 0.45, 100.0)

    _, fill = apply_trade(order, trade(0.30, 200.0), book([(0.45, 50.0)]))

    assert fill is not None
    assert fill.price == pytest.approx(0.45)
    assert fill.size == pytest.approx(100.0)


def test_apply_trade_without_a_book_fills_nothing():
    """Une donnée manquante ne doit jamais se transformer en profit."""
    order = RestingOrder("T", 0.45, 100.0)

    after, fill = apply_trade(order, trade(0.40, 10_000.0), None)

    assert fill is None
    assert after.filled == 0.0


def test_a_partial_fill_leaves_the_rest_resting():
    order = RestingOrder("T", 0.45, 100.0)

    after, fill = apply_trade(order, trade(0.45, 130.0), book([(0.45, 90.0)]))

    assert fill is not None
    assert fill.size == pytest.approx(40.0)
    assert after.remaining == pytest.approx(60.0)
    assert not after.is_complete


def test_fills_accumulate_into_the_account_end_to_end():
    """Le chemin complet : exécution du marché → remplissage → solde."""
    account = PaperAccount.opening(1_000.0)
    order = RestingOrder("T", 0.45, 100.0)
    later = MarketTrade("T", 0.44, 200.0, "SELL", NOW + timedelta(minutes=1))

    _, fill = apply_trade(order, later, book([(0.45, 50.0)]))
    assert fill is not None
    account = account.with_fill(fill)

    assert account.position("T").shares == pytest.approx(100.0)
    assert account.cash_usd == pytest.approx(955.0)
    assert account.pnl({"T": 0.50}) == pytest.approx(5.0)


# --- la récompense de la session -----------------------------------------
#
# Ces tests portent tous sur le même point : la récompense doit rémunérer LES
# ORDRES QUI DORMENT SUR LE CARNET, à leur prix et pour les parts qui leur
# restent. Un modèle qui recalcule à chaque tour ce que vaudrait un ordre
# FRAIS, reposté au prix idéal du milieu courant, décrit un teneur qui
# annulerait et reposterait sans cesse, gratuitement, sans jamais se faire
# remplir. Il crédite alors des dollars dans trois cas où le carnet réel ne
# paie rien : ordre entièrement servi, milieu qui s'éloigne, ordre raboté par
# un remplissage partiel. Les trois penchent du même côté — celui qui flatte.

MARKET_TOKENS = ("YES", "NO")
MAX_SPREAD = 0.06
MIN_SIZE = 100.0
DAILY_POOL = 100.0


def branch(token_id: str, mid: float, depth: float = 500.0) -> Book:
    """Un carnet symétrique de 2 cents autour de `mid`."""
    return Book(
        token_id=token_id,
        bids=(Level(round(mid - 0.02, 4), depth),),
        asks=(Level(round(mid + 0.02, 4), depth),),
        tick_size=0.001,
        min_order_size=5.0,
    )


def books_at(mid_yes: float, depth: float = 500.0) -> dict[str, Book]:
    return {
        "YES": branch("YES", mid_yes, depth),
        "NO": branch("NO", round(1.0 - mid_yes, 4), depth),
    }


def market() -> PaperMarket:
    return PaperMarket(
        condition_id="C",
        question="Un marché de démonstration",
        token_ids=MARKET_TOKENS,
        max_spread=MAX_SPREAD,
        daily_pool=DAILY_POOL,
        min_size=MIN_SIZE,
    )


def session(orders: list[RestingOrder], bankroll: float = 1_000.0) -> PaperSession:
    return PaperSession.opening(
        bankroll=bankroll, markets=[market()], orders=orders
    )


def both_branches_resting(filled: float = 0.0) -> list[RestingOrder]:
    """Nos deux achats, postés à 3 cents sous un milieu de 0,50."""
    return [
        RestingOrder("YES", 0.47, MIN_SIZE, filled=filled),
        RestingOrder("NO", 0.47, MIN_SIZE, filled=filled),
    ]


def test_two_resting_orders_earn_their_share_of_the_pool():
    """Le cas nominal, pour que les cas dégradés aient un point de comparaison."""
    earned = session(both_branches_resting()).reward_usd(books_at(0.50), 86_400.0)

    # Notre score vaut 25 points par branche contre 444 déjà postés : environ
    # 5 % du pool. Le chiffre exact importe moins que son ordre de grandeur —
    # ce qui compte est qu'il soit strictement positif et loin du pool entier.
    assert 0.0 < earned < DAILY_POOL
    assert earned == pytest.approx(5.33, abs=0.2)


def test_a_fully_served_order_stops_earning():
    """Un ordre servi a quitté le carnet : il ne rapporte plus rien.

    C'est le remplissage qui met fin à la récompense, et c'est exactement ce
    qu'un modèle d'ordre frais ne peut pas voir — il repostait à chaque tour un
    ordre que le marché avait déjà consommé.
    """
    served = session(both_branches_resting(filled=MIN_SIZE))

    assert served.reward_usd(books_at(0.50), 86_400.0) == pytest.approx(0.0)


def test_one_branch_served_leaves_the_other_penalised():
    """Une seule branche debout, c'est de la liquidité unilatérale.

    Polymarket la divise par trois dans la plage centrale. La session doit
    subir cette division comme n'importe quel teneur, au lieu de continuer à
    compter deux branches dont une n'existe plus.
    """
    two_sided = session(both_branches_resting())
    one_sided = session(
        [
            RestingOrder("YES", 0.47, MIN_SIZE, filled=MIN_SIZE),
            RestingOrder("NO", 0.47, MIN_SIZE),
        ]
    )

    full = two_sided.reward_usd(books_at(0.50), 86_400.0)
    half = one_sided.reward_usd(books_at(0.50), 86_400.0)

    assert 0.0 < half < full
    # La part n'est pas linéaire dans le score (elle se dilue), donc on vérifie
    # l'encadrement plutôt qu'un tiers exact.
    assert half < full / 2.0


def test_the_reward_follows_our_posted_price_when_the_middle_drifts():
    """Le milieu s'éloigne, notre ordre sort de la bande, la récompense cesse.

    Notre ordre est posté à 0,47 pour un milieu de 0,50. Quand le milieu monte
    à 0,60, sa distance passe à 13 cents pour une bande de 6 : score nul. Un
    modèle qui reposte au prix idéal du moment ne verrait jamais cette sortie
    et continuerait de créditer le même montant qu'à l'immobile.
    """
    live = session(both_branches_resting())

    assert live.reward_usd(books_at(0.50), 86_400.0) > 0.0
    assert live.reward_usd(books_at(0.60), 86_400.0) == pytest.approx(0.0)


def test_a_partial_fill_reduces_what_the_order_earns():
    """Moins de parts posées, moins de points marqués."""
    whole = session([RestingOrder("YES", 0.47, 400.0), RestingOrder("NO", 0.47, 400.0)])
    rabotted = session(
        [
            RestingOrder("YES", 0.47, 400.0, filled=250.0),
            RestingOrder("NO", 0.47, 400.0, filled=250.0),
        ]
    )

    assert rabotted.reward_usd(books_at(0.50), 86_400.0) < whole.reward_usd(
        books_at(0.50), 86_400.0
    )


def test_an_order_below_the_minimum_size_earns_nothing():
    """`rewardsMinSize` est un seuil de qualification, pas une préférence.

    Un ordre raboté sous ce seuil reste sur le carnet mais cesse d'être compté
    par Polymarket. Règle appliquée à NOS ordres seulement : le carnet public
    agrège des paliers, pas des ordres, donc la même règle est inapplicable à
    la concurrence. L'asymétrie nous sous-estime — c'est le sens acceptable.
    """
    crumb = session(
        [
            RestingOrder("YES", 0.47, MIN_SIZE, filled=MIN_SIZE - 1.0),
            RestingOrder("NO", 0.47, MIN_SIZE, filled=MIN_SIZE - 1.0),
        ]
    )

    assert crumb.reward_usd(books_at(0.50), 86_400.0) == pytest.approx(0.0)


def test_an_unreadable_book_pays_nothing_rather_than_crashing():
    """Une branche sans ask n'a pas de milieu : on ne sait pas lire, on ne paie pas."""
    blind = dict(books_at(0.50))
    blind["NO"] = Book(
        token_id="NO",
        bids=(Level(0.48, 500.0),),
        asks=(),
        tick_size=0.001,
        min_order_size=5.0,
    )

    assert session(both_branches_resting()).reward_usd(blind, 86_400.0) == 0.0


def test_a_market_without_any_of_our_orders_earns_nothing():
    """Le pool ne se distribue pas à qui ne poste pas."""
    absent = session([])

    assert absent.reward_usd(books_at(0.50), 86_400.0) == pytest.approx(0.0)


def test_the_reward_is_proportional_to_the_time_spent_quoting():
    live = session(both_branches_resting())

    hour = live.reward_usd(books_at(0.50), 3_600.0)
    day = live.reward_usd(books_at(0.50), 86_400.0)

    assert day == pytest.approx(hour * 24.0)


def test_no_time_elapsed_pays_nothing():
    live = session(both_branches_resting())

    assert live.reward_usd(books_at(0.50), 0.0) == 0.0


# --- la recotation ---------------------------------------------------------


def test_without_a_policy_the_quote_never_moves():
    """Le défaut reste la cote figée : recoter est une décision, pas un réglage."""
    live = session(both_branches_resting())

    after = live.with_requotes(books_at(0.60))

    assert after.orders == live.orders
    assert after.requotes == 0


def test_a_distanced_quote_is_reposted_at_the_new_middle():
    live = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(),
        policy=RequotePolicy(),
    )

    after = live.with_requotes(books_at(0.60))

    assert after.requotes > 0
    moved = {o.token_id: o.price for o in after.orders}
    # Le milieu de YES est passé à 0,60 : la nouvelle cote doit s'en approcher,
    # sans jamais croiser le meilleur ask (0,62).
    assert 0.47 < moved["YES"] < 0.62


def test_requoting_restores_the_reward_that_drift_had_killed():
    """Le point de tout le module, en une assertion."""
    drifted = books_at(0.60)
    frozen = session(both_branches_resting())
    chasing = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(),
        policy=RequotePolicy(),
    ).with_requotes(drifted)

    assert frozen.reward_usd(drifted, 86_400.0) == pytest.approx(0.0)
    assert chasing.reward_usd(drifted, 86_400.0) > 0.0


def test_a_quote_still_close_enough_is_left_alone():
    """Recoter pour un dixième de cent, c'est poursuivre le prix pour rien."""
    live = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(),
        policy=RequotePolicy(),
    )

    after = live.with_requotes(books_at(0.50))

    assert after.requotes == 0


def test_a_served_order_is_never_reposted():
    """Rien ne reste à obtenir : reposer serait rouvrir une position close."""
    served = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(filled=MIN_SIZE),
        policy=RequotePolicy(),
    )

    assert served.with_requotes(books_at(0.60)).requotes == 0


def test_requoting_carries_over_only_the_shares_still_wanted():
    """Un ordre à moitié servi se reposte pour sa moitié, pas pour son tout."""
    half = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=[
            RestingOrder("YES", 0.47, 400.0, filled=250.0),
            RestingOrder("NO", 0.47, 400.0, filled=250.0),
        ],
        policy=RequotePolicy(),
    )

    after = half.with_requotes(books_at(0.60))
    yes = next(o for o in after.orders if o.token_id == "YES")

    assert yes.size == pytest.approx(150.0)
    assert yes.filled == pytest.approx(0.0)


def test_a_disabled_policy_behaves_exactly_like_no_policy():
    """La branche témoin de l'A/B doit être un vrai témoin."""
    frozen = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(),
        policy=RequotePolicy(enabled=False),
    )

    assert frozen.with_requotes(books_at(0.60)).orders == frozen.orders


def test_an_unreadable_book_leaves_the_quote_where_it_is():
    """On n'agit pas sur une lecture incertaine."""
    blind = dict(books_at(0.60))
    blind["YES"] = Book(
        token_id="YES",
        bids=(Level(0.58, 500.0),),
        asks=(),
        tick_size=0.001,
        min_order_size=5.0,
    )
    live = PaperSession.opening(
        bankroll=1_000.0,
        markets=[market()],
        orders=both_branches_resting(),
        policy=RequotePolicy(),
    )

    after = live.with_requotes(blind)
    yes = next(o for o in after.orders if o.token_id == "YES")

    assert yes.price == pytest.approx(0.47)


def test_a_crowded_book_dilutes_our_share():
    """Deux fois plus de concurrence, nettement moins de dollars."""
    live = session(both_branches_resting())

    thin = live.reward_usd(books_at(0.50, depth=200.0), 86_400.0)
    crowded = live.reward_usd(books_at(0.50, depth=2_000.0), 86_400.0)

    assert crowded < thin
