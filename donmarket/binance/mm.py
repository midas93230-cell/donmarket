"""Tenue de marché sur Binance Prediction — la boucle qui cote en continu.

Ce que la sonde (`probe.py`) fait UNE fois, ce module le fait en boucle et sur
plusieurs marchés. C'est la différence entre un thermomètre et une machine.

## D'où vient l'argent, et pourquoi seulement depuis le 2026-08-19

Un ordre LIMIT ne paie AUCUN frais (`feeRateBps: 0`, mesuré), là où un MARKET
en paie 180 bps. Le revenu est donc l'ÉCART : acheter au meilleur bid, revendre
au meilleur ask. Sur un carnet à 0,39 / 0,40 cela fait 2,56 % par aller-retour.
En preneur, les 1,8 % payés à chaque sens engloutissaient cet écart deux fois —
c'est exactement ce qui a produit les −2,49 $ du compte.

## Ce que la boucle ne sait pas, et qu'elle mesure

Le TAUX DE REMPLISSAGE. Un écart affiché n'est pas un écart obtenu : la leçon
du 2026-07-28 sur Polymarket est que les +2 % lus dans les carnets ne se sont
jamais retrouvés dans les exécutions. La boucle tient donc un registre de
chaque remplissage réel, et c'est lui — pas le carnet — qui dira si la
stratégie vaut quelque chose.

## Sélection adverse : le risque qu'on ne peut pas supprimer

Être rempli à l'achat signifie que quelqu'un a voulu vendre à notre prix. Sur
un marché de prédiction, ce quelqu'un en sait parfois plus que nous. La branche
achetée peut valoir 0 à la résolution. D'où le plafond d'inventaire par marché,
et le refus de coter un marché dont l'échéance approche : une position qu'on
n'a pas eu le temps de revendre devient un pari, pas un stock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .model import PredictionBook, PredictionMarket
from .trade import LIMIT, PredictionOrder

logger = logging.getLogger(__name__)

TICK = 0.01

# Sous deux pas de cotation, il n'y a rien à capturer : acheter au bid et
# revendre à l'ask rapporterait moins qu'un pas, et le moindre décalage du
# carnet transforme le gain en perte.
MIN_SPREAD_TICKS = 2

# Marge d'échéance. Une position qu'on n'a pas le temps de revendre n'est plus
# un stock mais un pari sur le résultat.
MIN_MINUTES_LEFT = 90


@dataclass(frozen=True)
class Rung:
    """Une cotation voulue sur une branche : ce qu'on veut acheter et revendre."""

    market: PredictionMarket
    token_id: str
    outcome: str
    buy_price: float
    sell_price: float
    best_bid: float
    best_ask: float

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def gross_edge(self) -> float:
        """Gain brut d'un aller-retour complet, en fraction du capital engagé.

        Sans frais (mesuré : `feeRateBps: 0` sur un LIMIT), c'est exactement
        l'écart rapporté au prix d'achat. BRUT : il suppose les DEUX côtés
        remplis, ce que rien ne garantit — c'est même l'inconnue centrale.
        """
        if self.buy_price <= 0:
            return 0.0
        return (self.sell_price - self.buy_price) / self.buy_price


@dataclass
class Inventory:
    """Ce qu'on détient réellement, par jeton."""

    shares: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)

    def add_fill(self, token_id: str, shares: float, usdt: float) -> None:
        self.shares[token_id] = self.shares.get(token_id, 0.0) + shares
        self.cost[token_id] = self.cost.get(token_id, 0.0) + usdt

    def held(self, token_id: str) -> float:
        return self.shares.get(token_id, 0.0)


def eligible(
    markets: Sequence[PredictionMarket],
    books: Mapping[str, PredictionBook],
    *,
    now_ms: int,
    tick: float = TICK,
) -> tuple[list[Rung], list[tuple[int, str]]]:
    """Les branches cotables, et le motif de chaque écartée.

    Les rejets sont RENDUS et non journalisés : « aucun marché éligible » sans
    les motifs envoie chercher une panne là où c'est un filtre qui a tout pris.
    """
    retenues: list[Rung] = []
    rejets: list[tuple[int, str]] = []

    for market in markets:
        statut = (market.status or "").upper()
        if statut and statut != "OPEN":
            rejets.append((market.market_id, "statut " + statut))
            continue
        if market.end_time_ms is None:
            rejets.append((market.market_id, "échéance illisible"))
            continue
        reste_min = (market.end_time_ms - now_ms) / 60_000
        if reste_min < MIN_MINUTES_LEFT:
            rejets.append(
                (
                    market.market_id,
                    f"échéance dans {reste_min:.0f} min — une position non "
                    "revendue deviendrait un pari",
                )
            )
            continue

        for token_id in market.outcome_token_ids:
            book = books.get(token_id)
            if book is None or book.best_bid is None or book.best_ask is None:
                rejets.append((market.market_id, "carnet incomplet"))
                continue
            bid, ask = book.best_bid.price, book.best_ask.price
            ecart_ticks = round((ask - bid) / tick)
            if ecart_ticks < MIN_SPREAD_TICKS:
                rejets.append(
                    (
                        market.market_id,
                        f"écart {ecart_ticks} pas — moins de "
                        f"{MIN_SPREAD_TICKS}, rien à capturer",
                    )
                )
                continue
            retenues.append(
                Rung(
                    market=market,
                    token_id=token_id,
                    outcome=str(book.raw.get("outcome") or "?"),
                    # On REJOINT la file au meilleur prix plutôt que de
                    # l'améliorer d'un pas : améliorer resserre l'écart qu'on
                    # cherche justement à encaisser.
                    buy_price=round(bid, 2),
                    sell_price=round(ask, 2),
                    best_bid=bid,
                    best_ask=ask,
                )
            )

    # Le meilleur écart d'abord : c'est lui le revenu, à remplissage égal.
    retenues.sort(key=lambda r: r.gross_edge, reverse=True)
    return retenues, rejets


def plan(
    rungs: Sequence[Rung],
    inventory: Inventory,
    *,
    notional_per_market: float,
    max_markets: int,
) -> list[PredictionOrder]:
    """Les ordres voulus, à partir de l'état. Fonction PURE : aucun réseau.

    Règle : une branche où l'on ne détient rien se fait ACHETER au bid ; une
    branche où l'on détient des parts se fait REVENDRE à l'ask. Jamais les deux
    à la fois sur la même branche — ce serait se croiser soi-même, et le carnet
    nous apparierait contre nous, ce qui paie l'écart au lieu de l'encaisser.
    """
    ordres: list[PredictionOrder] = []
    for rung in rungs[:max_markets]:
        detenu = inventory.held(rung.token_id)
        if detenu > 0:
            ordres.append(
                PredictionOrder(
                    market_id=rung.market.market_id,
                    token_id=rung.token_id,
                    side="SELL",
                    order_type=LIMIT,
                    price=rung.sell_price,
                    size=detenu,
                )
            )
            continue
        parts = round(notional_per_market / rung.buy_price, 2)
        if parts <= 0:
            continue
        ordres.append(
            PredictionOrder(
                market_id=rung.market.market_id,
                token_id=rung.token_id,
                side="BUY",
                order_type=LIMIT,
                price=rung.buy_price,
                size=parts,
            )
        )
    return ordres
