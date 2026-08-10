"""Client de LECTURE des marchés de prédiction Binance.

Aucun ordre ne part d'ici : le chemin d'écriture vit dans `trade.py` et il est
désarmé par défaut. Ce module ne fait que lire.

TROIS FAITS MESURÉS le 2026-08-09 contre `api.binance.com`, et chacun change
la façon d'écrire le client :

1. **AUCUNE ROUTE N'EST PUBLIQUE.** Sur le spot, `/api/v3/ticker/price` répond
   sans clé. Ici, `category/list` — de simples libellés de catégories — rend
   `-2014 API-key format invalid` sans en-tête, et `-2008 Invalid Api-Key ID`
   avec une clé bidon. Il n'y a donc pas de « mode dégradé sans clé » à
   proposer, et pas de testnet documenté pour ce produit. `is_readable` le dit
   franchement plutôt que de laisser un balayage vide passer pour un marché
   vide.

2. **LES ROUTES EXISTENT BIEN.** Contrôle indispensable, parce qu'une erreur
   d'authentification renvoyée sur une route inexistante ne prouverait rien :
   `/sapi/v1/w3w/wallet/prediction/choucroute/garnie` rend un **404** avec un
   corps de type Spring (`{"error":"Not Found","path":…}`), là où les 26
   chemins documentés rendent un code d'erreur Binance. Le 404 arrive donc
   AVANT le contrôle de clé, ce qui fait de ce contrôle une preuve d'existence.

3. **httpx SUFFIT.** Binance affirme qu'il faut descendre à `http.client` pour
   éviter le percent-encodage des crochets. Vérifié : `copy_with(raw_path=…)`
   transmet les octets intacts. Voir `signing.py` — tout passe par là, y
   compris les requêtes sans crochet, pour qu'il n'existe qu'un seul chemin de
   construction d'URL et donc une seule chose à vérifier.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import httpx

from ..config import (
    BINANCE_BASE,
    BINANCE_PREDICTION_PREFIX,
    BINANCE_RECV_WINDOW_MS,
    SETTINGS,
)
from .model import (
    ERROR_HINTS,
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionMarket,
    extract_rows,
    parse_book,
    parse_market,
)
from .signing import redact, signed_query

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

# Codes pour lesquels réessayer est une perte de temps : ils ne se corrigent
# pas en attendant. Réessayer trois fois une clé absente triple simplement le
# délai avant le message utile.
FATAL_CODES = frozenset({-1022, -2008, -2014, -2015, -31003})


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Credentials:
    """Clé et secret Binance. Ne jamais journaliser, ne jamais persister."""

    api_key: str
    api_secret: str

    @staticmethod
    def from_env() -> "Credentials | None":
        key = _env("BINANCE_API_KEY")
        secret = _env("BINANCE_API_SECRET")
        if not key or not secret:
            return None
        return Credentials(api_key=key, api_secret=secret)


class BinancePredictionClient:
    """Client asynchrone signé. À utiliser en gestionnaire de contexte."""

    def __init__(
        self,
        *,
        credentials: Credentials | None = None,
        base_url: str = BINANCE_BASE,
        recv_window_ms: int = BINANCE_RECV_WINDOW_MS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._credentials = (
            credentials if credentials is not None else Credentials.from_env()
        )
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def is_readable(self) -> bool:
        """MESURÉ : sans clé ET secret, aucune route ne répond, même en lecture."""
        return self._credentials is not None

    @property
    def missing_credentials(self) -> tuple[str, ...]:
        """Ce qu'il manque, nommément — plutôt qu'un « non configuré » opaque."""
        absent = []
        if not _env("BINANCE_API_KEY"):
            absent.append("BINANCE_API_KEY")
        if not _env("BINANCE_API_SECRET"):
            absent.append("BINANCE_API_SECRET")
        return tuple(absent)

    async def __aenter__(self) -> "BinancePredictionClient":
        headers = {"accept": "application/json", "User-Agent": SETTINGS.user_agent}
        if self._credentials is not None:
            headers["X-MBX-APIKEY"] = self._credentials.api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=SETTINGS.http_timeout,
            headers=headers,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require(self) -> tuple[httpx.AsyncClient, Credentials]:
        if self._client is None:
            raise RuntimeError(
                "BinancePredictionClient doit être utilisé dans un `async with`"
            )
        if self._credentials is None:
            raise BinanceApiError(
                "aucune clé Binance : "
                + ", ".join(self.missing_credentials or ("BINANCE_API_KEY",))
                + " manquante(s). MESURÉ : même les données de marché de "
                "prédiction exigent une clé signée — il n'y a pas de lecture "
                "anonyme à proposer",
                code=-2014,
            )
        return self._client, self._credentials

    def _clean(self, text: str) -> str:
        if self._credentials is None:
            return redact(text)
        return redact(text, self._credentials.api_key, self._credentials.api_secret)

    def _raise_for_payload(self, payload: Any, *, path: str) -> None:
        """Traduit `{"code": -1022, "msg": …}` en exception utile.

        Binance rend souvent HTTP 200 avec un corps d'erreur, ou HTTP 400 avec
        le même corps : c'est le CODE qui fait foi, pas le statut. Vérifier le
        statut seul laisserait passer des échecs pour des succès.
        """
        if not isinstance(payload, Mapping):
            return
        code = payload.get("code")
        if not isinstance(code, int) or code >= 0:
            return
        message = str(payload.get("msg") or "").strip()
        hint = ERROR_HINTS.get(code)
        detail = f"{path} : Binance {code} — {message}"
        if hint:
            detail += f"\n  → {hint}"
        raise BinanceApiError(self._clean(detail), code=code, path=path)

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        attempts: int = RETRY_ATTEMPTS,
    ) -> Any:
        """Requête signée, transmise octet pour octet telle qu'elle a été signée.

        Le chemin passe par `raw_path` et JAMAIS par le paramètre `params`
        d'httpx : sitôt qu'httpx réassemble la requête, il peut réencoder, et
        la signature ne couvre alors plus ce qui est envoyé.

        `attempts` vaut 1 pour toute écriture. Réessayer une lecture est
        gratuit ; réessayer un ordre après une coupure réseau peut le passer
        DEUX FOIS, puisqu'une requête perdue en chemin est indiscernable d'une
        requête reçue dont la réponse s'est perdue.
        """
        client, credentials = self._require()
        full_path = f"{BINANCE_PREDICTION_PREFIX}{path}"
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            # La signature est refaite à chaque essai : elle porte un
            # horodatage, et rejouer l'ancienne après une attente de plusieurs
            # secondes déclencherait -1021 au lieu de réussir.
            query = signed_query(
                params or {},
                secret=credentials.api_secret,
                recv_window_ms=self.recv_window_ms,
            )
            url = httpx.URL(self.base_url).copy_with(
                raw_path=f"{full_path}?{query}".encode("utf-8")
            )
            try:
                response = await client.request(method, url)
                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                if payload is not None:
                    self._raise_for_payload(payload, path=full_path)

                if response.status_code == 404:
                    raise BinanceApiError(
                        f"{full_path} : 404 — cette route n'existe pas côté "
                        "Binance (un 404 arrive AVANT le contrôle de clé)",
                        path=full_path,
                    )
                if response.status_code in TRANSIENT_STATUS:
                    raise httpx.HTTPStatusError(
                        f"statut transitoire {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return payload
            except BinanceApiError as exc:
                if exc.code in FATAL_CODES or exc.code is None:
                    raise
                last_error = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise BinanceApiError(
            self._clean(
                f"{full_path} a échoué après {attempts} essai(s) : {last_error}"
            ),
            path=full_path,
        ) from last_error

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params)

    async def post(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """POST signé — le seul point d'entrée en écriture, exposé pour `trade`.

        Tout part en chaîne de requête, corps vide : c'est la convention SAPI,
        et surtout c'est le SEUL chemin où la chaîne signée est aussi la chaîne
        transmise. Répartir les paramètres entre l'URL et un corps ouvrirait
        une seconde façon de casser la signature.

        UN SEUL ESSAI (`attempts=1`), là où `_get` en fait trois. Une écriture
        rejouée après une coupure réseau peut passer l'ordre deux fois : une
        requête perdue à l'aller est indiscernable d'une requête reçue dont la
        réponse s'est perdue. En cas de doute, c'est `active_orders()` qui
        tranche — une lecture, donc sans danger.
        """
        return await self._request("POST", path, params, attempts=1)

    # --- Données de marché -------------------------------------------------

    async def list_categories(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /category/list` — catégories L1 et L2."""
        payload = await self._get("/category/list")
        return extract_rows(payload, where="category/list")

    async def list_markets(
        self,
        *,
        page: int = 1,
        rows: int = 50,
        category: str | None = None,
        sort: str | None = None,
    ) -> tuple[PredictionMarket, ...]:
        """`GET /market/list` — liste paginée.

        Les noms de paramètres de pagination ne sont pas publiés. On envoie la
        convention SAPI habituelle (`page`/`rows`) ; si le serveur l'ignore,
        `collect_markets` le détecte par piétinement, comme côté Predict.fun,
        au lieu de boucler en croyant collecter.
        """
        params: dict[str, Any] = {"page": page, "rows": rows}
        if category:
            params["categorySlug"] = category
        if sort:
            params["sort"] = sort
        payload = await self._get("/market/list", params)
        return tuple(
            parse_market(row) for row in extract_rows(payload, where="market/list")
        )

    async def search_markets(self, keyword: str) -> tuple[PredictionMarket, ...]:
        """`GET /market/search` — recherche sémantique par mot-clé."""
        payload = await self._get("/market/search", {"keyword": keyword})
        return tuple(
            parse_market(row) for row in extract_rows(payload, where="market/search")
        )

    async def market_detail(self, market_id: int) -> Mapping[str, Any]:
        """`GET /market/detail` — détail d'un marché, variantes comprises."""
        payload = await self._get("/market/detail", {"marketId": market_id})
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return data if isinstance(data, Mapping) else payload
        raise BinanceSchemaError("market/detail : objet attendu")

    async def fetch_book(
        self, market_id: int, *, token_id: str | None = None
    ) -> PredictionBook:
        """`GET /order-book`.

        `token_id` est transmis quand il est fourni, parce que la doc REST
        parle d'un carnet « par outcome token » alors que le flux WebSocket est
        indexé par marché — discordance non tranchée, détaillée dans `model`.
        L'envoyer ne coûte rien ; supposer laquelle des deux lectures est la
        bonne coûterait un carnet lu à l'envers.
        """
        params: dict[str, Any] = {"marketId": market_id}
        if token_id:
            params["tokenId"] = token_id
        payload = await self._get("/order-book", params)
        return parse_book(payload, market_id=market_id)

    async def last_trade_price(self, market_id: int) -> float | None:
        """`GET /order-book/last-trade-price`. None si aucun échange."""
        payload = await self._get(
            "/order-book/last-trade-price", {"marketId": market_id}
        )
        source = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(source, Mapping):
            source = payload if isinstance(payload, Mapping) else {}
        for key in ("price", "lastTradePrice", "lastPrice"):
            value = source.get(key)
            if isinstance(value, (int, float, str)):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    async def fetch_books(
        self, market_ids: Sequence[int]
    ) -> dict[int, PredictionBook]:
        """Carnets en parallèle borné.

        Comme sur Predict.fun et contrairement au CLOB Polymarket, il n'existe
        AUCUN endpoint groupé : c'est une requête par marché. La concurrence
        est bornée par `DONMARKET_MAX_CONCURRENCY`, et un carnet illisible est
        signalé puis omis — pas transformé en carnet vide.
        """
        unique = list(dict.fromkeys(market_ids))
        if not unique:
            return {}

        semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)

        async def run(market_id: int) -> tuple[int, PredictionBook | None]:
            async with semaphore:
                try:
                    return market_id, await self.fetch_book(market_id)
                except (BinanceApiError, BinanceSchemaError) as exc:
                    logger.warning("carnet %s ignoré : %s", market_id, exc)
                    return market_id, None

        results = await asyncio.gather(*(run(mid) for mid in unique))
        books = {mid: book for mid, book in results if book is not None}

        missing = len(unique) - len(books)
        if missing:
            logger.info("%d carnets sur %d non obtenus", missing, len(unique))
        return books

    # --- Compte (lecture) --------------------------------------------------

    async def list_wallets(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /wallet/list` — portefeuilles de prédiction de l'utilisateur."""
        payload = await self._get("/wallet/list")
        return extract_rows(payload, where="wallet/list")

    async def portfolio(self) -> Mapping[str, Any]:
        """`GET /pnl/portfolio` — vue d'ensemble : positions, PnL agrégé."""
        payload = await self._get("/pnl/portfolio")
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return data if isinstance(data, Mapping) else payload
        raise BinanceSchemaError("pnl/portfolio : objet attendu")

    async def quota_status(self) -> Mapping[str, Any]:
        """`GET /quota/limit/status` — quota de négociation quotidien restant.

        À lire AVANT de bâtir un plan d'ordres : un quota épuisé fait échouer
        les ordres un par un, ce qui se lit comme une panne d'exécution.
        """
        payload = await self._get("/quota/limit/status")
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return data if isinstance(data, Mapping) else payload
        raise BinanceSchemaError("quota/limit/status : objet attendu")

    async def payment_option_balances(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /balance/payment-options` — soldes disponibles par moyen."""
        payload = await self._get("/balance/payment-options")
        return extract_rows(payload, where="balance/payment-options")

    async def active_orders(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /order/list` — ordres ouverts."""
        payload = await self._get("/order/list")
        return extract_rows(payload, where="order/list")

    async def order_history(self, **filters: Any) -> tuple[Mapping[str, Any], ...]:
        """`GET /order/history` — ordres de tous statuts."""
        payload = await self._get("/order/history", filters or None)
        return extract_rows(payload, where="order/history")

    async def positions(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /position/list` — positions en jetons."""
        payload = await self._get("/position/list")
        return extract_rows(payload, where="position/list")

    async def transfer_status(self, transfer_id: str) -> Mapping[str, Any]:
        """`GET /transfer/status`.

        PIÈGE DOCUMENTÉ (change-log du 2026-06-16) : l'état terminal de succès
        est `COMPLETED`, **pas** `SUCCESS`. Attendre `SUCCESS` fait boucler
        indéfiniment sur un transfert pourtant abouti. `PROCESSING` et
        `PENDING` sont intermédiaires, `FAILED` est terminal.
        """
        payload = await self._get("/transfer/status", {"transferId": transfer_id})
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return data if isinstance(data, Mapping) else payload
        raise BinanceSchemaError("transfer/status : objet attendu")


TERMINAL_TRANSFER_STATES = frozenset({"COMPLETED", "FAILED"})
PENDING_TRANSFER_STATES = frozenset({"PROCESSING", "PENDING"})
