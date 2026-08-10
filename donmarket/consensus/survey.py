"""Mesure l'ensemble sur de vraies séries Polymarket.

Orchestration seulement : la logique est dans `ensemble.py` et
`diagnostics.py`, qui sont purs et testés. Ce module va chercher les données et
agrège les comptes rendus.

Le tableau des seuils est le cœur du résultat. Il ne dit pas si la méthode
gagne — il dit si elle DÉCIDE. Une méthode qui n'atteint jamais son seuil ne
peut ni gagner ni perdre : elle ne fait rien, et aucune performance annoncée ne
peut en venir.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Sequence

from ..analysis.rewards import daily_pool
from ..api import clob, gamma
from .diagnostics import analyse
from .ensemble import build_ensemble

logger = logging.getLogger(__name__)

# En dessous, une série ne couvre pas assez de temps pour que les horizons
# longs de l'ensemble (90 minutes) aient seulement de quoi se prononcer.
MIN_SERIES_POINTS = 120

# Seuils balayés pour montrer le compromis. Descendre jusqu'à 4 n'est pas une
# suggestion : c'est pour rendre visible l'endroit où la sélectivité s'effondre.
SWEPT_THRESHOLDS = (31, 28, 24, 20, 16, 12, 8, 6, 4)


@dataclass(frozen=True)
class ConsensusSurvey:
    """Ce que l'ensemble vaut, mesuré plutôt qu'annoncé."""

    members: int
    threshold: int
    markets: int
    duration_seconds: float
    correlations: tuple[float, ...] = ()
    effectives: tuple[float, ...] = ()
    decision_rates: tuple[float, ...] = ()
    by_threshold: dict[int, float] = field(default_factory=dict)

    @property
    def median_correlation(self) -> float | None:
        return statistics.median(self.correlations) if self.correlations else None

    @property
    def median_effective(self) -> float | None:
        return statistics.median(self.effectives) if self.effectives else None

    @property
    def median_decision_rate(self) -> float | None:
        return (
            statistics.median(self.decision_rates) if self.decision_rates else None
        )


async def survey_consensus(
    *, markets: int = 40, threshold: int = 28, step: int = 5, size: int = 31
) -> ConsensusSurvey:
    """Rejoue l'ensemble sur les `markets` marchés les plus actifs."""
    started = time.monotonic()

    universe = await gamma.fetch_all_markets(closed=False)
    lively = [
        m for m in universe if m.has_rewards and daily_pool(m) > 0 and m.token_ids
    ]
    lively.sort(key=lambda m: m.volume_24h, reverse=True)
    tokens = [m.outcomes[0].token_id for m in lively[:markets]]
    logger.info("%d marchés lus, historiques de %d d'entre eux…", len(universe), len(tokens))

    histories = await clob.fetch_price_histories(tokens)
    usable: list[Sequence[float]] = [
        prices for prices in histories.values() if len(prices) >= MIN_SERIES_POINTS
    ]
    logger.info("%d séries d'au moins %d points", len(usable), MIN_SERIES_POINTS)

    members = build_ensemble(size)
    correlations: list[float] = []
    effectives: list[float] = []
    rates: list[float] = []

    for prices in usable:
        report = analyse(members, prices, threshold=threshold, step=step)
        if report.mean_correlation is None or report.effective_members is None:
            continue
        correlations.append(report.mean_correlation)
        effectives.append(report.effective_members)
        rates.append(report.decision_rate)

    by_threshold: dict[int, float] = {}
    for swept in SWEPT_THRESHOLDS:
        if swept > size:
            continue
        taken = [
            analyse(members, prices, threshold=swept, step=step).decision_rate
            for prices in usable
        ]
        if taken:
            by_threshold[swept] = statistics.median(taken)

    return ConsensusSurvey(
        members=size,
        threshold=threshold,
        markets=len(usable),
        duration_seconds=time.monotonic() - started,
        correlations=tuple(correlations),
        effectives=tuple(effectives),
        decision_rates=tuple(rates),
        by_threshold=by_threshold,
    )
