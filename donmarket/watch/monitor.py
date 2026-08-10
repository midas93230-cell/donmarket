"""Le moniteur : un fil qui garde les carnets des candidats à jour en continu,
et qui accumule la moyenne temporelle de leur liquidité concurrente.

Il répond à la limite la plus coûteuse de la stratégie. Un balayage complet
dure 48 à 190 secondes et ne peut pas tourner en boucle sans marteler des API
publiques gratuites ; or la liquidité concurrente d'un pool bouge en secondes
(52 $ à 171 $ en 45 s, mesuré le 2026-07-29). Entre deux balayages, on est
aveugle exactement là où ça compte.

Le flux corrige ça pour un coût dérisoire : une seule connexion, et seulement
pour les jetons des candidats retenus — quelques dizaines, pas les 4 200 de
l'univers.

La moyenne, elle, corrige autre chose. Polymarket paie sur des relevés
échantillonnés tout au long de la journée : la valeur instantanée n'est qu'un
tirage. Le moniteur accumule donc, pour chaque candidat, la moyenne de sa
concurrence PONDÉRÉE PAR LE TEMPS (voir `average.py` pour pourquoi le temps et
non le nombre de messages).

Ce module est la frontière entre deux mondes : le flux est asynchrone, le
serveur HTTP est synchrone et multi-fils. La frontière est un verrou.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Mapping, Sequence

from ..analysis.scoring import competing_score
from ..api.clob import Book
from ..api.ws import stream_books
from .average import TimeAverage

logger = logging.getLogger(__name__)

StreamFactory = Callable[
    [Sequence[str]], AsyncIterator[tuple[dict[str, Book], tuple[str, ...]]]
]

# En deçà, la moyenne ne couvre pas assez de temps pour valoir mieux que
# l'instantané qu'elle prétend remplacer. La valeur vient de la mesure : les
# oscillations observées ont une période de l'ordre de la dizaine de secondes.
MIN_AVERAGE_SECONDS = 20.0


@dataclass(frozen=True)
class LiquidityProbe:
    """Ce qu'il faut savoir pour mesurer la concurrence d'un candidat.

    Le moniteur ignore ce qu'est un marché récompensé : il lui suffit de
    connaître les deux jetons du marché et la largeur de la bande qualifiante.
    L'ordre des jetons compte — c'est le milieu du PREMIER qui décide de la
    règle de plage dans `qmin` — et c'est celui du marché, donc YES puis NO.
    """

    key: str
    token_ids: tuple[str, ...]
    band: float


@dataclass(frozen=True)
class LiveSnapshot:
    """Les carnets tels qu'ils sont maintenant, plus de quoi juger leur âge.

    `updated_at` n'est pas décoratif : un carnet de dix minutes affiché comme
    « temps réel » est pire qu'un carnet absent, parce qu'il est cru.
    """

    books: dict[str, Book]
    updated_at: datetime | None
    updates: int
    tokens: tuple[str, ...]
    means: dict[str, float] = field(default_factory=dict)
    covered: dict[str, float] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float | None:
        if self.updated_at is None:
            return None
        return (datetime.now(timezone.utc) - self.updated_at).total_seconds()

    def trusted_mean(self, key: str) -> float | None:
        """La moyenne, ou None tant qu'elle ne couvre pas assez de temps."""
        if self.covered.get(key, 0.0) < MIN_AVERAGE_SECONDS:
            return None
        return self.means.get(key)


class LiveMonitor:
    """Suit un ensemble de candidats, et en change à chaque balayage."""

    def __init__(self, *, stream_factory: StreamFactory = stream_books) -> None:
        self._stream_factory = stream_factory
        self._lock = threading.Lock()
        self._books: dict[str, Book] = {}
        self._averages: dict[str, TimeAverage] = {}
        self._probes: tuple[LiquidityProbe, ...] = ()
        self._by_token: dict[str, list[LiquidityProbe]] = {}
        self._updated_at: datetime | None = None
        self._updates = 0
        self._tokens: tuple[str, ...] = ()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def snapshot(self) -> LiveSnapshot:
        now = time.monotonic()
        with self._lock:
            means: dict[str, float] = {}
            for key, average in self._averages.items():
                mean = average.mean_at(now)
                if mean is not None:
                    means[key] = mean
            return LiveSnapshot(
                books=dict(self._books),
                updated_at=self._updated_at,
                updates=self._updates,
                tokens=self._tokens,
                means=means,
                covered={
                    key: average.covered_seconds(now)
                    for key, average in self._averages.items()
                },
            )

    def watch(self, probes: Sequence[LiquidityProbe]) -> bool:
        """Reprend la surveillance sur un nouvel ensemble de candidats.

        Tout est jeté : carnets ET moyennes. Une moyenne accumulée sur les
        candidats du balayage précédent ne décrit pas ceux du nouveau, et la
        garder ferait vieillir silencieusement le seul chiffre sur lequel on
        décide.
        """
        self.stop()
        # Deux jetons au minimum : un marché à une seule branche n'a pas de
        # score calculable. Le laisser passer coûterait une place dans
        # l'abonnement au flux pour une moyenne qui ne s'accumulerait jamais,
        # et l'absence de moyenne se lit comme « pas encore assez de temps »
        # plutôt que comme « ne sera jamais mesurable ».
        kept = tuple(p for p in probes if len(p.token_ids) >= 2 and p.band > 0)
        tokens = tuple(dict.fromkeys(t for p in kept for t in p.token_ids))

        index: dict[str, list[LiquidityProbe]] = {}
        for probe in kept:
            for token_id in probe.token_ids:
                index.setdefault(token_id, []).append(probe)

        with self._lock:
            self._books = {}
            self._averages = {}
            self._probes = kept
            self._by_token = index
            self._updated_at = None
            self._updates = 0
            self._tokens = tokens

        if not tokens:
            return False

        self._thread = threading.Thread(
            target=self._run, args=(tokens,), name="donmarket-ws", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Coupe le fil en cours, s'il y en a un. Idempotent."""
        with self._lock:
            loop, task, thread = self._loop, self._task, self._thread
            self._loop = self._task = self._thread = None
        if loop is not None and task is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def _observe(
        self, books: Mapping[str, Book], touched: Sequence[str], now: float
    ) -> None:
        """Met à jour les moyennes des candidats touchés. Appelé sous verrou."""
        concerned = {
            probe.key: probe
            for token_id in touched
            for probe in self._by_token.get(token_id, ())
        }
        for key, probe in concerned.items():
            # `competing_score` prend les deux branches ENSEMBLE — les seaux de
            # Polymarket croisent les bids d'une branche avec les asks de
            # l'autre — et rend None dès qu'un carnet manque. Mieux vaut ne
            # rien observer que d'observer un pool à moitié lu : une moyenne
            # temporelle avale une valeur basse sans jamais la signaler.
            score = competing_score(books, probe.token_ids, probe.band)
            if score is None:
                continue
            self._averages[key] = self._averages.get(key, TimeAverage()).observe(
                score, at=now
            )

    def _run(self, tokens: tuple[str, ...]) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            task = loop.create_task(self._consume(tokens))
            with self._lock:
                self._loop, self._task = loop, task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            logger.debug("Surveillance arrêtée.")
        except Exception:  # un fil ne doit jamais mourir en silence
            logger.exception("La surveillance temps réel s'est arrêtée")
        finally:
            loop.close()

    async def _consume(self, tokens: tuple[str, ...]) -> None:
        stream = self._stream_factory(tokens)
        try:
            async for books, touched in stream:
                now = time.monotonic()
                with self._lock:
                    self._books = dict(books)
                    self._updated_at = datetime.now(timezone.utc)
                    self._updates += 1
                    self._observe(books, touched, now)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
