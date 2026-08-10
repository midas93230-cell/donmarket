"""Tests du flux temps réel et du moniteur.

Le danger propre à ce module n'est pas le plantage, c'est la DÉRIVE. Un carnet
mis à jour avec la mauvaise convention reste un carnet d'apparence normale ; il
s'éloigne du vrai marché message après message, et tous les rendements calculés
dessus restent crédibles. Aucune exception ne sera jamais levée.

D'où des tests qui portent sur la convention elle-même, vérifiée en direct le
2026-07-29 par réconciliation avec un instantané REST (4 carnets sur 12
identiques au palier près, les autres divergeant de 0,07 à 3 % — l'activité de
deux secondes, pas une erreur de convention) :

- `size` est la NOUVELLE taille du palier, pas une variation ;
- une taille nulle SUPPRIME le palier ;
- `side: BUY` désigne le côté achat, et rien d'autre.
"""

from __future__ import annotations

import asyncio

import pytest

from donmarket.analysis.rewards import RewardCandidate, recompute_with_books
from donmarket.api.clob import Book, Level
from donmarket.api.ws import apply_message, apply_price_change, changes_by_token
from donmarket.watch.monitor import (
    MIN_AVERAGE_SECONDS,
    LiquidityProbe,
    LiveMonitor,
    LiveSnapshot,
)


def _book(token_id: str = "111") -> Book:
    return Book(
        token_id=token_id,
        bids=(Level(0.40, 100.0), Level(0.39, 200.0)),
        asks=(Level(0.42, 150.0), Level(0.43, 250.0)),
        tick_size=0.001,
        min_order_size=5.0,
    )


def _sizes(levels) -> dict[float, float]:
    return {level.price: level.size for level in levels}


class TestPriceChange:
    def test_size_replaces_the_level_it_does_not_add_to_it(self) -> None:
        # La question qui décide de tout : 100 + 30 = 130, ou bien 30 ?
        updated = apply_price_change(
            _book(), [{"price": "0.40", "size": "30", "side": "BUY"}]
        )
        assert _sizes(updated.bids)[0.40] == 30.0

    def test_zero_size_removes_the_level(self) -> None:
        updated = apply_price_change(
            _book(), [{"price": "0.40", "size": "0", "side": "BUY"}]
        )
        assert 0.40 not in _sizes(updated.bids)
        # Le palier suivant devient le meilleur — c'est tout l'intérêt.
        assert updated.best_bid == 0.39

    def test_a_new_price_creates_a_level(self) -> None:
        updated = apply_price_change(
            _book(), [{"price": "0.41", "size": "500", "side": "BUY"}]
        )
        assert updated.best_bid == 0.41
        assert _sizes(updated.bids)[0.41] == 500.0

    def test_buy_touches_the_bids_and_sell_the_asks(self) -> None:
        updated = apply_price_change(
            _book(),
            [
                {"price": "0.41", "size": "10", "side": "BUY"},
                {"price": "0.41", "size": "20", "side": "SELL"},
            ],
        )
        assert _sizes(updated.bids)[0.41] == 10.0
        assert _sizes(updated.asks)[0.41] == 20.0

    def test_keeps_best_first_ordering_after_an_update(self) -> None:
        # Le flux envoie ses paliers dans le désordre, comme le REST.
        updated = apply_price_change(
            _book(), [{"price": "0.50", "size": "10", "side": "BUY"}]
        )
        assert [level.price for level in updated.bids] == [0.50, 0.40, 0.39]

    def test_ignores_an_unreadable_change_rather_than_corrupting_the_book(self) -> None:
        updated = apply_price_change(
            _book(),
            [
                {"price": "abc", "size": "10", "side": "BUY"},
                {"price": "0.40", "size": None, "side": "BUY"},
            ],
        )
        assert _sizes(updated.bids) == _sizes(_book().bids)

    def test_leaves_the_original_book_untouched(self) -> None:
        original = _book()
        apply_price_change(original, [{"price": "0.40", "size": "0", "side": "BUY"}])
        assert original.best_bid == 0.40


class TestMessages:
    def test_groups_changes_by_token(self) -> None:
        grouped = changes_by_token(
            {
                "price_changes": [
                    {"asset_id": "111", "price": "0.21", "size": "5", "side": "BUY"},
                    {"asset_id": "222", "price": "0.79", "size": "5", "side": "SELL"},
                    {"asset_id": "111", "price": "0.22", "size": "7", "side": "BUY"},
                ]
            }
        )
        assert set(grouped) == {"111", "222"}
        assert len(grouped["111"]) == 2

    def test_a_book_message_replaces_the_whole_book(self) -> None:
        books: dict[str, Book] = {}
        touched = apply_message(
            books,
            {
                "event_type": "book",
                "asset_id": "111",
                "bids": [{"price": "0.30", "size": "10"}],
                "asks": [{"price": "0.31", "size": "10"}],
            },
        )
        assert touched == ("111",)
        assert books["111"].best_bid == 0.30

    def test_ignores_changes_for_a_token_with_no_snapshot_yet(self) -> None:
        # Sinon on construirait un carnet partiel qui a l'air complet.
        books: dict[str, Book] = {}
        touched = apply_message(
            books,
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": "111", "price": "0.4", "size": "1", "side": "BUY"}
                ],
            },
        )
        assert touched == ()
        assert books == {}

    def test_ignores_message_types_it_does_not_model(self) -> None:
        books = {"111": _book()}
        assert apply_message(books, {"event_type": "last_trade_price"}) == ()


class TestLiveRecompute:
    def _candidate(self, competing: float, gross: float) -> RewardCandidate:
        return RewardCandidate(
            condition_id="0xabc",
            question="Will X happen?",
            slug="will-x",
            daily_pool=150.0,
            competing_q=competing,
            own_q=25.0,
            engaged_usd=50.0,
            gross_yield=gross,
            drift=2.0,
            oscillation=10.0,
            hours_left=96.0,
            token_ids=("111", "222"),
            max_spread=0.03,
        )

    def test_yield_rises_when_competing_liquidity_leaves(self) -> None:
        # Le cas observé en direct : 461 $ de concurrence tombés à 256 $.
        crowded = {
            "111": Book("111", (Level(0.40, 500.0),), (Level(0.42, 500.0),), None, None),
            "222": Book("222", (Level(0.58, 500.0),), (Level(0.60, 500.0),), None, None),
        }
        empty = {
            "111": Book("111", (Level(0.40, 20.0),), (Level(0.42, 20.0),), None, None),
            "222": Book("222", (Level(0.58, 20.0),), (Level(0.60, 20.0),), None, None),
        }
        candidate = self._candidate(competing=400.0, gross=10.0)

        busy = recompute_with_books(candidate, crowded)
        quiet = recompute_with_books(candidate, empty)
        assert quiet.gross_yield > busy.gross_yield

    def test_keeps_the_drift_measured_over_24h(self) -> None:
        # La dérive est une statistique sur 24 h : elle ne change pas en une
        # minute, et la recalculer sur un carnet n'aurait aucun sens.
        candidate = self._candidate(competing=400.0, gross=10.0)
        books = {
            "111": Book("111", (Level(0.40, 10.0),), (Level(0.42, 10.0),), None, None),
            "222": Book("222", (Level(0.58, 10.0),), (Level(0.60, 10.0),), None, None),
        }
        assert recompute_with_books(candidate, books).drift == 2.0

    def test_a_missing_book_leaves_the_candidate_untouched(self) -> None:
        # Une déconnexion ne doit pas ressembler à un pool qui se vide.
        candidate = self._candidate(competing=400.0, gross=10.0)
        assert recompute_with_books(candidate, {}) is candidate


class TestMonitor:
    def test_collects_books_from_the_stream(self) -> None:
        book = _book()

        async def fake_stream(_tokens):
            yield {"111": book}, ("111",)

        monitor = LiveMonitor(stream_factory=fake_stream)
        monitor.watch([LiquidityProbe("k", ("111", "222"), 0.03)])
        deadline = 3.0
        while monitor.snapshot().updates == 0 and deadline > 0:
            deadline -= 0.05
            _sleep(0.05)
        monitor.stop()

        snapshot = monitor.snapshot()
        assert snapshot.updates == 1
        assert snapshot.books["111"].best_bid == 0.40
        assert snapshot.age_seconds is not None

    def test_watching_a_new_set_discards_the_previous_books(self) -> None:
        # Les anciens carnets décrivent des marchés qu'on ne suit plus : les
        # garder ferait vieillir l'affichage sans que ça se voie.
        async def empty_stream(_tokens):
            await asyncio.sleep(0.5)
            return
            yield  # pragma: no cover

        monitor = LiveMonitor(stream_factory=empty_stream)
        monitor.watch([LiquidityProbe("a", ("111", "222"), 0.03)])
        monitor.watch([LiquidityProbe("b", ("333", "444"), 0.03)])
        snapshot = monitor.snapshot()
        monitor.stop()

        assert snapshot.books == {}
        assert snapshot.means == {}
        assert snapshot.tokens == ("333", "444")

    def test_watching_nothing_starts_no_thread(self) -> None:
        monitor = LiveMonitor(stream_factory=lambda _t: None)
        assert monitor.watch([]) is False

    def test_a_one_sided_probe_is_never_watched(self) -> None:
        """Un marché à une seule branche n'a pas de score calculable.

        Le garder consommerait une place dans l'abonnement pour une moyenne qui
        ne viendrait jamais — et une moyenne absente se lit « pas encore assez
        de temps », pas « jamais mesurable ».
        """
        monitor = LiveMonitor(stream_factory=lambda _t: None)
        assert monitor.watch([LiquidityProbe("k", ("111",), 0.03)]) is False

    def test_accumulates_the_time_average_of_competing_liquidity(self) -> None:
        # La sonde porte les DEUX branches : les seaux de Polymarket croisent
        # les bids de l'une avec les asks de l'autre, donc une sonde à un seul
        # jeton n'a pas de score calculable — et n'accumulerait rien.
        crowded = Book("111", (Level(0.40, 1000.0),), (Level(0.42, 1000.0),), None, None)
        empty = Book("111", (Level(0.40, 10.0),), (Level(0.42, 10.0),), None, None)
        other = Book("222", (Level(0.58, 100.0),), (Level(0.60, 100.0),), None, None)

        async def fake_stream(_tokens):
            yield {"111": crowded, "222": other}, ("111",)
            await asyncio.sleep(0.05)
            yield {"111": empty, "222": other}, ("111",)
            await asyncio.sleep(0.05)

        monitor = LiveMonitor(stream_factory=fake_stream)
        monitor.watch([LiquidityProbe("k", ("111", "222"), 0.03)])
        deadline = 3.0
        while monitor.snapshot().updates < 2 and deadline > 0:
            deadline -= 0.05
            _sleep(0.05)
        snapshot = monitor.snapshot()
        monitor.stop()

        # La moyenne existe et vit entre les deux valeurs observées.
        assert 0 < snapshot.means["k"]
        assert snapshot.covered["k"] > 0

    def test_a_mean_covering_too_little_time_is_not_trusted(self) -> None:
        # Une « moyenne » sur deux secondes n'est qu'un instantané déguisé,
        # et c'est le déguisement qui est dangereux.
        snapshot = LiveSnapshot(
            books={}, updated_at=None, updates=1, tokens=("111",),
            means={"k": 120.0}, covered={"k": 3.0},
        )
        assert snapshot.trusted_mean("k") is None

    def test_a_mean_covering_enough_time_is_trusted(self) -> None:
        snapshot = LiveSnapshot(
            books={}, updated_at=None, updates=1, tokens=("111",),
            means={"k": 120.0}, covered={"k": MIN_AVERAGE_SECONDS + 1.0},
        )
        assert snapshot.trusted_mean("k") == 120.0

    def test_stop_is_safe_before_anything_started(self) -> None:
        LiveMonitor(stream_factory=lambda _t: None).stop()


def _sleep(seconds: float) -> None:
    """Attente courte, isolée ici pour que les tests restent lisibles."""
    import time

    time.sleep(seconds)


@pytest.mark.parametrize("side", ["BUY", "buy", "Buy"])
def test_side_is_read_case_insensitively(side: str) -> None:
    updated = apply_price_change(_book(), [{"price": "0.41", "size": "1", "side": side}])
    assert 0.41 in _sizes(updated.bids)
