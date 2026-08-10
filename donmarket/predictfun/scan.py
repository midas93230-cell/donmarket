"""Balayage Predict.fun : ce qui est cotable, et ce que le rebate y vaut.

Ce scanner ne classe PAS par « rendement par jour ». Le rendement par jour du
côté teneur est indéterminé sur Predict.fun, et le dire est plus utile que de
l'estimer : la récompense se touche à l'exécution (voir `rebates.py`), et rien
dans l'API ne permet de mesurer un taux d'exécution — il n'existe ni endpoint de
transactions, ni historique de prix. Les deux techniques qui ont produit les
verdicts Polymarket (rejeu des exécutions réelles via `data-api/trades`, dérive
24 h via `/prices-history`) n'ont donc pas d'équivalent ici.

Ce que le scanner produit à la place, et qui est vrai :
  - la structure du carnet, lue sans hypothèse ;
  - le rebate exact par part exécutée à la cote actuelle ;
  - le mouvement adverse qui l'annule, pour que l'ordre de grandeur du risque
    soit affiché à côté de celui du gain ;
  - la vérification en direct que le jeu complet coûte bien ≥ 1 ;
  - des motifs de rejet explicites.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from .api import MarketPage, MarketStats, PredictClient
from .model import PredictBook, PredictMarket
from .rebates import (
    MIN_ORDER_USDT,
    breakeven_adverse_move,
    looks_rebate_eligible,
    maker_rebate_per_share,
    program_is_running,
    rebate_yield_on_filled_notional,
    taker_fee_per_share,
)


@dataclass(frozen=True)
class RebateCandidate:
    """Un marché cotable, et ce qu'on sait vraiment de sa rémunération."""

    market: PredictMarket
    book: PredictBook
    stats: MarketStats | None
    # Prix auquel on évalue le rebate : le milieu du carnet, c'est-à-dire là où
    # une cote symétrique serait posée.
    reference_price: float
    rebate_per_share: float
    rebate_yield_on_fill: float
    breakeven_move: float
    fee_per_share: float
    entry_ticket_usd: float
    rebate_eligible_guess: bool
    within_spread_threshold: bool

    @property
    def spread(self) -> float | None:
        return self.book.spread

    @property
    def full_set_ask_sum(self) -> float | None:
        return self.book.full_set_ask_sum

    @property
    def breakeven_ticks(self) -> float | None:
        """Le mouvement adverse d'équilibre, exprimé en pas de cotation.

        C'est la lecture la plus parlante : « le rebate est mangé par un
        mouvement de 0,25 pas » dit immédiatement que la marge est mince.
        """
        tick = self.market.tick_size
        return self.breakeven_move / tick if tick > 0 else None


@dataclass(frozen=True)
class Rejection:
    """Un marché écarté, avec la raison — jamais un silence."""

    market_id: int
    title: str
    reason: str


@dataclass(frozen=True)
class PredictScanResult:
    """Rapport de balayage. Les réserves se lisent AVANT les chiffres."""

    network: str
    page: MarketPage
    candidates: tuple[RebateCandidate, ...]
    rejections: tuple[Rejection, ...]
    program_running: bool
    scanned_at: date

    def complaints(self) -> tuple[str, ...]:
        """Tout ce qui limite la portée du rapport, y compris fatalement."""
        notes = list(self.page.complaints())
        if not self.program_running:
            notes.append(
                "l'essai de maker rebate est terminé (fin annoncée le 16/09/2026) : "
                "les rebates affichés valent zéro tant qu'aucune reconduction "
                "n'est publiée"
            )
        notes.append(
            "aucun rendement par jour n'est calculé : la récompense se touche à "
            "l'EXÉCUTION et l'API n'expose ni transactions ni historique de prix, "
            "donc le taux d'exécution n'est pas mesurable"
        )
        if self.candidates and not any(c.rebate_eligible_guess for c in self.candidates):
            notes.append(
                "aucun marché retenu ne ressemble à un marché crypto UP/DOWN — "
                "or ce sont les seuls éligibles au rebate au lancement"
            )
        return tuple(notes)


def evaluate_market(
    market: PredictMarket,
    book: PredictBook,
    stats: MarketStats | None = None,
) -> RebateCandidate | Rejection:
    """Juge un marché sur son carnet réel. Rend un candidat ou un rejet motivé."""
    if book.is_empty():
        return Rejection(market.market_id, market.title, "carnet vide")

    mid = book.midpoint
    if mid is None:
        side = "aucun bid" if book.best_yes_bid is None else "aucun ask"
        return Rejection(market.market_id, market.title, f"carnet unilatéral ({side})")

    spread = book.spread
    if spread is not None and spread < 0:
        return Rejection(
            market.market_id, market.title, f"carnet croisé (écart {spread:+.4f})"
        )

    fee_rate = market.fee_rate
    rebate = maker_rebate_per_share(mid, fee_rate=fee_rate)

    # Ticket d'entrée : la plateforme impose 1 USDT minimum ET, sur les marchés
    # à récompense en points, une taille minimale en PARTS (`shareThreshold`).
    # Les deux s'appliquent, on retient la contrainte la plus chère.
    share_threshold = market.share_threshold or 0.0
    entry_ticket = max(MIN_ORDER_USDT, share_threshold * mid)

    within = (
        market.spread_threshold is not None
        and spread is not None
        and spread <= market.spread_threshold
    )

    return RebateCandidate(
        market=market,
        book=book,
        stats=stats,
        reference_price=mid,
        rebate_per_share=rebate,
        rebate_yield_on_fill=rebate_yield_on_filled_notional(mid, fee_rate=fee_rate),
        breakeven_move=breakeven_adverse_move(mid, fee_rate=fee_rate),
        fee_per_share=taker_fee_per_share(mid, fee_rate=fee_rate),
        entry_ticket_usd=entry_ticket,
        rebate_eligible_guess=looks_rebate_eligible(
            market.category_slug, market.market_variant
        ),
        within_spread_threshold=within,
    )


async def scan_predictfun(
    *,
    network: str | None = None,
    bankroll: float | None = None,
    include_closed: bool = False,
    when: date | None = None,
) -> PredictScanResult:
    """Balaie l'univers visible, mesure les carnets, et rend un rapport honnête.

    `bankroll` ne sert qu'à écarter ce qui n'est pas finançable : le ticket
    d'entrée est une contrainte dure, pas un critère de classement.
    """
    async with PredictClient(network=network) as client:
        resolved_network = client.network
        page = await client.fetch_markets(trading_status="OPEN")

        tradable = [m for m in page.markets if include_closed or m.is_open]
        rejections: list[Rejection] = [
            Rejection(m.market_id, m.title, f"non négociable ({m.trading_status or '?'})")
            for m in page.markets
            if not (include_closed or m.is_open)
        ]

        books = await client.fetch_books([m.market_id for m in tradable])

        candidates: list[RebateCandidate] = []
        for market in tradable:
            book = books.get(market.market_id)
            if book is None:
                rejections.append(
                    Rejection(market.market_id, market.title, "aucun carnet servi (404)")
                )
                continue
            verdict = evaluate_market(market, book)
            if isinstance(verdict, Rejection):
                rejections.append(verdict)
                continue
            if bankroll is not None and verdict.entry_ticket_usd > bankroll:
                rejections.append(
                    Rejection(
                        market.market_id,
                        market.title,
                        f"ticket {verdict.entry_ticket_usd:.2f} $ > capital {bankroll:.2f} $",
                    )
                )
                continue
            candidates.append(verdict)

        # Les statistiques coûtent une requête par marché : on ne les paie que
        # pour ce qui a survécu au filtrage.
        enriched = [
            replace(candidate, stats=await client.fetch_stats(candidate.market.market_id))
            for candidate in candidates
        ]

    scanned_at = when or datetime.now(timezone.utc).date()
    # Tri par rebate par part : c'est la seule grandeur mesurée, donc la seule
    # sur laquelle un classement ne raconte pas d'histoire. Elle culmine au
    # milieu du carnet (p = 0,5) et s'effondre aux extrêmes.
    ordered = tuple(sorted(enriched, key=lambda c: c.rebate_per_share, reverse=True))

    return PredictScanResult(
        network=resolved_network,
        page=page,
        candidates=ordered,
        rejections=tuple(rejections),
        program_running=program_is_running(scanned_at),
        scanned_at=scanned_at,
    )
