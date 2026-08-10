"""Traduction d'un résultat de balayage en JSON, sans y ajouter de sens.

Un point mérite d'être dit ici plutôt que dans la page : le champ `held`.

`evaluate_reward_market` vérifie le ticket d'entrée marché par marché. Chaque
candidat tient donc dans le capital — pris isolément. Les afficher côte à côte
et additionner leurs gains laisse croire qu'on peut tous les prendre, ce qui
est faux dès que la somme des tickets dépasse le capital. C'est exactement
l'erreur qui a produit « +16,84 $/jour » sur 200 $ engagés pour 100 $
disponibles le 2026-07-28.

`allocate()` tranche, et `held` porte sa réponse jusqu'à l'écran : les lignes
retenues sont finançables ENSEMBLE, les autres ne le sont qu'à la place d'une
autre. Le total, lui, ne compte que les premières.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..analysis.rewards import (
    RewardCandidate,
    allocate,
    competing_from_books,
    with_competing,
)
from ..execute.orders import PlannedOrder, plan_portfolio
from ..scan.rewards_scan import RewardScanResult
from ..watch.monitor import LiveSnapshot


def order_payload(order: PlannedOrder) -> dict[str, Any]:
    """Un ordre planifié. Le motif du prix voyage avec lui, toujours."""
    return {
        "question": order.question,
        "token_id": order.token_id,
        "side": order.side,
        "price": order.price,
        "size": order.size,
        "notional": order.notional,
        "reason": order.reason,
    }


@dataclass(frozen=True)
class _Reading:
    """Les trois lectures d'un même candidat, et laquelle fait foi.

    Ordre de préférence, du plus fiable au moins fiable :

    1. la MOYENNE temporelle, quand le flux a couvert assez de temps — c'est
       la seule qui corresponde à ce que Polymarket paie réellement ;
    2. l'INSTANTANÉ du flux, en attendant que la moyenne soit crédible ;
    3. la valeur du BALAYAGE, quand le flux ne dit encore rien.

    Les trois sont exposées côte à côte plutôt que la seule retenue : c'est
    l'écart entre elles qui informe, pas leur valeur isolée.
    """

    scan: RewardCandidate
    instant: RewardCandidate
    decided: RewardCandidate
    averaged: bool
    average_seconds: float


def _read(
    candidate: RewardCandidate, live: LiveSnapshot | None
) -> _Reading:
    """Choisit la lecture qui fait foi pour un candidat."""
    if live is None:
        return _Reading(candidate, candidate, candidate, False, 0.0)

    instant_competing = competing_from_books(candidate, live.books)
    instant = (
        candidate
        if instant_competing is None
        else with_competing(candidate, instant_competing)
    )

    mean = live.trusted_mean(candidate.condition_id)
    covered = live.covered.get(candidate.condition_id, 0.0)
    if mean is None:
        return _Reading(candidate, instant, instant, False, covered)
    return _Reading(candidate, instant, with_competing(candidate, mean), True, covered)


def candidate_payload(reading: _Reading, *, held: bool) -> dict[str, Any]:
    """Une ligne du tableau. Le net qui fait foi d'abord, les autres à côté."""
    candidate = reading.decided
    return {
        "live": reading.instant is not reading.scan,
        "averaged": reading.averaged,
        "average_seconds": reading.average_seconds,
        "scan_net_yield": reading.scan.net_yield,
        "instant_net_yield": reading.instant.net_yield,
        "instant_competing_q": reading.instant.competing_q,
        "condition_id": candidate.condition_id,
        "question": candidate.question,
        "slug": candidate.slug,
        "net_yield": candidate.net_yield,
        "gross_yield": candidate.gross_yield,
        "drift": candidate.drift,
        "oscillation": candidate.oscillation,
        "daily_usd": candidate.daily_usd,
        "engaged_usd": candidate.engaged_usd,
        "daily_pool": candidate.daily_pool,
        # En POINTS de score, pas en dollars : deux carnets portant le même
        # capital marquent différemment selon leur distance au milieu. La page
        # affiche donc « pts », et notre propre score à côté — seul leur
        # rapport a un sens, un score isolé n'a pas d'échelle absolue.
        "competing_q": candidate.competing_q,
        "own_q": candidate.own_q,
        "hours_left": candidate.hours_left,
        "rejected_by": list(candidate.rejected_by),
        "held": held,
    }


def scan_payload(
    result: RewardScanResult, *, live: LiveSnapshot | None = None
) -> dict[str, Any]:
    """Le compte rendu complet : entonnoir, candidats, et portefeuille tenable.

    Quand des carnets frais sont disponibles, chaque candidat est RECALCULÉ
    dessus avant d'être classé et alloué. C'est délibéré : le total affiché
    doit décrire le marché de maintenant, pas celui d'il y a trois minutes.
    La valeur du balayage reste à côté, pour que le mouvement soit visible.
    """
    readings = [_read(candidate, live) for candidate in result.candidates]
    readings.sort(key=lambda r: r.decided.daily_usd, reverse=True)

    held = allocate([r.decided for r in readings], bankroll=result.bankroll)
    held_ids = {candidate.condition_id for candidate in held}

    # Le plan ne porte que sur les positions réellement tenables ensemble, et
    # se calcule sur les prix du carnet — pas sur l'approximation à 1 $ le jeu
    # complet qui sert au classement.
    plan = plan_portfolio(
        held, live.books if live else {}, bankroll=result.bankroll
    )

    return {
        "mode": result.mode.value,
        "bankroll": result.bankroll,
        "found": result.found,
        "duration_seconds": result.duration_seconds,
        "books_fetched": result.books_fetched,
        "histories_fetched": result.histories_fetched,
        # L'entonnoir n'est pas décoratif : quand rien ne sort, il dit à quelle
        # étape l'univers s'est vidé — pas assez de marchés récompensés,
        # échéances trop proches, ou tickets hors capital.
        "funnel": {
            "markets_seen": result.markets_seen,
            "rewarded": result.rewarded,
            "alive": result.alive,
            "affordable": result.affordable,
        },
        "portfolio": {
            "held_count": len(held),
            "engaged_usd": sum(c.engaged_usd for c in held),
            "daily_usd": sum(c.daily_usd for c in held),
        },
        # Ce que le bot POSERAIT, sur les carnets d'à l'instant. Rien n'est
        # envoyé : c'est là pour être lu avant que quoi que ce soit soit armé.
        "plan": {
            "orders": [order_payload(order) for order in plan.orders],
            "markets": plan.markets,
            "notional": plan.notional,
            "fits": plan.fits,
            "skipped": list(plan.skipped),
        },
        "live_count": sum(1 for r in readings if r.instant is not r.scan),
        "averaged_count": sum(1 for r in readings if r.averaged),
        "candidates": [
            candidate_payload(r, held=r.decided.condition_id in held_ids)
            for r in readings
        ],
        "near_misses": [
            candidate_payload(_read(c, None), held=False) for c in result.near_misses
        ],
    }
