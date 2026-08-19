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
    BINANCE_CLOCK_SKEW_WARN_MS,
    BINANCE_PREDICTION_PREFIX,
    BINANCE_RECV_WINDOW_MS,
    BINANCE_TIME_PATH,
    SETTINGS,
)
from .model import (
    ERROR_HINTS,
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionMarket,
    extract_rows,
    flatten_market_topics,
    parse_book,
    parse_market,
)
from .signing import now_ms, redact, signed_query

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

# Codes pour lesquels réessayer est une perte de temps : ils ne se corrigent
# pas en attendant. Réessayer trois fois une clé absente triple simplement le
# délai avant le message utile.
FATAL_CODES = frozenset({-1022, -2008, -2014, -2015, -31003})

# Horodatage hors fenêtre. Volontairement HORS de `FATAL_CODES` : c'est le seul
# code que le client sache réparer lui-même, en recalant son horloge sur celle
# du serveur. Le laisser fatal renverrait l'utilisateur inspecter sa clé, qui
# n'y est pour rien.
CLOCK_SKEW_CODE = -1021

# Binance ne tient pas ce marché : il revend Predict.fun. La valeur est écrite
# côté serveur dans chaque ligne de `/market/list` et `/order-book` l'exige en
# paramètre. Constante par défaut, mais la valeur de la ligne prime quand elle
# existe : le jour où Binance branche un second fournisseur, une constante en
# dur enverrait toutes les requêtes vers le mauvais.
DEFAULT_VENDOR = "PREDICT_FUN"


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
        # Le SECRET passe par le coffre : scellé (`dpapi:v1:…`) il doit être
        # descellé ici, sinon c'est la chaîne chiffrée qui sert de clé HMAC et
        # le serveur rend `-1022 Signature not valid` — une erreur qui accuse
        # le code de signature alors que la faute est au transport du secret.
        # Une valeur en clair ressort inchangée. Même discipline que
        # `execute/engine.py` et `builder/attribution.py`.
        from ..store.vault import read_secret

        key = _env("BINANCE_API_KEY")
        secret = read_secret("BINANCE_API_SECRET")
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
        # Écart à ajouter à l'horloge locale pour retomber sur celle de
        # Binance. Reste à 0 tant que rien ne le contredit : on ne paie un
        # aller-retour réseau que le jour où la signature est refusée.
        self._clock_offset_ms = 0
        self._clock_measured = False
        # Adresse du portefeuille de prédiction. Lue une fois, gardée : elle ne
        # change pas en cours de session, et cinq routes l'exigent.
        self._wallet_address: str | None = None
        # `walletId` est un champ DISTINCT de `walletAddress` dans
        # /wallet/list, et `place-order-bundle` exige les DEUX. Les confondre
        # rend exactement le même -3026 qu'un champ absent.
        self._wallet_id: str | None = None
        # Compte de paiement retenu pour les ordres. Lu une fois : il ne change
        # pas en cours de session, et `place-order-bundle` l'exige.
        self._account_type: str | None = None

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

    # --- Horloge -----------------------------------------------------------

    @property
    def clock_offset_ms(self) -> int:
        """Écart mesuré entre l'horloge locale et celle de Binance, en ms.

        Positif = le serveur est en avance sur nous ; négatif = nous sommes en
        avance sur lui, et c'est ce sens-là qui casse tout au-delà de 1 000 ms.
        """
        return self._clock_offset_ms

    def _timestamp_ms(self) -> int:
        """L'heure telle que Binance la voit, autant qu'on puisse la connaître."""
        return now_ms() + self._clock_offset_ms

    async def sync_clock(self) -> int:
        """Recale l'horodatage signé sur l'heure du serveur. Sans effet de bord.

        `/api/v3/time` est la SEULE route publique utile ici : elle répond sans
        signature, donc elle reste joignable exactement quand la signature est
        refusée. Le trajet aller-retour est retiré en prenant le point MILIEU
        de la mesure — sinon on impute au décalage d'horloge une latence qui
        n'en est pas.

        BEST-EFFORT ASSUMÉ : si l'heure serveur est illisible, l'écart connu
        est conservé et l'erreur d'origine remonte intacte. Masquer un `-1021`
        derrière un échec de synchronisation ferait disparaître le seul message
        qui dit à l'utilisateur de resynchroniser sa machine.
        """
        client, _ = self._require()
        avant = now_ms()
        try:
            response = await client.get(BINANCE_TIME_PATH)
            response.raise_for_status()
            payload = response.json()
            heure_serveur = int(payload["serverTime"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "heure serveur Binance illisible (%s) — écart d'horloge "
                "toujours estimé à %+d ms",
                self._clean(str(exc)),
                self._clock_offset_ms,
            )
            return self._clock_offset_ms

        apres = now_ms()
        milieu_local = (avant + apres) // 2
        self._clock_offset_ms = heure_serveur - milieu_local
        self._clock_measured = True

        if abs(self._clock_offset_ms) > BINANCE_CLOCK_SKEW_WARN_MS:
            logger.warning(
                "horloge locale décalée de %+d ms par rapport à Binance "
                "(aller-retour %d ms) — compensé à chaque requête. Cause "
                "racine : service w32time arrêté. Correctif durable, en "
                "PowerShell ADMINISTRATEUR : "
                "Set-Service w32time -StartupType Automatic; "
                "Start-Service w32time; w32tm /resync /force",
                -self._clock_offset_ms,
                apres - avant,
            )
        else:
            logger.info(
                "horloge alignée sur Binance à %+d ms près", self._clock_offset_ms
            )
        return self._clock_offset_ms

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

        # Budget d'essais, séparé du compteur : un rejeu déclenché par un
        # recalage d'horloge ne consomme PAS le budget ordinaire. Sans cette
        # séparation, une écriture (`attempts=1`) refusée pour horodatage
        # serait perdue alors même que le serveur l'a écartée avant de la lire.
        budget = attempts
        resynchronisations_restantes = 1
        attempt = 0

        while attempt < budget:
            attempt += 1
            # La signature est refaite à chaque essai : elle porte un
            # horodatage, et rejouer l'ancienne après une attente de plusieurs
            # secondes déclencherait -1021 au lieu de réussir.
            query = signed_query(
                params or {},
                secret=credentials.api_secret,
                recv_window_ms=self.recv_window_ms,
                timestamp_ms=self._timestamp_ms(),
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
                if exc.code == CLOCK_SKEW_CODE and resynchronisations_restantes:
                    # Le serveur a REFUSÉ la requête avant de la traiter : rien
                    # n'a été exécuté, donc la rejouer est sans danger, y
                    # compris sur une écriture. Une seule fois : si l'écart
                    # persiste après recalage, la cause n'est plus l'horloge et
                    # boucler ne ferait que retarder le message utile.
                    resynchronisations_restantes -= 1
                    ecart = await self.sync_clock()
                    logger.info(
                        "%s : horodatage refusé, horloge recalée de %+d ms puis rejeu",
                        full_path,
                        ecart,
                    )
                    last_error = exc
                    budget += 1
                    continue
                if exc.code in FATAL_CODES or exc.code is None:
                    raise
                last_error = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
            if attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        # Le code d'erreur d'origine est CONSERVÉ. Le perdre en cours de route
        # (ce que faisait la version précédente) rendait `-1021` indiscernable
        # d'une panne réseau pour l'appelant, alors que c'est précisément le
        # code qui porte le diagnostic.
        raise BinanceApiError(
            self._clean(
                f"{full_path} a échoué après {attempt} essai(s) : {last_error}"
            ),
            code=last_error.code if isinstance(last_error, BinanceApiError) else None,
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
        limit: int = 20,
        offset: int = 0,
        category: str | None = None,
        sort: str | None = None,
    ) -> tuple[PredictionMarket, ...]:
        """`GET /market/list` — liste paginée, aplatie en marchés négociables.

        PAGINATION MESURÉE le 2026-08-18, et c'était un piège silencieux :
        seuls `limit` et `offset` agissent. `page`/`rows` — ce que ce client
        envoyait — et `pageIndex`/`pageSize` sont acceptés (HTTP 200) puis
        IGNORÉS : on reçoit invariablement la même première page. Une collecte
        bâtie dessus piétine, et le diagnostic naturel (« le curseur n'avance
        pas », comme chez Predict.fun) est faux : c'est la requête qui est
        mal formée, pas le serveur qui refuse d'avancer.

        La réponse porte `total` et `hasMore` : la fin de collecte se lit donc
        dans la réponse, sans avoir à deviner par piétinement.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if category:
            params["categorySlug"] = category
        if sort:
            params["sort"] = sort
        payload = await self._get("/market/list", params)
        return tuple(
            parse_market(row)
            for row in flatten_market_topics(payload, where="market/list")
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
        self, market_id: int, *, token_id: str, vendor: str = DEFAULT_VENDOR
    ) -> PredictionBook:
        """`GET /order-book` — le carnet d'UNE branche.

        LES TROIS PARAMÈTRES SONT OBLIGATOIRES, mesuré le 2026-08-18 en les
        retirant un par un. Et le diagnostic est en escalier : sans `vendor`
        le serveur réclame `vendor` ; une fois ajouté il réclame `tokenId` ;
        une fois celui-ci fourni il réclame `marketId`. Chaque `-3026` ne
        nomme qu'un seul manquant, donc trois allers-retours sont nécessaires
        pour découvrir la signature complète — d'où ce commentaire plutôt
        qu'une redécouverte.

        `token_id` n'est plus optionnel comme dans la version précédente : le
        carnet est PAR BRANCHE. La discordance « par marché ou par jeton »
        laissée ouverte le 2026-08-09 est donc tranchée — c'est par jeton.
        """
        payload = await self._get(
            "/order-book",
            {"marketId": market_id, "tokenId": token_id, "vendor": vendor},
        )
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
        self, markets: Sequence[PredictionMarket]
    ) -> dict[str, PredictionBook]:
        """Tous les carnets de branche, en parallèle borné, indexés par JETON.

        INDEXÉ PAR JETON, PAS PAR MARCHÉ : un marché a deux branches et donc
        deux carnets. Les ranger par `market_id` — ce que faisait la version
        précédente — en écraserait un des deux en silence, et le carnet
        survivant passerait pour celui du marché entier.

        Il n'existe AUCUN endpoint groupé, comme sur Predict.fun et
        contrairement au CLOB Polymarket : c'est une requête par branche. Un
        carnet illisible est signalé puis omis, jamais transformé en carnet
        vide — un carnet vide se lirait « aucune liquidité », ce qui est une
        information, alors qu'on n'en a aucune.
        """
        taches: list[tuple[int, str, str]] = []
        vus: set[str] = set()
        for marche in markets:
            vendor = str(marche.raw.get("vendor") or DEFAULT_VENDOR)
            for token_id in marche.outcome_token_ids:
                if token_id in vus:
                    continue
                vus.add(token_id)
                taches.append((marche.market_id, token_id, vendor))
        if not taches:
            return {}

        semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)

        async def run(
            market_id: int, token_id: str, vendor: str
        ) -> tuple[str, PredictionBook | None]:
            async with semaphore:
                try:
                    return token_id, await self.fetch_book(
                        market_id, token_id=token_id, vendor=vendor
                    )
                except (BinanceApiError, BinanceSchemaError) as exc:
                    logger.warning(
                        "carnet %s/%s ignoré : %s", market_id, token_id[:12], exc
                    )
                    return token_id, None

        results = await asyncio.gather(*(run(*t) for t in taches))
        books = {tok: book for tok, book in results if book is not None}

        missing = len(taches) - len(books)
        if missing:
            logger.info("%d carnets sur %d non obtenus", missing, len(taches))
        return books

    # --- Compte (lecture) --------------------------------------------------

    async def list_wallets(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /wallet/list` — portefeuilles de prédiction de l'utilisateur."""
        payload = await self._get("/wallet/list")
        return extract_rows(payload, where="wallet/list")

    async def wallet_address(self) -> str:
        """L'adresse du portefeuille de prédiction, lue une fois puis gardée.

        MESURÉ le 2026-08-18 : `position/list`, `pnl/portfolio`, `order/list`,
        `order/history` et `trade/get-quote` l'exigent tous. Le client ne
        l'envoyait pas — ces cinq routes rendaient `-3026` et paraissaient
        cassées indépendamment, alors que le défaut était unique.

        Un compte SANS portefeuille n'est pas une panne : c'est une étape qui
        n'a pas été faite dans l'application. Le message le dit, plutôt que de
        renvoyer l'utilisateur inspecter sa clé d'API — piège dans lequel cette
        session est déjà tombée une fois.
        """
        if self._wallet_address is not None:
            return self._wallet_address

        wallets = await self.list_wallets()
        adresses = [
            str(w["walletAddress"]).strip()
            for w in wallets
            if str(w.get("walletAddress") or "").strip()
        ]
        if not adresses:
            raise BinanceApiError(
                "aucun portefeuille de prédiction sur ce compte. Il se crée "
                "dans l'application Binance (compte Prédiction) — aucune "
                "ligne de code ne remplace cette étape",
                path="/wallet/list",
            )
        if len(adresses) > 1:
            # On ne choisit PAS en silence : le solde et les positions
            # rapportés seraient ceux d'un portefeuille pris au hasard.
            logger.warning(
                "%d portefeuilles de prédiction — le premier est retenu (%s…). "
                "Les positions et le PnL rapportés ne concernent que celui-là",
                len(adresses),
                adresses[0][:10],
            )
        self._wallet_address = adresses[0]
        premier = next(
            w for w in wallets if str(w.get("walletAddress") or "").strip()
        )
        identifiant = str(premier.get("walletId") or "").strip()
        self._wallet_id = identifiant or None
        return self._wallet_address

    async def wallet_id(self) -> str:
        """L'identifiant du portefeuille — exigé par `place-order-bundle`.

        MESURÉ le 2026-08-19 : `place-order-bundle` accepte `orderType: LIMIT`
        et réclamait seulement ce champ de plus. C'est ce qui a montré que le
        chemin teneur existe, là où `get-quote` refusait le LIMIT tout court.

        Lu par le même appel que l'adresse : les deux vivent dans la même ligne
        de `/wallet/list`, et deux allers-retours pour une seule réponse serait
        du gaspillage sur un chemin déjà lent.
        """
        if self._wallet_id is None:
            await self.wallet_address()
        if not self._wallet_id:
            raise BinanceApiError(
                "portefeuille de prédiction sans `walletId` — "
                "`place-order-bundle` l'exige et rien ne le remplace",
                path="/wallet/list",
            )
        return self._wallet_id

    async def portfolio(self) -> Mapping[str, Any]:
        """`GET /pnl/portfolio` — vue d'ensemble : positions, PnL agrégé."""
        payload = await self._get(
            "/pnl/portfolio", {"walletAddress": await self.wallet_address()}
        )
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

    async def funding_account_type(self) -> str:
        """Le compte de paiement qui porte les fonds — exigé par les ordres.

        MESURÉ le 2026-08-19 : `place-order-bundle` réclame `accountType` après
        `timeInForce`. Cinquième marche d'un escalier où chaque `-3026` ne
        nomme qu'un paramètre à la fois.

        On le DÉDUIT des soldes plutôt que de l'écrire en dur. Une constante
        casserait le jour où le compte est financé depuis SPOT plutôt que
        CeDeFi, et le refus parlerait alors de solde insuffisant — sans dire
        que c'est le compte désigné qui n'était pas le bon. C'est exactement
        la méprise `POLYMARKET_FUNDER` / `POLYMARKET_ADDRESS`, transposée ici.
        """
        if self._account_type is not None:
            return self._account_type

        soldes = await self.payment_option_balances()
        meilleur: tuple[float, str] | None = None
        for ligne in soldes:
            if not ligne.get("enabled"):
                continue
            nom = str(ligne.get("accountType") or "").strip()
            if not nom:
                continue
            try:
                montant = float(ligne.get("availableBalanceDisplay") or 0)
            except (TypeError, ValueError):
                montant = 0.0
            if meilleur is None or montant > meilleur[0]:
                meilleur = (montant, nom)

        if meilleur is None:
            raise BinanceApiError(
                "aucun compte de paiement actif — impossible de désigner "
                "`accountType` pour un ordre",
                path="/balance/payment-options",
            )
        if meilleur[0] <= 0:
            logger.warning(
                "le compte de paiement retenu (%s) affiche un solde nul — "
                "les ordres seront refusés pour solde insuffisant",
                meilleur[1],
            )
        self._account_type = meilleur[1]
        return self._account_type

    async def payment_option_balances(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /balance/payment-options` — soldes disponibles par moyen."""
        payload = await self._get("/balance/payment-options")
        return extract_rows(payload, where="balance/payment-options")

    async def active_orders(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /order/list` — ordres ouverts."""
        payload = await self._get(
            "/order/list", {"walletAddress": await self.wallet_address()}
        )
        return extract_rows(payload, where="order/list")

    async def order_history(self, **filters: Any) -> tuple[Mapping[str, Any], ...]:
        """`GET /order/history` — ordres de tous statuts."""
        payload = await self._get(
            "/order/history",
            {"walletAddress": await self.wallet_address(), **filters},
        )
        return extract_rows(payload, where="order/history")

    async def positions(self) -> tuple[Mapping[str, Any], ...]:
        """`GET /position/list` — positions en jetons."""
        payload = await self._get(
            "/position/list", {"walletAddress": await self.wallet_address()}
        )
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
