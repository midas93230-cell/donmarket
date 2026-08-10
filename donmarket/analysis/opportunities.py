"""Détection d'opportunités — le cerveau décisionnel de DONmarket.

Deux régimes de seuils cohabitent :

- NORMAL  : large, sert à observer et à mesurer ce que le marché offre.
- SERIEUX : le « mode sérieux ». Il n'y a ni transe ni état de grâce dans un
  programme — ce qui existe, et qui est bien plus utile, c'est un régime de
  HAUTE CONVICTION : plusieurs conditions strictes doivent être réunies
  simultanément, et le bot reste immobile le reste du temps. Ne rien faire est
  une décision, et sur ces marchés c'est la bonne dans l'immense majorité des cas.

Ce module est PUR : il ne fait aucun appel réseau et n'écrit aucun fichier, ce
qui permet de tester la logique de décision sans dépendre de l'état du marché.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Sequence

from ..api.clob import Book
from ..model import Market

# Un jeu complet (toutes les branches d'un marché) est remboursé exactement 1 $
# à la résolution : c'est l'invariant sur lequel repose tout l'arbitrage.
COMPLETE_SET_PAYOUT = 1.0

# Polymarket ne prélève pas de frais de transaction sur le CLOB à ce jour, mais
# un moteur qui suppose zéro coût est un moteur qui ment. On garde une réserve
# explicite, ajustable, couvrant frais éventuels et glissement.
DEFAULT_COST_BUFFER = 0.002


class Mode(str, Enum):
    NORMAL = "normal"
    SERIEUX = "serieux"


@dataclass(frozen=True)
class Thresholds:
    """Les conditions qu'une opportunité doit franchir pour être retenue."""

    min_edge: float  # marge nette minimale par jeu complet, en dollars
    min_depth_usd: float  # dollars réellement disponibles au carnet
    min_volume_24h: float  # activité minimale du marché
    max_spread: float  # au-delà, le carnet est trop lâche pour être fiable
    max_days_to_resolution: float | None  # capital immobilisé trop longtemps
    cost_buffer: float = DEFAULT_COST_BUFFER

    @staticmethod
    def for_mode(mode: Mode) -> "Thresholds":
        if mode is Mode.SERIEUX:
            # Haute conviction : marge nette d'au moins 1 cent par dollar
            # engagé, profondeur réelle, marché vivant, résolution proche.
            return Thresholds(
                min_edge=0.01,
                min_depth_usd=50.0,
                min_volume_24h=1_000.0,
                max_spread=0.03,
                max_days_to_resolution=90.0,
            )
        return Thresholds(
            min_edge=0.0,
            min_depth_usd=0.0,
            min_volume_24h=0.0,
            max_spread=1.0,
            max_days_to_resolution=None,
        )


@dataclass(frozen=True)
class Opportunity:
    """Une opportunité mesurée sur un marché, avec sa raison d'être retenue."""

    condition_id: str
    question: str
    slug: str
    kind: str  # "buy_complete_set" ou "sell_complete_set"
    edge: float  # marge NETTE par jeu complet, réserve de coûts déduite
    gross_edge: float  # marge brute, avant réserve
    sum_price: float  # somme des meilleurs prix des branches
    depth_usd: float  # dollars exécutables au meilleur palier
    spread: float
    volume_24h: float
    days_to_resolution: float | None
    max_shares: float  # parts exécutables sans creuser le carnet
    rejected_by: tuple[str, ...] = ()  # seuils non franchis (vide = retenue)

    @property
    def is_actionable(self) -> bool:
        return not self.rejected_by

    @property
    def profit_at(self) -> float:
        """Gain théorique si l'on prend toute la taille disponible."""
        return self.edge * self.max_shares


def _days_until(end_date: datetime | None, *, now: datetime | None = None) -> float | None:
    if end_date is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (end_date - reference).total_seconds() / 86400.0


def _best_prices(
    market: Market, books: Mapping[str, Book]
) -> tuple[list[float], list[float], list[float], list[float]] | None:
    """Extrait (asks, bids, tailles ask, tailles bid) pour chaque branche.

    Renvoie None si une seule branche manque : un arbitrage incomplet n'est pas
    un arbitrage, c'est un pari déguisé.
    """
    asks: list[float] = []
    bids: list[float] = []
    ask_sizes: list[float] = []
    bid_sizes: list[float] = []

    for outcome in market.outcomes:
        book = books.get(outcome.token_id)
        if book is None or book.best_ask is None or book.best_bid is None:
            return None
        asks.append(book.best_ask)
        bids.append(book.best_bid)
        ask_sizes.append(book.asks[0].size)
        bid_sizes.append(book.bids[0].size)

    if len(asks) < 2:
        return None
    return asks, bids, ask_sizes, bid_sizes


def _check(
    thresholds: Thresholds,
    *,
    edge: float,
    depth_usd: float,
    volume_24h: float,
    spread: float,
    days: float | None,
) -> tuple[str, ...]:
    """Liste les seuils non franchis. Vide = l'opportunité est retenue."""
    failures: list[str] = []
    if edge < thresholds.min_edge:
        failures.append(f"edge {edge:.4f} < {thresholds.min_edge:.4f}")
    if depth_usd < thresholds.min_depth_usd:
        failures.append(f"profondeur {depth_usd:.2f}$ < {thresholds.min_depth_usd:.2f}$")
    if volume_24h < thresholds.min_volume_24h:
        failures.append(f"volume24h {volume_24h:.0f}$ < {thresholds.min_volume_24h:.0f}$")
    if spread > thresholds.max_spread:
        failures.append(f"spread {spread:.4f} > {thresholds.max_spread:.4f}")
    if thresholds.max_days_to_resolution is not None:
        if days is None or days > thresholds.max_days_to_resolution:
            shown = "inconnue" if days is None else f"{days:.0f}j"
            failures.append(
                f"résolution {shown} > {thresholds.max_days_to_resolution:.0f}j"
            )
    return tuple(failures)


def evaluate_market(
    market: Market,
    books: Mapping[str, Book],
    *,
    mode: Mode = Mode.SERIEUX,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Mesure les deux arbitrages de jeu complet possibles sur un marché.

    - ACHAT  : acheter toutes les branches pour moins de 1 $ → gain garanti.
    - VENTE  : vendre toutes les branches pour plus de 1 $ → gain garanti
               (suppose de détenir ou de pouvoir constituer le jeu).

    Toutes les opportunités mesurées sont renvoyées, y compris celles qui
    échouent aux seuils : `rejected_by` dit pourquoi. Mesurer puis filtrer vaut
    mieux que filtrer en silence — c'est ce qui permet de savoir si le marché
    n'offre rien, ou si ce sont nos seuils qui sont mal réglés.
    """
    if not market.is_tradable:
        return []

    prices = _best_prices(market, books)
    if prices is None:
        return []
    asks, bids, ask_sizes, bid_sizes = prices

    thresholds = Thresholds.for_mode(mode)
    days = _days_until(market.end_date, now=now)

    # Le spread se mesure BRANCHE PAR BRANCHE (ask_i − bid_i). Comparer l'ask du
    # « No » au bid du « Yes » produisait des spreads de 0,96 sur des carnets
    # pourtant serrés, et faisait rejeter tous les marchés à tort.
    # On retient le pire des spreads : c'est la branche la plus lâche qui
    # décide de la qualité réelle d'exécution du jeu complet.
    spread = max(ask - bid for ask, bid in zip(asks, bids))

    results: list[Opportunity] = []

    # --- Achat du jeu complet ---
    sum_asks = sum(asks)
    gross_buy = COMPLETE_SET_PAYOUT - sum_asks
    net_buy = gross_buy - thresholds.cost_buffer
    max_shares_buy = min(ask_sizes)
    depth_buy = sum(price * size for price, size in zip(asks, ask_sizes))
    results.append(
        Opportunity(
            condition_id=market.condition_id,
            question=market.question,
            slug=market.slug,
            kind="buy_complete_set",
            edge=net_buy,
            gross_edge=gross_buy,
            sum_price=sum_asks,
            depth_usd=depth_buy,
            spread=spread,
            volume_24h=market.volume_24h,
            days_to_resolution=days,
            max_shares=max_shares_buy,
            rejected_by=_check(
                thresholds,
                edge=net_buy,
                depth_usd=depth_buy,
                volume_24h=market.volume_24h,
                spread=spread,
                days=days,
            ),
        )
    )

    # --- Vente du jeu complet ---
    sum_bids = sum(bids)
    gross_sell = sum_bids - COMPLETE_SET_PAYOUT
    net_sell = gross_sell - thresholds.cost_buffer
    max_shares_sell = min(bid_sizes)
    depth_sell = sum(price * size for price, size in zip(bids, bid_sizes))
    results.append(
        Opportunity(
            condition_id=market.condition_id,
            question=market.question,
            slug=market.slug,
            kind="sell_complete_set",
            edge=net_sell,
            gross_edge=gross_sell,
            sum_price=sum_bids,
            depth_usd=depth_sell,
            spread=spread,
            volume_24h=market.volume_24h,
            days_to_resolution=days,
            max_shares=max_shares_sell,
            rejected_by=_check(
                thresholds,
                edge=net_sell,
                depth_usd=depth_sell,
                volume_24h=market.volume_24h,
                spread=spread,
                days=days,
            ),
        )
    )

    return results


def scan_opportunities(
    markets: Sequence[Market],
    books: Mapping[str, Book],
    *,
    mode: Mode = Mode.SERIEUX,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Évalue tout l'univers et renvoie les seules opportunités retenues."""
    found: list[Opportunity] = []
    for market in markets:
        for opportunity in evaluate_market(market, books, mode=mode, now=now):
            if opportunity.is_actionable:
                found.append(opportunity)
    found.sort(key=lambda opp: opp.profit_at, reverse=True)
    return found


def affordable(opportunity: Opportunity, bankroll: float, min_order_size: float) -> bool:
    """Le capital disponible permet-il seulement d'entrer ?

    Polymarket impose une taille d'ordre minimale (souvent 5 parts). Avec un
    petit capital, beaucoup d'opportunités « visibles » sont hors de portée.
    """
    if bankroll <= 0 or min_order_size <= 0:
        return False
    cost_per_set = opportunity.sum_price
    return bankroll >= cost_per_set * min_order_size
