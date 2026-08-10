"""Balayage des récompenses de liquidité — la seule stratégie encore vivante.

Le balayage se fait en DEUX PASSES, et ce n'est pas une optimisation
prématurée : c'est imposé par la forme des API.

Les carnets se demandent par lots (`POST /books`, des centaines de jetons par
requête) : on peut donc les prendre pour tout l'univers récompensé. Les
historiques de prix, eux, ne se groupent pas — c'est UN appel par jeton. Les
demander pour 450 marchés coûterait 450 requêtes pour n'en garder que quelques
dizaines.

D'où l'enchaînement :

1. Gamma donne l'univers ; on garde les marchés récompensés, à plus de 24 h de
   l'échéance, et dont le ticket d'entrée tient dans le capital.
2. Le CLOB donne tous les carnets d'un coup → rendement BRUT de chacun.
3. On ne paie l'historique que pour la tête de ce classement brut.
4. On reclasse cette tête au NET, seul chiffre qui décide.

Le biais est assumé et documenté : un marché à faible rendement brut mais à
dérive nulle ne remontera jamais, faute d'historique. C'est acceptable tant que
le brut plafonne le net — ce qui est vrai, la dérive étant toujours positive.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..analysis.opportunities import Mode
from ..analysis.rewards import (
    MIN_HOURS_LEFT,
    RewardCandidate,
    RewardThresholds,
    daily_pool,
    evaluate_reward_market,
    hours_until,
)
from ..api import clob, gamma
from ..model import Market
from ..store import db

logger = logging.getLogger(__name__)

# Nombre de marchés pour lesquels on paie un appel d'historique. Chaque unité
# coûte une requête : c'est le curseur entre exhaustivité et durée du scan.
HISTORY_BUDGET = 60


@dataclass(frozen=True)
class RewardScanResult:
    """Le compte rendu d'un balayage, entonnoir compris.

    Les compteurs intermédiaires ne sont pas décoratifs : quand un scan ne
    retient rien, ils disent OÙ l'univers s'est vidé — pas assez de marchés
    récompensés, échéances trop proches, ou tickets hors capital.
    """

    mode: Mode
    bankroll: float
    markets_seen: int
    rewarded: int
    alive: int
    affordable: int
    books_fetched: int
    histories_fetched: int
    duration_seconds: float
    candidates: tuple[RewardCandidate, ...] = ()
    near_misses: tuple[RewardCandidate, ...] = ()

    @property
    def found(self) -> int:
        return len(self.candidates)


def _select_universe(
    markets: list[Market], *, bankroll: float, now: datetime
) -> tuple[list[Market], int, int]:
    """Réduit l'univers aux marchés récompensés, vivants et finançables.

    Renvoie aussi les deux compteurs intermédiaires, pour que l'appelant puisse
    dire à quelle étape l'entonnoir s'est refermé.
    """
    rewarded = [m for m in markets if m.has_rewards and daily_pool(m) > 0]

    alive = []
    for market in rewarded:
        remaining = hours_until(market.end_date, now=now)
        if remaining is not None and remaining >= MIN_HOURS_LEFT:
            alive.append(market)

    affordable = [
        m for m in alive if m.rewards_min_size and m.rewards_min_size <= bankroll
    ]
    return affordable, len(rewarded), len(alive)


def _persist(result: RewardScanResult) -> None:
    """Enregistre le balayage. Un échec d'écriture ne perd pas la mesure."""
    try:
        with db.connect() as connection:
            db.record_reward_scan(connection, result)
    except Exception as exc:  # noqa: BLE001 — on journalise, on ne masque pas
        logger.error("Échec d'écriture en base (le scan reste valide) : %s", exc)


async def scan_rewards(
    *,
    mode: Mode = Mode.SERIEUX,
    bankroll: float,
    history_budget: int = HISTORY_BUDGET,
    near_miss_count: int = 10,
    persist: bool = True,
) -> RewardScanResult:
    """Balaie l'univers récompensé et classe les candidats au rendement NET."""
    started = time.monotonic()
    now = datetime.now(timezone.utc)

    logger.info("Lecture de l'univers des marchés ouverts…")
    markets = await gamma.fetch_all_markets(closed=False)
    universe, rewarded, alive = _select_universe(markets, bankroll=bankroll, now=now)
    logger.info(
        "%d marchés lus → %d récompensés → %d à plus de %.0f h → %d finançables à %.2f $",
        len(markets),
        rewarded,
        alive,
        MIN_HOURS_LEFT,
        len(universe),
        bankroll,
    )

    if not universe:
        # Un univers vide s'enregistre comme les autres : c'est l'entonnoir qui
        # s'est refermé, et savoir QUAND il se referme fait partie de la mesure.
        empty = RewardScanResult(
            mode=mode,
            bankroll=bankroll,
            markets_seen=len(markets),
            rewarded=rewarded,
            alive=alive,
            affordable=0,
            books_fetched=0,
            histories_fetched=0,
            duration_seconds=time.monotonic() - started,
        )
        if persist:
            _persist(empty)
        return empty

    token_ids = [token for market in universe for token in market.token_ids]
    logger.info("Récupération de %d carnets d'ordres…", len(token_ids))
    books = await clob.fetch_books(token_ids)
    logger.info("%d carnets reçus", len(books))

    # Passe 1 — rendement brut, sans historique : seuils volontairement ouverts
    # pour que le classement porte sur la mesure, pas sur la décision.
    survey = RewardThresholds(
        min_net_yield=float("-inf"),
        min_hours_left=MIN_HOURS_LEFT,
        max_engaged_usd=bankroll,
        require_history=False,
    )
    by_gross: list[tuple[float, Market]] = []
    for market in universe:
        candidate = evaluate_reward_market(
            market, books, prices=None, thresholds=survey, now=now
        )
        if candidate is not None:
            by_gross.append((candidate.gross_yield, market))
    by_gross.sort(key=lambda pair: pair[0], reverse=True)
    logger.info("%d marchés mesurables (deux carnets lisibles)", len(by_gross))

    # Passe 2 — l'historique, donc la dérive, donc le net.
    head = [market for _, market in by_gross[:history_budget]]
    logger.info("Récupération de %d historiques 24 h (un appel chacun)…", len(head))
    histories = await clob.fetch_price_histories(
        [market.outcomes[0].token_id for market in head]
    )
    logger.info("%d historiques reçus", len(histories))

    thresholds = RewardThresholds.for_mode(mode, bankroll=bankroll)
    actionable: list[RewardCandidate] = []
    rejected: list[RewardCandidate] = []
    for market in head:
        candidate = evaluate_reward_market(
            market,
            books,
            prices=histories.get(market.outcomes[0].token_id),
            thresholds=thresholds,
            now=now,
        )
        if candidate is None:
            continue
        (actionable if candidate.is_actionable else rejected).append(candidate)

    actionable.sort(key=lambda c: c.daily_usd, reverse=True)
    rejected.sort(key=lambda c: c.net_yield, reverse=True)

    result = RewardScanResult(
        mode=mode,
        bankroll=bankroll,
        markets_seen=len(markets),
        rewarded=rewarded,
        alive=alive,
        affordable=len(universe),
        books_fetched=len(books),
        histories_fetched=len(histories),
        duration_seconds=time.monotonic() - started,
        candidates=tuple(actionable),
        near_misses=tuple(rejected[:near_miss_count]),
    )

    if persist:
        _persist(result)
    return result
