"""Client de lecture Predict.fun.

LECTURE SEULE. Passer un ordre exige une signature de portefeuille BNB Chain
(JWT) ; rien de tel n'est ici, et l'exécution ne sera pas branchée tant qu'un
portefeuille n'aura pas été fourni.

Trois défauts d'API mesurés le 2026-08-09 dictent la forme de ce module. Aucun
n'est théorique : chacun produit un chiffre faux plutôt qu'une erreur.

1. LE CURSEUR N'AVANCE JAMAIS (testnet). `/v1/markets` renvoie toujours le même
   `cursor` (« MjkwODQ= », base64 de « 29084 ») et les 20 mêmes marchés. Mesuré :
   30 pages demandées → 600 lignes → **20 marchés distincts**, 580 doublons.
   Une boucle « tant qu'il y a un curseur » tourne sans fin en croyant collecter.
   D'où l'arrêt dès qu'une page n'apporte aucun id neuf, et le signalement.

2. TOUS LES FILTRES SONT IGNORÉS. `tradingStatus`, `categorySlug`, `limit`,
   `offset`, `page`, `cursor` : le serveur renvoie 200 et le même contenu, y
   compris pour `tradingStatus=CHOUCROUTE`. Croire au filtre, c'est prendre
   14 marchés clos pour des marchés ouverts. On envoie quand même les paramètres
   (mainnet peut différer) et on refiltre TOUJOURS côté client, en signalant
   quand le serveur les a manifestement ignorés.

3. LE CARNET RÉPOND 404 SUR UN MARCHÉ CLOS. Ce n'est pas une panne, c'est
   l'absence de carnet : `fetch_book` rend None au lieu de lever.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from ..config import PREDICT_BASE_URLS, PREDICT_MAX_PAGES, PREDICT_PAGE_SIZE, SETTINGS
from .model import PredictBook, PredictMarket, PredictSchemaError, parse_book, parse_market

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)


class PredictApiError(RuntimeError):
    """Échec irrécupérable côté Predict.fun."""


@dataclass(frozen=True)
class MarketPage:
    """Le résultat d'un balayage, avec le compte-rendu de ses propres limites.

    `pagination_stalled` et `filter_ignored` ne sont pas décoratifs : sans eux,
    « 20 marchés » se lit comme « l'univers fait 20 marchés » alors que ça veut
    dire « l'API ne sait pas m'en montrer davantage ». La différence change la
    valeur de toute statistique calculée en aval.
    """

    markets: tuple[PredictMarket, ...]
    pages_fetched: int
    rows_received: int
    pagination_stalled: bool
    filter_ignored: bool

    @property
    def duplicate_rows(self) -> int:
        return self.rows_received - len(self.markets)

    def complaints(self) -> tuple[str, ...]:
        """Ce qu'il faut imprimer AVANT les chiffres, jamais après."""
        notes: list[str] = []
        if self.pagination_stalled:
            notes.append(
                f"pagination bloquée : {self.pages_fetched} pages demandées, "
                f"{self.rows_received} lignes reçues, seulement {len(self.markets)} "
                "marchés distincts — l'univers visible est plafonné par l'API, "
                "pas par le marché"
            )
        if self.filter_ignored:
            notes.append(
                "le serveur a ignoré le filtre de statut : le tri a été refait côté client"
            )
        return tuple(notes)


@dataclass(frozen=True)
class MarketStats:
    """`GET /v1/markets/{id}/stats` — endpoint séparé.

    À ne pas confondre avec le champ `stats` inline du marché, qui est resté
    `null` sur tout l'échantillon : seul l'endpoint dédié renvoie des chiffres.
    """

    market_id: int
    total_liquidity_usd: float | None
    volume_24h_usd: float | None
    volume_total_usd: float | None
    # Liquidité à moins de 3 cents du meilleur ask, telle que Predict.fun la
    # calcule. Nom d'origine `liquidity3cAskUsd` — la définition exacte n'est
    # pas documentée, on la conserve sans la réinterpréter.
    liquidity_3c_ask_usd: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _network_key() -> str:
    return (os.getenv("PREDICTFUN_NETWORK") or "testnet").strip().lower()


def _api_key() -> str | None:
    """Clé mainnet. Jamais journalisée, jamais persistée."""
    key = os.getenv("PREDICTFUN_API_KEY")
    return key.strip() if key and key.strip() else None


class PredictClient:
    """Client asynchrone de lecture. À utiliser en gestionnaire de contexte."""

    def __init__(
        self,
        *,
        network: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.network = (network or _network_key()).lower()
        if self.network not in PREDICT_BASE_URLS:
            raise ValueError(
                f"réseau inconnu {self.network!r} — attendu "
                f"{' ou '.join(PREDICT_BASE_URLS)}"
            )
        self.base_url = PREDICT_BASE_URLS[self.network]
        self._api_key = api_key if api_key is not None else _api_key()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def is_readable(self) -> bool:
        """Mainnet refuse toute lecture non authentifiée (401 vérifié le 2026-08-09)."""
        return self.network == "testnet" or bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "User-Agent": SETTINGS.user_agent}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    async def __aenter__(self) -> "PredictClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=SETTINGS.http_timeout,
            headers=self._headers(),
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("PredictClient doit être utilisé dans un `async with`")
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET avec réessais sur les statuts transitoires. 404 remonte tel quel."""
        client = self._require_client()
        last_error: Exception | None = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = await client.get(path, params=params)
                if response.status_code == 404:
                    raise httpx.HTTPStatusError(
                        "404", request=response.request, response=response
                    )
                if response.status_code == 401:
                    raise PredictApiError(
                        f"{path} : 401 — la lecture mainnet exige un en-tête "
                        "x-api-key (PREDICTFUN_API_KEY) ; le testnet n'en demande pas"
                    )
                if response.status_code in TRANSIENT_STATUS:
                    raise httpx.HTTPStatusError(
                        f"statut transitoire {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    raise
                last_error = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise PredictApiError(f"{path} a échoué après {RETRY_ATTEMPTS} essais") from last_error

    async def fetch_markets(
        self,
        *,
        max_pages: int = PREDICT_MAX_PAGES,
        trading_status: str | None = None,
    ) -> MarketPage:
        """Collecte les marchés en suivant le curseur, et s'arrête quand il piétine.

        La condition d'arrêt n'est PAS « plus de curseur » — sur testnet il y en
        a toujours un. C'est « cette page n'a apporté aucun id nouveau », seule
        condition qui termine face à un curseur qui ne bouge pas.
        """
        seen: dict[int, PredictMarket] = {}
        cursor: str | None = None
        rows_received = 0
        pages = 0
        stalled = False
        filter_ignored = False

        while pages < max_pages:
            params: dict[str, Any] = {"limit": PREDICT_PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            if trading_status:
                params["tradingStatus"] = trading_status

            payload = await self._get("/v1/markets", params)
            if not isinstance(payload, dict):
                raise PredictSchemaError("/v1/markets : objet attendu")
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise PredictSchemaError("/v1/markets : champ `data` (liste) attendu")

            pages += 1
            rows_received += len(rows)
            fresh = 0
            for row in rows:
                market = parse_market(row)
                if market.market_id not in seen:
                    seen[market.market_id] = market
                    fresh += 1
                if trading_status and market.trading_status != trading_status:
                    filter_ignored = True

            next_cursor = payload.get("cursor")
            if fresh == 0 and pages > 1:
                stalled = True
                break
            if not rows or not next_cursor:
                break
            if next_cursor == cursor:
                stalled = True
                break
            cursor = str(next_cursor)

        if pages >= max_pages and not stalled:
            logger.info(
                "arrêt au plafond de %d pages — il reste peut-être des marchés", max_pages
            )

        return MarketPage(
            markets=tuple(seen.values()),
            pages_fetched=pages,
            rows_received=rows_received,
            pagination_stalled=stalled,
            filter_ignored=filter_ignored,
        )

    async def fetch_book(self, market_id: int) -> PredictBook | None:
        """Carnet d'un marché. None si l'API répond 404 (marché sans carnet)."""
        try:
            payload = await self._get(f"/v1/markets/{market_id}/orderbook")
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.debug("marché %s : aucun carnet (404)", market_id)
                return None
            raise PredictApiError(f"carnet {market_id} : {exc}") from exc
        return parse_book(payload)

    async def fetch_stats(self, market_id: int) -> MarketStats | None:
        """Statistiques d'un marché. None si absentes."""
        try:
            payload = await self._get(f"/v1/markets/{market_id}/stats")
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise PredictApiError(f"stats {market_id} : {exc}") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None

        def num(key: str) -> float | None:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        return MarketStats(
            market_id=market_id,
            total_liquidity_usd=num("totalLiquidityUsd"),
            volume_24h_usd=num("volume24hUsd"),
            volume_total_usd=num("volumeTotalUsd"),
            liquidity_3c_ask_usd=num("liquidity3cAskUsd"),
            raw=data,
        )

    async def fetch_books(self, market_ids: Sequence[int]) -> dict[int, PredictBook]:
        """Carnets en parallèle borné.

        Contrairement au CLOB Polymarket, il n'existe AUCUN endpoint groupé :
        c'est une requête par marché. Le quota documenté est de 240 requêtes par
        minute, ce qui rend un balayage large coûteux — d'où la concurrence
        bornée par `DONMARKET_MAX_CONCURRENCY`.
        """
        unique = list(dict.fromkeys(market_ids))
        if not unique:
            return {}

        semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)

        async def run(market_id: int) -> tuple[int, PredictBook | None]:
            async with semaphore:
                try:
                    return market_id, await self.fetch_book(market_id)
                except (PredictApiError, PredictSchemaError) as exc:
                    logger.warning("carnet %s ignoré : %s", market_id, exc)
                    return market_id, None

        results = await asyncio.gather(*(run(mid) for mid in unique))
        books = {mid: book for mid, book in results if book is not None}

        missing = len(unique) - len(books)
        if missing:
            logger.info("%d carnets sur %d non obtenus", missing, len(unique))
        return books
