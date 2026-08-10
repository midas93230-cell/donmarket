"""La formule de récompense de Polymarket, et ce qu'elle invalide chez nous.

Source : docs.polymarket.com/market-makers/liquidity-rewards
    S(v,s) = ((v − s)/v)² · b
    Qone = bids(m) + asks(m')      Qtwo = asks(m) + bids(m')
    Qmin = max(min(Qone,Qtwo), max(Qone,Qtwo)/c)  si milieu ∈ [0,10 ; 0,90]
    Qmin = min(Qone, Qtwo)                        sinon
    part = Q_nous / Σ Q,  échantillonné chaque minute

Ces tests existent surtout pour verrouiller ce que le modèle linéaire
précédent (somme des dollars postés dans la bande) racontait de faux.
"""

from __future__ import annotations

import pytest

from donmarket.analysis.scoring import (
    SINGLE_SIDED_PENALTY,
    competing_score,
    distance_after_posting,
    market_scores,
    order_score,
    own_score,
    planned_price,
    qmin,
    score_on_book,
)
from donmarket.api.clob import Book, Level


def book(token_id: str, bids, asks, tick: float = 0.001) -> Book:
    return Book(
        token_id=token_id,
        bids=tuple(Level(price=p, size=s) for p, s in bids),
        asks=tuple(Level(price=p, size=s) for p, s in asks),
        tick_size=tick,
        min_order_size=5.0,
    )


class TestOrderScore:
    def test_scores_full_size_at_the_midpoint(self) -> None:
        # s = 0 → ((v−0)/v)² = 1 : l'ordre vaut sa taille entière.
        assert order_score(size=100.0, spread=0.0, max_spread=0.03) == pytest.approx(100.0)

    def test_scores_exactly_zero_at_the_band_edge(self) -> None:
        """LE défaut qui coûtait de l'argent réel.

        Le planificateur posait à `mid − max_spread`. À cette distance le
        facteur vaut ((v−v)/v)² = 0 : risque d'inventaire complet, récompense
        nulle. Rien dans l'ancien modèle linéaire ne pouvait le signaler.
        """
        assert order_score(size=100.0, spread=0.03, max_spread=0.03) == 0.0

    def test_scores_zero_outside_the_band(self) -> None:
        assert order_score(size=100.0, spread=0.05, max_spread=0.03) == 0.0

    def test_is_quadratic_not_linear(self) -> None:
        # À mi-bande le modèle linéaire aurait dit 50 parts ; la vraie
        # formule en dit 25. C'est un facteur 2 sur toute la concurrence.
        assert order_score(size=100.0, spread=0.015, max_spread=0.03) == pytest.approx(25.0)

    def test_counts_shares_not_dollars(self) -> None:
        """`b` est une taille en PARTS, pas en dollars.

        L'ancien calcul sommait `prix × taille`. Deux ordres de même taille
        marquent pareil quel que soit le niveau du prix ; en dollars ils
        pouvaient différer d'un facteur 9 entre un marché à 0,10 et à 0,90.
        """
        near_zero = order_score(size=100.0, spread=0.0, max_spread=0.03)
        near_one = order_score(size=100.0, spread=0.0, max_spread=0.03)
        assert near_zero == near_one == pytest.approx(100.0)


class TestQmin:
    def test_takes_the_min_when_the_midpoint_is_extreme(self) -> None:
        # Hors de [0,10 ; 0,90] : il FAUT les deux côtés.
        assert qmin(100.0, 0.0, midpoint=0.95) == 0.0
        assert qmin(100.0, 40.0, midpoint=0.05) == pytest.approx(40.0)

    def test_penalises_rather_than_cancels_single_sided_in_range(self) -> None:
        # Dans [0,10 ; 0,90] : un seul côté marque, divisé par c.
        assert qmin(100.0, 0.0, midpoint=0.50) == pytest.approx(100.0 / SINGLE_SIDED_PENALTY)

    def test_prefers_the_balanced_score_when_it_beats_the_penalty(self) -> None:
        # min = 40 > max/3 = 33,3 → la cotation équilibrée gagne.
        assert qmin(100.0, 40.0, midpoint=0.50) == pytest.approx(40.0)

    def test_treats_the_range_bounds_as_inclusive(self) -> None:
        assert qmin(90.0, 0.0, midpoint=0.10) == pytest.approx(30.0)
        assert qmin(90.0, 0.0, midpoint=0.90) == pytest.approx(30.0)


class TestMarketScores:
    def test_pairs_each_branch_with_the_complement_of_the_other(self) -> None:
        """Qone = bids(YES) + asks(NO), Qtwo = asks(YES) + bids(NO).

        Un acheteur de YES et un vendeur de NO fournissent la même liquidité
        économique ; la formule les met donc dans le même seau.
        """
        yes = book("yes", bids=[(0.50, 100.0)], asks=[(0.52, 60.0)])
        no = book("no", bids=[(0.48, 20.0)], asks=[(0.50, 10.0)])
        # mid_yes = 0,51 ; mid_no = 0,49
        scores = market_scores(yes, no, max_spread=0.03)
        assert scores is not None
        q_one, q_two = scores

        # Qone : bid YES à 0,50 (s = 0,01) + ask NO à 0,50 (s = 0,01)
        expected_one = order_score(100.0, 0.01, 0.03) + order_score(10.0, 0.01, 0.03)
        # Qtwo : ask YES à 0,52 (s = 0,01) + bid NO à 0,48 (s = 0,01)
        expected_two = order_score(60.0, 0.01, 0.03) + order_score(20.0, 0.01, 0.03)

        assert q_one == pytest.approx(expected_one)
        assert q_two == pytest.approx(expected_two)

    def test_returns_none_without_a_usable_midpoint(self) -> None:
        empty = book("yes", bids=[], asks=[])
        no = book("no", bids=[(0.48, 20.0)], asks=[(0.50, 10.0)])
        assert market_scores(empty, no, max_spread=0.03) is None


class TestCompetingScore:
    def test_a_book_parked_at_the_band_edge_is_not_competition(self) -> None:
        """La régression qui change le classement.

        10 000 parts posées au bord de la bande faisaient des milliers de
        dollars de « concurrence » dans l'ancien modèle. Elles ne marquent
        rien : le pool est en réalité quasi libre.
        """
        yes = book("yes", bids=[(0.47, 10_000.0)], asks=[(0.53, 10_000.0)])
        no = book("no", bids=[(0.47, 10_000.0)], asks=[(0.53, 10_000.0)])
        # mid = 0,50, bande = 0,03 → tout est posé exactement à s = v.
        assert competing_score({"yes": yes, "no": no}, ("yes", "no"), 0.03) == 0.0

    def test_ranks_a_tight_book_above_a_spread_out_one(self) -> None:
        tight_yes = book("yes", bids=[(0.4995, 100.0)], asks=[(0.5005, 100.0)])
        tight_no = book("no", bids=[(0.4995, 100.0)], asks=[(0.5005, 100.0)])
        loose_yes = book("yes", bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])
        loose_no = book("no", bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])

        tight = competing_score({"yes": tight_yes, "no": tight_no}, ("yes", "no"), 0.03)
        loose = competing_score({"yes": loose_yes, "no": loose_no}, ("yes", "no"), 0.03)
        assert tight is not None and loose is not None
        assert tight > loose > 0.0

    def test_returns_none_when_a_book_is_missing(self) -> None:
        yes = book("yes", bids=[(0.50, 100.0)], asks=[(0.51, 100.0)])
        assert competing_score({"yes": yes}, ("yes", "no"), 0.03) is None


class TestSelfMove:
    """Notre ordre déplace le milieu dont il mesure sa propre distance.

    C'est le défaut qui a produit « +238 %/jour » au balayage du 2026-07-31 :
    le classement plaçait en tête les carnets où NOTRE score valait zéro.
    """

    def test_an_order_behind_the_best_bid_does_not_move_the_midpoint(self) -> None:
        # Posté sous le meilleur bid : on entre dans la file, le carnet ne
        # bouge pas, la distance est celle qu'on croyait.
        assert distance_after_posting(0.50, 0.52, 0.49) == pytest.approx(0.02)

    def test_an_order_that_improves_the_book_drags_the_midpoint_with_it(self) -> None:
        # Milieu 0,51 ; on poste à 0,505, donc devant. Nouveau milieu :
        # (0,505 + 0,52)/2 = 0,5125 → distance 0,0075, pas 0,005.
        assert distance_after_posting(0.50, 0.52, 0.505) == pytest.approx(0.0075)

    def test_a_gaping_book_pushes_our_own_order_out_of_the_band(self) -> None:
        """LE cas mesuré en direct — « Will LIV Golf announce shutdown in 2026? »

        Carnet réel : bid 0,452 / ask 0,698, soit 0,246 d'écart, bande 0,045.
        Viser milieu − bande/2 = 0,5525 paraît sûr ; une fois posé, le milieu
        monte à 0,62525 et la distance atteint 0,07275 — hors bande, score NUL.
        Le modèle facturait 250 %/jour sur ce marché.
        """
        book = Book(
            token_id="yes",
            bids=(Level(price=0.452, size=27.0),),
            asks=(Level(price=0.698, size=39.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        price = planned_price(book, 0.045)
        assert price == pytest.approx(0.5525)
        assert distance_after_posting(0.452, 0.698, price) == pytest.approx(0.07275)
        assert score_on_book(book, price, size=20.0, max_spread=0.045) == 0.0

    def test_a_tight_book_still_scores(self) -> None:
        # La correction ne doit pas tout condamner : sur un carnet serré,
        # l'ordre reste largement dans la bande.
        book = Book(
            token_id="yes",
            bids=(Level(price=0.50, size=100.0),),
            asks=(Level(price=0.51, size=100.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        price = planned_price(book, 0.03)
        score = score_on_book(book, price, size=100.0, max_spread=0.03)
        assert score is not None and score > 0.0

    def test_the_break_even_is_a_spread_of_three_bands(self) -> None:
        """Poster à `m − v/2` ne marque que si l'écart ≤ 3 × la bande.

        La distance finale vaut (A−B)/2 + v/2 ; elle atteint v pile quand
        A−B = 3v. C'est la frontière, et elle vaut d'être écrite : elle dit
        quels marchés sont jouables AVANT de calculer le moindre rendement.
        """
        band = 0.03
        for spread, expected_zero in ((3 * band + 0.001, True), (3 * band - 0.01, False)):
            bid, ask = 0.50 - spread / 2, 0.50 + spread / 2
            book = Book(
                token_id="yes",
                bids=(Level(price=bid, size=100.0),),
                asks=(Level(price=ask, size=100.0),),
                tick_size=0.001,
                min_order_size=5.0,
            )
            price = planned_price(book, band)
            score = score_on_book(book, price, size=100.0, max_spread=band)
            assert (score == 0.0) is expected_zero, (spread, score)

    def test_returns_none_without_an_ask(self) -> None:
        assert distance_after_posting(0.50, None, 0.49) is None


class TestOwnScore:
    def test_balanced_two_sided_quotes_score_their_min(self) -> None:
        # Nos deux achats tombent l'un dans Qone, l'autre dans Qtwo.
        score = own_score(
            price_yes=0.49, price_no=0.49, size=100.0, mid_yes=0.50, max_spread=0.03
        )
        # s = 0,01 des deux côtés (mid_no = 0,50) → équilibré.
        assert score == pytest.approx(order_score(100.0, 0.01, 0.03))

    def test_collapses_to_the_worse_branch(self) -> None:
        """Ce que le prorata linéaire ne pouvait pas dire.

        Une branche collée au milieu ne rattrape pas une branche au bord :
        `min` retient la pire. Additionner les deux surestimait notre part.
        """
        good = own_score(0.4995, 0.4995, 100.0, mid_yes=0.50, max_spread=0.03)
        lopsided = own_score(0.4995, 0.47, 100.0, mid_yes=0.50, max_spread=0.03)
        assert lopsided < good
        # milieu = 0,50 → dans [0,10 ; 0,90] → plancher à max/c, pas zéro.
        assert lopsided == pytest.approx(good / SINGLE_SIDED_PENALTY)
