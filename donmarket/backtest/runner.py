"""Confronter le majorant à ce qui s'est réellement passé.

`analysis/rewards` soustrait au rendement une `drift` présentée comme un
MAJORANT du risque d'inventaire : elle suppose d'être rempli à fond du mauvais
côté et de porter 24 h. Le README en fait sa première limite assumée — « le net
réel est entre le chiffre affiché et le rendement brut ».

C'est une affirmation, pas une mesure. Ce module la teste : sur les mêmes
marchés, avec les mêmes paramètres de cotation, il rejoue la tenue de marché
minute par minute et compare le résultat de trading RÉALISÉ à celui que le
modèle SUPPOSE (`−drift`).

Deux issues, toutes deux utiles :

- le réalisé est au-dessus du supposé → le majorant tient, le net affiché est
  bien un plancher, et il est même trop pessimiste ;
- le réalisé est en dessous → le net affiché est FAUX dans le mauvais sens, et
  aucun candidat retenu jusqu'ici ne l'a été sur un chiffre valide.

Ce que ce module ne mesure toujours pas : les récompenses elles-mêmes. Le
carnet passé n'existe pas, donc la part du pool non plus (voir `replay`).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Sequence

from ..analysis.rewards import MIN_HOURS_LEFT, daily_pool, hours_until, path_stats
from ..analysis.scoring import quote_spread
from ..api import clob, gamma
from ..model import Market
from .replay import DEFAULT_MAX_INVENTORY, ReplayResult, replay_quotes

logger = logging.getLogger(__name__)

# Un appel d'historique par marché (piège n° 8) : ce curseur fixe la durée.
DEFAULT_MARKETS = 60

# Combien de marchés d'un même événement peuvent entrer dans l'échantillon.
#
# Le premier rejeu réel a rendu 4 marchés, dont 3 déclinaisons du même cessez-le-
# feu Israël/Iran. Ce n'était pas n = 4, c'était n ≈ 2 : trois marchés portés par
# la même dépêche bougent ensemble, et une médiane calculée dessus mesure une
# nouvelle, pas une stratégie. Les gros pools se concentrent justement sur
# l'actualité chaude, donc trier par pool sans plafonner reproduit le problème à
# chaque exécution.
MAX_PER_EVENT = 2

# En dessous de ce taux d'historiques obtenus, l'échantillon est amputé et le
# rapport doit le dire avant ses chiffres plutôt qu'après.
MIN_COVERAGE = 0.8

# En dessous de ce nombre de marchés rejoués, aucune médiane n'est lisible.
MIN_REPLAYS = 12

# Nombre de segments de slug qui définissent un événement. Quatre suffisent à
# rapprocher « israel-x-iran-ceasefire-continues-through-august-31 » et sa
# variante de septembre, sans confondre « us-x-iran-effective-ceasefire ».
# L'heuristique regroupe volontiers trop : écarter un marché réellement
# indépendant coûte moins cher que compter deux fois la même nouvelle.
EVENT_KEY_SEGMENTS = 4


def event_key(market: Market) -> str:
    """Clé de regroupement approximative des marchés d'un même événement.

    Le modèle Gamma ne porte pas d'identifiant d'événement, seulement un slug.
    On se rabat sur son préfixe, et sur la question quand le slug manque.
    """
    source = market.slug or market.question.lower().replace(" ", "-")
    segments = [part for part in source.split("-") if part]
    return "-".join(segments[:EVENT_KEY_SEGMENTS])


def diversified_head(markets: Sequence[Market], limit: int) -> list[Market]:
    """Prend les `limit` premiers marchés, sans plus de `MAX_PER_EVENT` par événement.

    L'ordre d'entrée est préservé : on garde les plus gros pools de chaque
    événement, on écarte seulement leurs doublons de rang inférieur.
    """
    seen: Counter[str] = Counter()
    head: list[Market] = []
    for market in markets:
        key = event_key(market)
        if seen[key] >= MAX_PER_EVENT:
            continue
        seen[key] += 1
        head.append(market)
        if len(head) >= limit:
            break
    return head


@dataclass(frozen=True)
class MarketReplay:
    """Un marché rejoué, avec les deux chiffres qui doivent être comparés."""

    condition_id: str
    question: str
    event_key: str
    half_spread: float
    size: float
    points: int
    result: ReplayResult
    assumed_pnl_pct: float  # −drift : ce que le modèle actuel suppose
    oscillation_pct: float

    @property
    def realized_pnl_pct(self) -> float:
        return self.result.pnl_pct

    @property
    def majorant_holds(self) -> bool:
        """Vrai si le réalisé est au moins aussi bon que le supposé."""
        return self.realized_pnl_pct >= self.assumed_pnl_pct

    @property
    def error(self) -> float:
        """De combien le modèle se trompe, en points de pourcentage par jour."""
        return self.realized_pnl_pct - self.assumed_pnl_pct


@dataclass(frozen=True)
class BacktestReport:
    """Le verdict, avec l'entonnoir qui dit sur quoi il porte."""

    markets_seen: int
    rewarded: int
    histories_fetched: int
    duration_seconds: float
    replays: tuple[MarketReplay, ...] = ()
    histories_requested: int = 0

    @property
    def replayed(self) -> int:
        return len(self.replays)

    @property
    def coverage(self) -> float:
        """Part des historiques demandés qui sont revenus, entre 0 et 1."""
        if self.histories_requested <= 0:
            return 0.0
        return self.histories_fetched / self.histories_requested

    @property
    def events_covered(self) -> int:
        """Nombre d'événements distincts dans l'échantillon rejoué.

        C'est la vraie taille d'échantillon : deux marchés du même cessez-le-feu
        ne sont pas deux observations indépendantes.
        """
        return len({replay.event_key for replay in self.replays})

    @property
    def sample_complaints(self) -> tuple[str, ...]:
        """Ce qui disqualifie l'échantillon — vide si rien ne le disqualifie.

        Un rapport qui ne sait pas dire qu'il est invalide se lit comme un
        rapport valide. Cette liste existe pour être imprimée AVANT les chiffres.
        """
        complaints: list[str] = []
        if self.histories_requested and self.coverage < MIN_COVERAGE:
            complaints.append(
                f"{self.histories_fetched}/{self.histories_requested} historiques "
                f"obtenus ({self.coverage:.0%}) : échantillon amputé, pas choisi"
            )
        if self.replayed < MIN_REPLAYS:
            complaints.append(
                f"{self.replayed} marchés rejoués (< {MIN_REPLAYS}) : "
                "aucune médiane n'est lisible à cette taille"
            )
        if self.replayed and self.events_covered < self.replayed / 2:
            complaints.append(
                f"{self.replayed} marchés pour seulement {self.events_covered} "
                "événements distincts : les observations ne sont pas indépendantes"
            )
        return tuple(complaints)

    @property
    def is_readable(self) -> bool:
        return not self.sample_complaints

    @property
    def active(self) -> tuple[MarketReplay, ...]:
        """Les marchés où la cotation a été remplie au moins une fois dans les deux sens.

        Ailleurs, le prix n'a jamais atteint nos cotes : « réalisé 0,00 » n'y
        veut pas dire que le majorant tient, mais qu'il n'a rien eu à majorer.
        Mélanger ces marchés aux autres fait dire au taux global l'inverse de
        ce qu'il mesure.
        """
        return tuple(r for r in self.replays if r.result.round_trips > 0)

    @property
    def active_holds_count(self) -> int:
        return sum(1 for r in self.active if r.majorant_holds)

    @property
    def median_error_active(self) -> float | None:
        if not self.active:
            return None
        return median(r.error for r in self.active)

    @property
    def median_realized(self) -> float | None:
        if not self.replays:
            return None
        return median(r.realized_pnl_pct for r in self.replays)

    @property
    def median_assumed(self) -> float | None:
        if not self.replays:
            return None
        return median(r.assumed_pnl_pct for r in self.replays)

    @property
    def majorant_holds_count(self) -> int:
        return sum(1 for r in self.replays if r.majorant_holds)

    @property
    def profitable_count(self) -> int:
        return sum(1 for r in self.replays if r.realized_pnl_pct > 0)

    @property
    def median_round_trips(self) -> float | None:
        if not self.replays:
            return None
        return median(r.result.round_trips for r in self.replays)


def replay_market(
    market: Market,
    prices: list[float],
    *,
    max_inventory: float = DEFAULT_MAX_INVENTORY,
) -> MarketReplay | None:
    """Rejoue un marché avec SES paramètres de récompense réels.

    Renvoie `None` si le marché ne porte pas de quoi coter : sans bande ni
    taille minimale, la stratégie n'a pas de forme sur ce marché et l'inclure
    reviendrait à rejouer une cotation inventée.
    """
    band_pct = market.rewards_max_spread
    size = market.rewards_min_size
    if not band_pct or not size or size <= 0 or len(prices) < 2:
        return None

    # Piège n° 6 : `rewardsMaxSpread` est en CENTS, pas en dollars.
    band = band_pct / 100.0
    half_spread = quote_spread(band)
    if half_spread <= 0:
        return None

    stats = path_stats(prices)
    if stats is None:
        return None

    result = replay_quotes(
        prices, half_spread=half_spread, size=size, max_inventory=max_inventory
    )
    return MarketReplay(
        condition_id=market.condition_id,
        question=market.question,
        event_key=event_key(market),
        half_spread=half_spread,
        size=size,
        points=len(prices),
        result=result,
        assumed_pnl_pct=-stats.drift,
        oscillation_pct=stats.oscillation,
    )


async def run_backtest(
    *,
    markets_limit: int = DEFAULT_MARKETS,
    max_inventory: float = DEFAULT_MAX_INVENTORY,
) -> BacktestReport:
    """Rejoue la stratégie sur les marchés récompensés aux plus gros pools.

    Le tri se fait sur le pool quotidien et non sur le rendement : le rendement
    dépend de la concurrence, donc du carnet du jour, alors qu'on cherche ici à
    mesurer un COÛT qui n'en dépend pas. Trier sur le rendement importerait le
    biais de sélection du scan dans une mesure censée le contrôler.
    """
    started = time.monotonic()
    now = datetime.now(timezone.utc)

    logger.info("Lecture de l'univers des marchés ouverts…")
    markets = await gamma.fetch_all_markets(closed=False)

    rewarded = [m for m in markets if m.has_rewards and daily_pool(m) > 0]
    alive = [
        m
        for m in rewarded
        if (hours_until(m.end_date, now=now) or 0.0) >= MIN_HOURS_LEFT
    ]
    alive.sort(key=daily_pool, reverse=True)
    head = diversified_head(alive, markets_limit)
    logger.info(
        "%d marchés lus → %d récompensés → %d vivants → %d retenus "
        "(%d événements, %d au plus par événement)",
        len(markets),
        len(rewarded),
        len(alive),
        len(head),
        len({event_key(market) for market in head}),
        MAX_PER_EVENT,
    )

    if not head:
        return BacktestReport(
            markets_seen=len(markets),
            rewarded=len(rewarded),
            histories_fetched=0,
            duration_seconds=time.monotonic() - started,
        )

    logger.info("Récupération de %d historiques (un appel chacun)…", len(head))
    histories = await clob.fetch_price_histories(
        [market.outcomes[0].token_id for market in head]
    )
    logger.info("%d historiques reçus", len(histories))

    replays: list[MarketReplay] = []
    for market in head:
        prices = histories.get(market.outcomes[0].token_id)
        if not prices:
            continue
        replay = replay_market(market, list(prices), max_inventory=max_inventory)
        if replay is not None:
            replays.append(replay)

    replays.sort(key=lambda r: r.error)
    return BacktestReport(
        markets_seen=len(markets),
        rewarded=len(rewarded),
        histories_fetched=len(histories),
        histories_requested=len(head),
        duration_seconds=time.monotonic() - started,
        replays=tuple(replays),
    )
