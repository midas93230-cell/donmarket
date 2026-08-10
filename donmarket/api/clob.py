"""Client du CLOB — carnets d'ordres réels.

Les métadonnées Gamma donnent un `bestBid`/`bestAsk` mis en cache ; pour décider
d'un ordre il faut le carnet réel, avec sa profondeur. Cet endpoint accepte les
requêtes groupées, ce qui rend le balayage de milliers de tokens réaliste.

PIÈGE VÉRIFIÉ SUR L'API : le carnet renvoie les `bids` par prix CROISSANT et les
`asks` par prix DÉCROISSANT. Le meilleur prix est donc en fin de liste, pas au
début. On calcule les extrema explicitement plutôt que de se fier à l'ordre.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

from ..config import BOOKS_BATCH_SIZE, CLOB_BASE, SETTINGS
from ..model import to_float

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)


class ClobError(RuntimeError):
    """Échec irrécupérable côté CLOB."""


def _price(level: "Level") -> float:
    return level.price


@dataclass(frozen=True)
class Level:
    """Un palier du carnet : un prix et la taille disponible à ce prix."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        """Valeur en dollars de ce palier (prix × parts)."""
        return self.price * self.size


@dataclass(frozen=True)
class Book:
    """Le carnet d'ordres d'un token, normalisé et trié."""

    token_id: str
    bids: tuple[Level, ...]  # trié du meilleur (plus cher) au pire
    asks: tuple[Level, ...]  # trié du meilleur (moins cher) au pire
    tick_size: float | None
    min_order_size: float | None

    def __post_init__(self) -> None:
        """Impose l'ordre meilleur-en-premier, quelle que soit la provenance.

        Le tri vit ici et nulle part ailleurs : c'est la seule façon que
        `best_bid`, `depth_usd` et `cost_to_buy` disent la vérité même pour un
        carnet construit à la main. Lire `bids[0]` sur un carnet brut du CLOB
        donne le PIRE prix — l'erreur est silencieuse et produit des chiffres
        crédibles, donc on la rend impossible plutôt que de la documenter.
        """
        object.__setattr__(self, "bids", tuple(sorted(self.bids, key=_price, reverse=True)))
        object.__setattr__(self, "asks", tuple(sorted(self.asks, key=_price)))

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    def depth_usd(self, side: str, levels: int = 3) -> float:
        """Dollars disponibles sur les `levels` meilleurs paliers d'un côté."""
        book_side = self.bids if side == "bid" else self.asks
        return sum(level.notional for level in book_side[:levels])

    def cost_to_buy(self, shares: float) -> float | None:
        """Coût réel d'un achat au marché, en consommant le carnet palier par palier.

        Renvoie None si la profondeur est insuffisante — un bot qui ignore ça
        se croit riche d'un prix affiché qu'il ne peut pas obtenir.
        """
        if shares <= 0:
            return 0.0
        remaining = shares
        cost = 0.0
        for level in self.asks:
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
            if remaining <= 0:
                return cost
        return None


def _parse_levels(rows: Any) -> tuple[Level, ...]:
    """Normalise une liste de paliers. Le tri est la charge de `Book`."""
    levels: list[Level] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price = to_float(row.get("price"))
        size = to_float(row.get("size"))
        if price is None or size is None or size <= 0:
            continue
        levels.append(Level(price=price, size=size))
    return tuple(levels)


def parse_book(payload: dict[str, Any]) -> Book | None:
    """Normalise un carnet brut du CLOB."""
    token_id = payload.get("asset_id")
    if not isinstance(token_id, str) or not token_id:
        return None
    return Book(
        token_id=token_id,
        bids=_parse_levels(payload.get("bids")),
        asks=_parse_levels(payload.get("asks")),
        tick_size=to_float(payload.get("tick_size")),
        min_order_size=to_float(payload.get("min_order_size")),
    )


def build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=CLOB_BASE,
        timeout=SETTINGS.http_timeout,
        headers={"User-Agent": SETTINGS.user_agent, "Accept": "application/json"},
    )


async def _post_books(client: httpx.AsyncClient, token_ids: Sequence[str]) -> list[Book]:
    """Récupère un lot de carnets en une requête, avec réessais."""
    body = [{"token_id": token_id} for token_id in token_ids]
    last_error: Exception | None = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.post("/books", json=body)
            if response.status_code in TRANSIENT_STATUS:
                raise httpx.HTTPStatusError(
                    f"statut transitoire {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            payload = response.json()
            rows = payload if isinstance(payload, list) else payload.get("data", [])
            parsed = (parse_book(row) for row in rows if isinstance(row, dict))
            return [book for book in parsed if book is not None]
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == RETRY_ATTEMPTS:
                break
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise ClobError(
        f"POST /books a échoué après {RETRY_ATTEMPTS} essais ({len(token_ids)} tokens)"
    ) from last_error


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def fetch_books(
    token_ids: Sequence[str], *, batch_size: int = BOOKS_BATCH_SIZE
) -> dict[str, Book]:
    """Récupère les carnets de tous les tokens demandés, en parallèle borné.

    Un lot qui échoue est journalisé et ignoré : perdre 100 carnets sur 40 000
    ne doit pas faire tomber un scan complet.
    """
    unique_ids = list(dict.fromkeys(tid for tid in token_ids if tid))
    if not unique_ids:
        return {}

    semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)
    books: dict[str, Book] = {}

    async with build_client() as client:

        async def run_batch(batch: Sequence[str]) -> list[Book]:
            async with semaphore:
                try:
                    return await _post_books(client, batch)
                except ClobError as exc:
                    logger.warning("Lot de carnets ignoré : %s", exc)
                    return []

        tasks = [run_batch(batch) for batch in _chunks(unique_ids, batch_size)]
        for batch_result in await asyncio.gather(*tasks):
            for book in batch_result:
                books[book.token_id] = book

    missing = len(unique_ids) - len(books)
    if missing:
        logger.info("%d carnets sur %d non renvoyés par le CLOB", missing, len(unique_ids))
    return books


# Le carnet dit où sont les prix maintenant ; il ne dit rien du risque encouru
# à poster un ordre qui va rester en place. C'est l'historique qui le dit : la
# dérive du prix sur 24 h est la perte que subit un teneur rempli du mauvais
# côté (mesurée à 3,00 % médians du capital, contre 0,75 % de récompense — voir
# analysis/rewards.py). Sans cette série, la stratégie de récompenses est
# aveugle sur son propre risque.
HISTORY_INTERVAL = "1d"
HISTORY_FIDELITY = 1  # un point par minute


async def _get_history(client: httpx.AsyncClient, token_id: str) -> tuple[float, ...]:
    """Série de prix d'un token. Renvoie une série vide plutôt que d'échouer."""
    try:
        response = await client.get(
            "/prices-history",
            params={
                "market": token_id,
                "interval": HISTORY_INTERVAL,
                "fidelity": HISTORY_FIDELITY,
            },
        )
        response.raise_for_status()
        rows = response.json().get("history") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.debug("Historique indisponible pour %s : %s", token_id, exc)
        return ()

    prices = (to_float(row.get("p")) for row in rows if isinstance(row, dict))
    return tuple(price for price in prices if price is not None)


async def fetch_price_histories(token_ids: Sequence[str]) -> dict[str, tuple[float, ...]]:
    """Récupère les séries de prix 24 h, une requête par token, en parallèle borné.

    Contrairement aux carnets, cet endpoint ne groupe pas : il faut un appel par
    token. On ne l'appelle donc que sur une présélection, jamais sur l'univers.

    Une série manquante est SILENCIEUSE par construction : `_get_history` rend
    un tuple vide, que la compréhension finale écarte. Le 01/08/2026, un
    incident passager a fait tomber 56 séries sur 60 sans qu'aucune ligne ne le
    signale, et le backtest a rendu un verdict sur les 4 survivantes. D'où le
    décompte ci-dessous, en WARNING : perdre des séries reste tolérable,
    l'ignorer non.
    """
    unique_ids = list(dict.fromkeys(tid for tid in token_ids if tid))
    if not unique_ids:
        return {}

    semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)

    async with build_client() as client:

        async def run(token_id: str) -> tuple[str, tuple[float, ...]]:
            async with semaphore:
                return token_id, await _get_history(client, token_id)

        results = await asyncio.gather(*(run(tid) for tid in unique_ids))

    histories = {token_id: prices for token_id, prices in results if prices}

    missing = len(unique_ids) - len(histories)
    if missing:
        logger.warning(
            "%d historiques sur %d non obtenus — tout calcul en aval porte sur "
            "un échantillon amputé",
            missing,
            len(unique_ids),
        )
    return histories
