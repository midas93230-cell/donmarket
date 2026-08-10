"""Décider si un ordre endormi aurait été rempli — sans jamais se flatter.

## Le problème, et pourquoi il n'a pas de solution exacte

Un ordre de démonstration n'est pas dans le carnet. On ne peut donc pas
observer son remplissage : on ne peut que le déduire des exécutions réelles.
Or le remplissage dépend de la POSITION DANS LA FILE, et la file n'est pas
publique — le carnet donne la taille par palier, jamais l'ordre d'arrivée.

Toute hypothèse sur cette position est invérifiable. Une hypothèse optimiste
produirait un compte de démonstration qui gagne de l'argent qu'un compte réel
n'aurait pas gagné, ce qui est exactement le mensonge à ne pas commettre ici.

## L'hypothèse retenue : toujours DERNIER

On se place systématiquement derrière tout ce qui est déjà posté à notre prix
ou mieux. C'est la borne pessimiste, et elle est presque vraie : un ordre qui
vient d'être posé EST le dernier de sa file. Il ne remonte qu'en attendant, et
la stratégie recote chaque minute — elle repart donc en queue à chaque fois.

Conséquence : un vendeur qui écoule 40 parts alors que 120 dorment devant nous
ne nous remplit pas du tout. C'est sévère, c'est voulu, et le compte de
démonstration en sortira plus pauvre que la réalité plutôt que l'inverse.

## Ce qui déclenche un remplissage

Notre ACHAT à `p` n'est servi que par un vendeur PRENEUR qui accepte `p` ou
moins. Un acheteur preneur, lui, consomme des ventes et ne nous touche pas —
piège classique : compter toutes les exécutions doublerait les remplissages.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from ..api.clob import Book
from .ledger import PaperFill


@dataclass(frozen=True)
class MarketTrade:
    """Une exécution réellement survenue sur le marché.

    `taker_side` est le sens du PRENEUR, celui qui a franchi la fourchette.
    C'est la convention de `data-api.polymarket.com/trades` et du flux
    `last_trade_price`. La confondre avec le sens du teneur inverse tous les
    remplissages, sans qu'aucun chiffre n'ait l'air anormal.
    """

    token_id: str
    price: float
    size: float  # en parts
    taker_side: str  # "BUY" ou "SELL"
    traded_at: datetime


@dataclass(frozen=True)
class RestingOrder:
    """Un de nos ordres, posé sur le carnet du simulateur."""

    token_id: str
    price: float
    size: float  # parts demandées
    filled: float = 0.0  # parts déjà obtenues

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    @property
    def is_complete(self) -> bool:
        return self.remaining <= 1e-9

    def with_fill(self, shares: float) -> "RestingOrder":
        return replace(self, filled=min(self.size, self.filled + shares))


def queue_ahead(book: Book, price: float) -> float:
    """Parts déjà postées devant nous pour un achat à `price`.

    Tout ce qui est offert à un prix SUPÉRIEUR est servi avant nous, et tout ce
    qui est offert au même prix aussi puisque nous venons d'arriver. D'où le
    `>=` : un `>` strict nous ferait passer devant la file de notre propre
    palier, c'est-à-dire nous inventerait une ancienneté.
    """
    return sum(level.size for level in book.bids if level.price >= price - 1e-9)


def fill_against_trade(
    order: RestingOrder,
    trade: MarketTrade,
    *,
    ahead: float,
) -> float:
    """Parts que ce passage de marché nous aurait données. Zéro le plus souvent.

    `ahead` est la profondeur qui nous précède, mesurée sur le carnet AVANT
    l'exécution. Le vendeur la consomme d'abord ; nous ne touchons que le
    reliquat, et seulement à hauteur de ce qu'il nous reste à acheter.
    """
    if order.is_complete or trade.token_id != order.token_id:
        return 0.0
    # Seul un vendeur preneur peut servir un achat qui dort.
    if trade.taker_side.upper() != "SELL":
        return 0.0
    # Il faut qu'il accepte notre prix. Le vendeur descend le carnet depuis le
    # meilleur bid ; s'il s'est arrêté au-dessus de nous, nous n'existons pas.
    if trade.price > order.price + 1e-9:
        return 0.0
    overflow = trade.size - ahead
    if overflow <= 0:
        return 0.0
    return min(overflow, order.remaining)


def apply_trade(
    order: RestingOrder,
    trade: MarketTrade,
    book: Book | None,
) -> tuple[RestingOrder, PaperFill | None]:
    """Applique une exécution à un ordre, et rend le remplissage éventuel.

    Sans carnet, la file est inconnue : on suppose alors qu'elle est infinie et
    rien n'est rempli. Supposer l'inverse — file vide, donc tout pour nous —
    transformerait une donnée manquante en profit.
    """
    ahead = queue_ahead(book, order.price) if book is not None else float("inf")
    shares = fill_against_trade(order, trade, ahead=ahead)
    if shares <= 0:
        return order, None
    fill = PaperFill(
        token_id=order.token_id,
        # Rempli à NOTRE prix limite, pas à celui du preneur : un ordre à cours
        # limité ne bénéficie jamais d'une amélioration côté passif.
        price=order.price,
        size=shares,
        filled_at=trade.traded_at,
    )
    return order.with_fill(shares), fill
