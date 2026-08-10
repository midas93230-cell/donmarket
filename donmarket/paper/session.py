"""La session de démonstration : l'état complet, et comment il avance d'un tour.

Tout est pur ici. Un tour prend l'état, les carnets frais et les exécutions
survenues depuis le tour précédent, et rend un NOUVEL état. Aucun réseau,
aucune horloge implicite — c'est ce qui rend la mécanique testable seconde par
seconde sans attendre le marché.

## Les trois mouvements du solde, dans l'ordre où ils s'appliquent

1. `with_trades` — les exécutions réelles remplissent nos ordres endormis. Du
   cash devient de l'inventaire. Le solde ne bouge pas d'un centime : on a
   échangé des dollars contre des parts de même valeur.
2. `with_rewards` — le temps passé à coter dans la bande crédite des dollars.
   C'est la seule entrée nette, et toute la thèse de la stratégie.
3. `with_marks` — l'inventaire est réévalué au milieu du carnet. C'est ici que
   se font les gains et les pertes.

Séparer les trois est délibéré : quand le solde bougera, on saura par lequel.
Un compte qui mélangerait remplissage et réévaluation ne permettrait plus de
dire si l'on gagne parce qu'on est payé ou parce que le vent tourne.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Sequence

from ..analysis.scoring import competing_score, qmin, score_on_book
from ..api.clob import Book
from .fills import MarketTrade, RestingOrder, apply_trade
from .ledger import InsufficientCash, PaperAccount, PaperFill
from .requote import RequotePolicy, requoted

SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class PaperMarket:
    """Un marché coté par la session, avec de quoi recalculer sa récompense."""

    condition_id: str
    question: str
    token_ids: tuple[str, ...]
    max_spread: float
    daily_pool: float
    min_size: float

    @property
    def is_quotable(self) -> bool:
        return len(self.token_ids) >= 2 and self.max_spread > 0 and self.min_size > 0


def _trade_key(trade: MarketTrade) -> tuple[str, float, float, float]:
    """Identifie une exécution pour ne pas la compter deux fois.

    Le flux public ne renvoie pas d'identifiant stable par remplissage, et deux
    sondages successifs se recouvrent largement. À défaut, on identifie par
    (jeton, instant, prix, taille). Deux exécutions rigoureusement identiques à
    la même seconde seront confondues : on en perd une. C'est le sens d'erreur
    acceptable — un remplissage manqué appauvrit le compte, un remplissage
    compté deux fois l'enrichit d'un gain qui n'existe pas.
    """
    return (trade.token_id, trade.traded_at.timestamp(), trade.price, trade.size)


@dataclass(frozen=True)
class PaperSession:
    """L'état complet d'une démonstration en cours."""

    account: PaperAccount
    markets: tuple[PaperMarket, ...] = ()
    orders: tuple[RestingOrder, ...] = ()
    marks: Mapping[str, float] = field(default_factory=dict)
    seen: frozenset[tuple[str, float, float, float]] = frozenset()
    rejected_for_cash: int = 0
    # Absente par défaut : une session qui recote sans qu'on l'ait demandé
    # décrirait une autre stratégie que celle qu'on croit observer.
    policy: RequotePolicy | None = None
    requotes: int = 0

    @classmethod
    def opening(
        cls,
        *,
        bankroll: float,
        markets: Sequence[PaperMarket],
        orders: Sequence[RestingOrder],
        policy: RequotePolicy | None = None,
    ) -> "PaperSession":
        return cls(
            account=PaperAccount.opening(bankroll),
            markets=tuple(markets),
            orders=tuple(orders),
            policy=policy,
        )

    @property
    def token_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for market in self.markets:
            for token_id in market.token_ids:
                if token_id not in seen:
                    seen.append(token_id)
        return tuple(seen)

    @property
    def equity(self) -> float:
        return self.account.equity(self.marks)

    @property
    def pnl(self) -> float:
        return self.account.pnl(self.marks)

    @property
    def pnl_pct(self) -> float:
        return self.account.pnl_pct(self.marks)

    @property
    def filled_orders(self) -> int:
        return sum(1 for order in self.orders if order.is_complete)

    def with_marks(self, books: Mapping[str, Book]) -> "PaperSession":
        """Réévalue l'inventaire au milieu des carnets frais.

        Un carnet sans milieu lisible laisse l'ancienne cotation en place
        plutôt que de l'effacer : perdre un prix ne doit pas faire disparaître
        la valeur d'une position.
        """
        updated = dict(self.marks)
        for token_id, book in books.items():
            midpoint = book.midpoint
            if midpoint is not None:
                updated[token_id] = midpoint
        return replace(self, marks=updated)

    def with_trades(
        self, trades: Sequence[MarketTrade], books: Mapping[str, Book]
    ) -> "PaperSession":
        """Confronte nos ordres endormis aux exécutions réellement survenues."""
        account = self.account
        orders = list(self.orders)
        seen = set(self.seen)
        rejected = self.rejected_for_cash

        for trade in trades:
            key = _trade_key(trade)
            if key in seen:
                continue
            seen.add(key)
            for index, order in enumerate(orders):
                updated, fill = apply_trade(order, trade, books.get(order.token_id))
                if fill is None:
                    continue
                try:
                    account = account.with_fill(fill)
                except InsufficientCash:
                    # Le compte ne suit plus : l'ordre reste endormi et non
                    # rempli. Le raboter à la volée inventerait un remplissage
                    # partiel que le carnet n'a pas produit.
                    rejected += 1
                    continue
                orders[index] = updated

        return replace(
            self,
            account=account,
            orders=tuple(orders),
            seen=frozenset(seen),
            rejected_for_cash=rejected,
        )

    def resting_score(
        self, books: Mapping[str, Book], market: PaperMarket
    ) -> float | None:
        """Le score de NOS ordres tels qu'ils dorment vraiment sur ce carnet.

        Distinction qui a coûté cher : il ne s'agit pas de ce que vaudrait un
        ordre FRAIS reposté maintenant au prix idéal, mais de ce que valent les
        ordres que nous avons posés, au prix où nous les avons posés, pour les
        parts qui leur restent. Les deux ne divergent qu'une fois le marché en
        mouvement — c'est-à-dire dans tous les cas qui décident du résultat.

        Trois raisons de ne rien marquer, toutes invisibles au modèle d'ordre
        frais, et toutes du même côté (celui qui flatte) :

        - **servi** : les parts consommées ont quitté le carnet ;
        - **distancé** : le milieu a dérivé, notre prix n'a pas bougé, la
          distance sort de la bande et le score tombe à zéro (`order_score`) ;
        - **croisé** : notre achat est passé au-dessus du meilleur ask. Il
          n'attend plus, il s'exécute — un ordre qui croise n'est pas de la
          liquidité postée, et le rémunérer reviendrait à se payer d'un ordre
          que le carnet ne contient pas.

        `None` veut dire « carnet illisible », jamais « rien gagné » : les deux
        rapportent zéro dollar, mais un carnet manquant ne doit pas ressembler à
        un mauvais placement, sans quoi une déconnexion se lirait comme une
        mesure.
        """
        book_yes = books.get(market.token_ids[0])
        if book_yes is None:
            return None
        mid_yes = book_yes.midpoint
        if mid_yes is None:
            return None

        buckets: list[float] = []
        for token_id in market.token_ids[:2]:
            book = books.get(token_id)
            if book is None:
                return None
            bucket = 0.0
            for order in self.orders:
                if order.token_id != token_id:
                    continue
                # `rewardsMinSize` est un seuil de QUALIFICATION : sous lui
                # l'ordre reste posté mais cesse d'être compté. Appliqué à nous
                # seuls — le carnet public agrège des paliers et non des ordres,
                # la règle y est inapplicable. L'asymétrie nous sous-estime.
                if order.remaining < market.min_size:
                    continue
                if book.best_ask is not None and order.price >= book.best_ask:
                    continue
                score = score_on_book(
                    book, order.price, order.remaining, market.max_spread
                )
                if score is None:
                    return None
                bucket += score
            buckets.append(bucket)
        return qmin(buckets[0], buckets[1], midpoint=mid_yes)

    def reward_usd(self, books: Mapping[str, Book], seconds: float) -> float:
        """Dollars de récompense mérités pendant `seconds`, mesurés sur carnet.

        Mesuré et non estimé : le score ne dépend que du prix et de la taille de
        nos ordres, tous deux connus exactement, et la concurrence est lue sur
        le carnet du moment. C'est le seul terme de ce module qui ne repose sur
        aucune hypothèse invérifiable.
        """
        if seconds <= 0:
            return 0.0
        total = 0.0
        for market in self.markets:
            if not market.is_quotable or market.daily_pool <= 0:
                continue
            # `None` signifie « carnet illisible », et zéro « rien qui marque ».
            # Les deux ne rapportent rien, mais `None <= 0` lèverait une
            # exception en pleine session : le cas est traité avant comparaison.
            own = self.resting_score(books, market)
            if own is None or own <= 0:
                continue
            # Notre score peut se lire alors que la concurrence non : il suffit
            # d'une branche où nous n'avons rien posté et dont le carnet n'a pas
            # d'ask. Sans cette garde, `own + None` interrompt la session.
            competing = competing_score(books, market.token_ids, market.max_spread)
            if competing is None:
                continue
            share = own / (own + competing)
            total += market.daily_pool * share * (seconds / SECONDS_PER_DAY)
        return total

    def with_rewards(self, books: Mapping[str, Book], seconds: float) -> "PaperSession":
        earned = self.reward_usd(books, seconds)
        if earned <= 0:
            return self
        return replace(self, account=self.account.with_reward(earned))

    def with_requotes(self, books: Mapping[str, Book]) -> "PaperSession":
        """Remplace les cotes que le milieu a distancées.

        Vient APRÈS le crédit de récompense : pendant le temps écoulé, l'ordre
        était à son ancien prix, et c'est celui-là qu'il faut payer. Recoter
        d'abord reviendrait à se faire rétribuer pour un placement qu'on n'avait
        pas encore.
        """
        policy = self.policy
        if policy is None or not policy.enabled or not self.orders:
            return self

        spreads = {
            token_id: market.max_spread
            for market in self.markets
            for token_id in market.token_ids
        }
        orders = list(self.orders)
        moved = 0
        for index, order in enumerate(orders):
            book = books.get(order.token_id)
            spread = spreads.get(order.token_id)
            if book is None or spread is None:
                continue
            fresh = requoted(order, book, spread, policy)
            if fresh is None:
                continue
            orders[index] = fresh
            moved += 1

        if not moved:
            return self
        return replace(self, orders=tuple(orders), requotes=self.requotes + moved)

    def tick(
        self,
        *,
        books: Mapping[str, Book],
        trades: Sequence[MarketTrade],
        seconds: float,
    ) -> "PaperSession":
        """Un tour complet : remplissages, récompenses, recotation, réévaluation.

        La réévaluation vient EN DERNIER pour que les parts acquises pendant ce
        tour soient valorisées au prix du tour, et non au prix précédent.
        """
        return (
            self.with_trades(trades, books)
            .with_rewards(books, seconds)
            .with_requotes(books)
            .with_marks(books)
        )


@dataclass(frozen=True)
class SessionSnapshot:
    """Ce qu'il faut pour afficher une session sans exposer son état interne."""

    started_at: datetime
    observed_at: datetime
    starting_usd: float
    equity_usd: float
    cash_usd: float
    inventory_usd: float
    rewards_usd: float
    pnl_usd: float
    pnl_pct: float
    fills: int
    orders_resting: int
    orders_filled: int
    markets: int

    @property
    def elapsed_seconds(self) -> float:
        return (self.observed_at - self.started_at).total_seconds()


def snapshot(
    session: PaperSession, *, started_at: datetime, observed_at: datetime
) -> SessionSnapshot:
    account = session.account
    return SessionSnapshot(
        started_at=started_at,
        observed_at=observed_at,
        starting_usd=account.starting_usd,
        equity_usd=session.equity,
        cash_usd=account.cash_usd,
        inventory_usd=account.inventory_value(session.marks),
        rewards_usd=account.rewards_usd,
        pnl_usd=session.pnl,
        pnl_pct=session.pnl_pct,
        fills=len(account.fills),
        orders_resting=sum(1 for order in session.orders if not order.is_complete),
        orders_filled=session.filled_orders,
        markets=len(session.markets),
    )


__all__ = [
    "PaperFill",
    "PaperMarket",
    "PaperSession",
    "SessionSnapshot",
    "snapshot",
]
