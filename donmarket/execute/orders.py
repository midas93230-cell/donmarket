"""Ce que le bot poserait, ordre par ordre — sans rien poser.

Ce module répond à une question qu'on doit pouvoir se poser AVANT d'armer quoi
que ce soit : à quel prix, sur quel jeton, pour combien de parts ? Tant qu'on
ne peut pas lire ça noir sur blanc, « automatiser mes trades » veut dire faire
confiance à du code qu'on n'a pas vu agir.

Il est PUR : pas de réseau, pas de clé, pas d'écriture. Un ordre planifié ici
n'existe que dans la mémoire du programme.

## La règle de prix, et le compromis qu'elle porte

Être dans la bande qualifiante ne suffit pas : Polymarket paie en
`((v − s)/v)² · taille`, donc le score s'effondre à mesure qu'on s'éloigne du
milieu, et vaut EXACTEMENT ZÉRO au bord (voir `analysis/scoring`). On vise donc
une distance cible, `quote_spread`, et non la bande entière. Deux situations :

- **Le meilleur bid est plus près du milieu que notre cible.** On s'y range, au
  même prix : il score mieux que notre cible et ne coûte rien de plus qu'entrer
  dans la file derrière ceux qui y sont déjà.
- **Le meilleur bid est plus loin que notre cible.** On se place à la cible,
  donc on améliore le carnet, donc on est **premier servi si le marché tourne
  contre nous**. C'est le moment où la récompense se paie en risque
  d'inventaire, et l'ordre le dit dans son motif plutôt que de le taire.

Une version antérieure posait au bord de la bande (`mid − max_spread`, arrondi
vers le bas). Elle plaçait donc du capital en tête de file, exposé à toute la
sélection adverse, pour un score nul — et l'arrondi la sortait même de la
bande. Rien dans le modèle de rendement linéaire d'alors ne pouvait le dire.

## Pourquoi les deux branches

Coter YES et NO du même marché fait qu'un remplissage d'un côté est en partie
compensé par l'autre : c'est ce qui rend la dérive mesurée (`analysis/rewards`)
un MAJORANT et non une perte attendue. Un seul côté, c'est un pari
directionnel déguisé en tenue de marché.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..analysis.rewards import RewardCandidate
from ..analysis.scoring import quote_spread
from ..api.clob import Book

# Pas de cotation par défaut sur Polymarket. Le carnet le renvoie par jeton
# (`tick_size`) ; cette valeur ne sert que lorsqu'il ne le donne pas.
DEFAULT_TICK = 0.001

# En dessous, un ordre n'a plus de sens économique : le prix ne peut pas être
# nul et une part au-dessus de 0,999 ne laisse aucun gain possible.
MIN_PRICE = 0.001
MAX_PRICE = 0.999

BUY = "BUY"


def round_down_to_tick(price: float, tick: float) -> float:
    """Arrondit vers le BAS au pas de cotation.

    Vers le bas et jamais vers le haut : arrondir vers le haut ferait payer
    l'ordre un tick de plus que voulu, ce qui, répété sur les deux branches de
    chaque marché, mange directement la marge.
    """
    if tick <= 0:
        return price
    return math.floor(price / tick + 1e-9) * tick


@dataclass(frozen=True)
class PlannedOrder:
    """Un ordre que le bot poserait, avec la raison de son prix."""

    condition_id: str
    question: str
    token_id: str
    side: str
    price: float
    size: float  # en parts
    reason: str

    @property
    def notional(self) -> float:
        """Dollars immobilisés par cet ordre s'il est posé."""
        return self.price * self.size


def plan_orders(
    candidate: RewardCandidate,
    books: Mapping[str, Book],
    *,
    tick: float = DEFAULT_TICK,
) -> tuple[PlannedOrder, ...]:
    """Les ordres qui qualifieraient ce candidat aux récompenses.

    Renvoie un tuple VIDE dès qu'une branche manque. Poser sur une seule
    branche d'un marché à deux branches n'est pas une demi-position : c'est une
    position directionnelle nue, et ce n'est pas la stratégie.
    """
    if not candidate.token_ids or candidate.max_spread <= 0:
        return ()
    size = candidate.engaged_usd  # `rewardsMinSize` parts, à ~1 $ le jeu complet
    if size <= 0:
        return ()

    orders: list[PlannedOrder] = []
    for token_id in candidate.token_ids:
        book = books.get(token_id)
        if book is None:
            return ()
        mid = book.midpoint
        if mid is None or book.best_bid is None:
            return ()

        step = book.tick_size or tick
        target = quote_spread(candidate.max_spread)

        if mid - book.best_bid <= target:
            price = book.best_bid
            reason = "meilleur bid plus près du milieu que notre cible — on rejoint la file"
        else:
            price = round_down_to_tick(mid - target, step)
            reason = (
                "meilleur bid trop loin du milieu — on améliore le carnet, "
                "donc premier servi"
            )

        price = round_down_to_tick(price, step)
        if price < MIN_PRICE or price > MAX_PRICE:
            return ()

        orders.append(
            PlannedOrder(
                condition_id=candidate.condition_id,
                question=candidate.question,
                token_id=token_id,
                side=BUY,
                price=price,
                size=size,
                reason=reason,
            )
        )

    return tuple(orders)


@dataclass(frozen=True)
class OrderPlan:
    """Le plan complet, avec ce qu'il coûte réellement."""

    orders: tuple[PlannedOrder, ...]
    bankroll: float
    skipped: tuple[str, ...] = ()

    @property
    def notional(self) -> float:
        return sum(order.notional for order in self.orders)

    @property
    def markets(self) -> int:
        return len({order.condition_id for order in self.orders})

    @property
    def fits(self) -> bool:
        return self.notional <= self.bankroll


def plan_portfolio(
    candidates: Sequence[RewardCandidate],
    books: Mapping[str, Book],
    *,
    bankroll: float,
    tick: float = DEFAULT_TICK,
) -> OrderPlan:
    """Planifie les ordres de plusieurs candidats sous contrainte de capital.

    Le coût réel d'un marché est la somme de ses DEUX ordres, et il n'est connu
    qu'une fois les prix calculés : `engaged_usd` supposait bid_yes + bid_no ≈
    1 $, ce qui est vrai à un ou deux ticks près, pas exactement. On vérifie
    donc le budget sur le prix effectivement retenu — sinon le plan peut
    dépasser le capital de quelques dollars sans que rien ne le signale.

    Un marché qui ne tient pas est écarté AVEC son motif : sauter un candidat
    en silence donne un plan qui a l'air complet alors qu'il ne l'est pas.
    """
    remaining = bankroll
    kept: list[PlannedOrder] = []
    skipped: list[str] = []

    for candidate in candidates:
        orders = plan_orders(candidate, books, tick=tick)
        if not orders:
            skipped.append(f"{candidate.question[:50]} — carnet incomplet")
            continue
        cost = sum(order.notional for order in orders)
        if cost > remaining:
            skipped.append(
                f"{candidate.question[:50]} — {cost:.2f} $ > {remaining:.2f} $ restants"
            )
            continue
        kept.extend(orders)
        remaining -= cost

    return OrderPlan(orders=tuple(kept), bankroll=bankroll, skipped=tuple(skipped))
