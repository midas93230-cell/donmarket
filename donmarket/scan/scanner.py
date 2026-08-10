"""Balayage complet : lire tout Polymarket, puis mesurer.

L'enchaînement est délibéré :

1. Gamma donne l'univers des marchés ouverts (métadonnées, volumes).
2. On ne garde que les marchés réellement négociables.
3. Le CLOB donne les carnets réels, par lots parallèles.
4. Le moteur d'analyse mesure chaque marché.

L'étape 3 est la plus coûteuse : c'est elle qui justifie les requêtes groupées
et la concurrence bornée. Un scan doit rester une opération de quelques minutes,
pas d'une heure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..analysis.opportunities import Mode, Opportunity, evaluate_market
from ..api import clob, gamma
from ..model import Market
from ..store import db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    """Le compte rendu honnête d'un balayage : y compris quand il ne trouve rien."""

    mode: Mode
    markets_seen: int
    markets_tradable: int
    books_fetched: int
    duration_seconds: float
    opportunities: tuple[Opportunity, ...] = ()
    near_misses: tuple[Opportunity, ...] = ()
    markets: tuple[Market, ...] = field(default=(), repr=False, compare=False)

    @property
    def found(self) -> int:
        return len(self.opportunities)


async def full_scan(
    *,
    mode: Mode = Mode.SERIEUX,
    max_markets: int | None = None,
    near_miss_count: int = 10,
    persist: bool = True,
) -> ScanResult:
    """Lit tout l'univers, mesure, et journalise le résultat.

    `near_misses` conserve les meilleures opportunités REJETÉES, triées par marge
    brute. C'est délibéré : savoir de combien on rate une opportunité vaut mieux
    qu'un « rien trouvé » sans explication.
    """
    started = time.monotonic()

    logger.info("Lecture de l'univers des marchés ouverts…")
    markets = await gamma.fetch_all_markets(closed=False)
    markets_seen = len(markets)

    tradable = [market for market in markets if market.is_tradable]
    if max_markets is not None:
        # Les marchés arrivent triés par volume décroissant : tronquer garde
        # donc les plus actifs, ceux qui comptent réellement.
        tradable = tradable[:max_markets]
    logger.info("%d marchés lus, %d négociables", markets_seen, len(tradable))

    token_ids = [token for market in tradable for token in market.token_ids]
    logger.info("Récupération de %d carnets d'ordres…", len(token_ids))
    books = await clob.fetch_books(token_ids)
    logger.info("%d carnets reçus", len(books))

    actionable: list[Opportunity] = []
    rejected: list[Opportunity] = []
    for market in tradable:
        for opportunity in evaluate_market(market, books, mode=mode):
            if opportunity.is_actionable:
                actionable.append(opportunity)
            else:
                rejected.append(opportunity)

    actionable.sort(key=lambda opp: opp.profit_at, reverse=True)
    rejected.sort(key=lambda opp: opp.gross_edge, reverse=True)

    result = ScanResult(
        mode=mode,
        markets_seen=markets_seen,
        markets_tradable=len(tradable),
        books_fetched=len(books),
        duration_seconds=time.monotonic() - started,
        opportunities=tuple(actionable),
        near_misses=tuple(rejected[:near_miss_count]),
        markets=tuple(tradable),
    )

    if persist:
        _persist(result)
    return result


def _persist(result: ScanResult) -> None:
    """Enregistre le scan. Un échec d'écriture ne doit pas perdre le résultat."""
    try:
        with db.connect() as connection:
            scan_id = db.start_scan(connection, result.mode.value)
            db.upsert_markets(connection, result.markets)
            db.record_opportunities(connection, scan_id, result.opportunities)
            db.finish_scan(
                connection,
                scan_id,
                markets_seen=result.markets_seen,
                markets_traded=result.markets_tradable,
                books_fetched=result.books_fetched,
                found=result.found,
            )
    except Exception as exc:  # noqa: BLE001 — on journalise, on ne masque pas
        logger.error("Échec d'écriture en base (le scan reste valide) : %s", exc)
