"""Modèle de données Prediction Trading (Binance). Module PUR.

CE QUI EST DOCUMENTÉ ET CE QUI NE L'EST PAS — à lire avant de faire confiance
à une valeur qui sort d'ici.

Binance publie deux niveaux de détail très inégaux, et l'écart dicte la forme
de ce module :

  - Le **payload WebSocket du carnet est spécifié champ par champ**
    (`/products/w3w-prediction/websocket-api/orderbook`) : `msgType`,
    `marketId`, `updateTimestampMs`, `asks`, `bids`, paliers en paires de
    chaînes `["0.32", "500"]`, asks CROISSANTS et bids DÉCROISSANTS, tailles
    strictement positives. `parse_book` s'appuie là-dessus et rien d'autre.
  - Les **réponses REST ne sont publiées nulle part** : l'index officiel
    (`llms-full.txt`) ne donne que le chemin, le verbe, une phrase et un
    `operationId` pour les 26 routes. Aucun nom de champ.

Conséquence : `parse_market` ne peut PAS être une transcription de schéma.
Il est écrit comme un lecteur défensif — il exige le strict nécessaire
(un identifiant), essaie plusieurs noms plausibles pour le reste, conserve la
charge brute, et **déclare** ce qu'il n'a pas su lire au lieu de combler par
des valeurs par défaut. Un champ absent ressort `None`, jamais 0 : un 0
inventé se propage en calcul de rendement et devient un chiffre faux.

DISCORDANCE NON RÉSOLUE, et elle compte pour la stratégie. La doc REST décrit
`order-book` comme « the order book for a specific prediction market **outcome
token** » (donc un carnet par branche), tandis que le flux WebSocket est
indexé par `marketId` et ne porte qu'un seul couple bids/asks (donc un carnet
par marché). Le fournisseur étant `predict_fun` — dont il est MESURÉ qu'il n'a
qu'un carnet par marché, le côté No étant dérivé par `no_ask = 1 − yes_bid` —
la lecture WebSocket est la plus cohérente. Mais ce n'est pas tranché sans
clé : tant que ça ne l'est pas, `PredictionBook.side_convention` vaut
`"inconnue"` et rien en aval n'a le droit de sommer deux branches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# Un jeu complet vaut 1 USDT à la résolution, comme partout ailleurs.
FULL_SET_USDT = 1.0

# Valeur fixe du champ `msgType` du flux carnet, d'après la doc.
ORDERBOOK_MSG_TYPE = "orderbook"


class BinanceSchemaError(ValueError):
    """La réponse n'a pas la forme documentée.

    Existe pour la même raison que `PredictSchemaError` côté Predict.fun, et
    la leçon vient du même endroit : le parseur de carnet Polymarket ignore en
    silence tout palier qui n'est pas un dictionnaire. Ici les paliers sont des
    PAIRES ; un parseur permissif rendrait donc un carnet VIDE sans une ligne
    d'erreur, et l'aval conclurait « aucune liquidité » au lieu de « je ne sais
    pas lire ». On échoue bruyamment.
    """


class BinanceApiError(RuntimeError):
    """Refus côté Binance, porteur de son code d'erreur.

    Les codes SAPI ne se paraphrasent pas : `-1022` et `-1021` mènent à des
    corrections opposées (chaîne signée mal formée / horloge décalée), et le
    message brut de Binance ne dit ni l'un ni l'autre en clair.
    """

    def __init__(
        self, message: str, *, code: int | None = None, path: str = ""
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


# Traductions des codes rencontrés ou documentés. Ce n'est pas de la
# décoration : chacune dit quelle action corrige l'erreur, ce que le message
# d'origine ne fait pas.
ERROR_HINTS: dict[int, str] = {
    -1021: (
        "horloge de la machine décalée de plus de recvWindow — resynchroniser "
        "l'heure système, ce n'est pas un problème de clé"
    ),
    -1022: (
        "signature invalide. Cause n°1 documentée : les crochets des clés "
        "indexées (cancelInfoList[0]…) ont été percent-encodés après signature. "
        "DONmarket transmet via raw_path pour l'éviter — si l'erreur survient "
        "quand même, la chaîne signée et la chaîne envoyée ont divergé ailleurs"
    ),
    -1121: "identifiant de marché inconnu du serveur",
    -2008: "clé d'API inconnue — vérifier BINANCE_API_KEY",
    -2014: (
        "en-tête X-MBX-APIKEY absent ou mal formé. MESURÉ : aucune route "
        "prédiction n'est publique, pas même les données de marché"
    ),
    -2015: (
        "clé rejetée : permission manquante, IP non autorisée, ou permission "
        "« Prediction Trading » non activée sur la page de gestion des clés"
    ),
    -31003: (
        "autorisation SAS absente. Les routes TRADE (ordre, annulation, "
        "transfert, rachat) l'exigent : elle s'active dans l'application "
        "Binance, pas par l'API"
    ),
}


def _as_float(value: Any, *, where: str) -> float:
    """Convertit en flottant ou lève. Aucune valeur par défaut silencieuse.

    La doc précise que prix et tailles voyagent en CHAÎNES, pour éviter la
    perte de précision en JavaScript. On accepte donc les deux formes, mais on
    refuse `None`, `True` et tout ce qui n'est pas un nombre lisible.
    """
    if value is None or isinstance(value, bool):
        raise BinanceSchemaError(f"{where} : nombre attendu, reçu {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise BinanceSchemaError(
                f"{where} : nombre attendu, reçu {value!r}"
            ) from exc
    raise BinanceSchemaError(f"{where} : nombre attendu, reçu {type(value).__name__}")


def _opt_float(payload: Mapping[str, Any], *names: str) -> float | None:
    """Premier champ lisible parmi plusieurs noms plausibles, sinon None.

    None signifie « le serveur ne me l'a pas donné », ce qui n'est pas 0.
    """
    for name in names:
        if name in payload and payload[name] is not None:
            try:
                return _as_float(payload[name], where=name)
            except BinanceSchemaError:
                continue
    return None


def _opt_str(payload: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _opt_int(payload: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


@dataclass(frozen=True)
class PredictionLevel:
    """Un palier : un prix unitaire en USDT et une taille en PARTS."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        """USDT immobilisés par ce palier."""
        return self.price * self.size


def parse_levels(rows: Any, *, where: str) -> tuple[PredictionLevel, ...]:
    """Lit une liste de paliers `[["0.32","500"], …]`.

    La doc garantit `size > 0` (« upstream filters empty levels ») : on ne
    déduit donc pas d'une taille nulle qu'il s'agit d'une suppression de palier
    — ce serait importer la sémantique du WebSocket Polymarket, où `size = 0`
    veut dire « ce palier disparaît ». Ici une taille nulle est une ANOMALIE de
    schéma, et on lève.
    """
    if rows is None:
        return ()
    if not isinstance(rows, (list, tuple)):
        raise BinanceSchemaError(f"{where} : liste de paliers attendue")

    levels: list[PredictionLevel] = []
    for index, row in enumerate(rows):
        spot = f"{where}[{index}]"
        if isinstance(row, Mapping):
            # Forme non documentée pour ce produit. On l'accepte plutôt que de
            # rendre un carnet vide, mais sans la présenter comme attendue.
            price = _as_float(row.get("price"), where=f"{spot}.price")
            size = _as_float(
                row.get("size", row.get("quantity")), where=f"{spot}.size"
            )
        elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
            if len(row) < 2:
                raise BinanceSchemaError(
                    f"{spot} : paire [prix, taille] attendue, reçu {row!r}"
                )
            price = _as_float(row[0], where=f"{spot}[0]")
            size = _as_float(row[1], where=f"{spot}[1]")
        else:
            raise BinanceSchemaError(
                f"{spot} : paire [prix, taille] attendue, reçu {type(row).__name__}"
            )

        if size <= 0:
            raise BinanceSchemaError(
                f"{spot} : taille {size} — la doc garantit des tailles > 0, "
                "un palier vide signale un schéma qui a changé"
            )
        if not 0.0 < price < 1.0:
            raise BinanceSchemaError(
                f"{spot} : prix {price} hors de (0, 1) — un prix de marché de "
                "prédiction est une probabilité"
            )
        levels.append(PredictionLevel(price=price, size=size))
    return tuple(levels)


@dataclass(frozen=True)
class PredictionBook:
    """Carnet d'un marché, meilleur prix EN PREMIER.

    Ordre : bids décroissants, asks croissants — donc `bids[0]` et `asks[0]`
    sont les meilleurs. C'est l'inverse de Polymarket (meilleur en DERNIER) et
    c'est conforme à Predict.fun. Cette inversion a déjà coûté cher une fois :
    l'invariant est imposé dans `__post_init__` et non dans le parseur, pour
    qu'un carnet construit à la main — test, cache relu, backtest — ne puisse
    pas présenter le PIRE prix comme le meilleur sans lever d'erreur.
    """

    market_id: int
    bids: tuple[PredictionLevel, ...] = ()
    asks: tuple[PredictionLevel, ...] = ()
    updated_ms: int | None = None
    token_id: str | None = None
    # Tant qu'aucune clé n'a permis de trancher la discordance REST/WebSocket
    # décrite en tête de module, on refuse de prétendre savoir si ce carnet
    # couvre le marché entier ou une seule branche.
    side_convention: str = "inconnue"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bids",
            tuple(sorted(self.bids, key=lambda level: level.price, reverse=True)),
        )
        object.__setattr__(
            self, "asks", tuple(sorted(self.asks, key=lambda level: level.price))
        )

    @property
    def best_bid(self) -> PredictionLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> PredictionLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> float | None:
        """Écart d'une MÊME branche (ask − bid), jamais entre branches.

        Le rappel n'est pas superflu : calculer l'écart entre branches a déjà
        produit 0,96 sur un carnet serré à 0,001 côté Polymarket.
        """
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask.price - self.best_bid.price

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2.0

    @property
    def is_two_sided(self) -> bool:
        return bool(self.bids and self.asks)


def parse_book(payload: Any, *, market_id: int | None = None) -> PredictionBook:
    """Lit un carnet, qu'il vienne du REST ou du flux WebSocket.

    Le flux enveloppe la charge utile dans une CHAÎNE JSON (champ `data` du
    message `TOPIC`) : c'est à l'appelant de faire ce premier `json.loads`,
    documenté comme piège par Binance (« Requires a second JSON.parse »).
    Ici on accepte la charge nue, ou une enveloppe `{"data": {…}}` telle que
    SAPI en produit habituellement.
    """
    if isinstance(payload, Mapping) and "bids" not in payload and "asks" not in payload:
        inner = payload.get("data")
        if isinstance(inner, Mapping):
            payload = inner

    if not isinstance(payload, Mapping):
        raise BinanceSchemaError("carnet : objet attendu")

    msg_type = payload.get("msgType")
    if msg_type is not None and msg_type != ORDERBOOK_MSG_TYPE:
        raise BinanceSchemaError(
            f"carnet : msgType {msg_type!r} — {ORDERBOOK_MSG_TYPE!r} attendu"
        )

    resolved = _opt_int(payload, "marketId", "market_id", "id")
    if resolved is None:
        resolved = market_id
    if resolved is None:
        raise BinanceSchemaError(
            "carnet : `marketId` absent et aucun fourni par l'appelant"
        )

    return PredictionBook(
        market_id=resolved,
        bids=parse_levels(payload.get("bids"), where="bids"),
        asks=parse_levels(payload.get("asks"), where="asks"),
        # `timestamp` est le nom du REST (mesuré 2026-08-18) ;
        # `updateTimestampMs` celui du flux WebSocket. Les deux coexistent.
        updated_ms=_opt_int(
            payload, "timestamp", "updateTimestampMs", "updateTime", "time"
        ),
        token_id=_opt_str(payload, "tokenId", "outcomeTokenId", "token"),
        raw=dict(payload),
    )


@dataclass(frozen=True)
class PredictionMarket:
    """Un marché, lu défensivement faute de schéma REST publié.

    Seul `market_id` est exigé. Tout le reste peut valoir `None`, et
    `unread_fields` dit lesquels — c'est ce qui permet au rapport d'annoncer
    « je n'ai pas su lire le titre » plutôt que d'afficher une ligne vide qui
    ressemble à une donnée.
    """

    market_id: int
    title: str | None = None
    category: str | None = None
    status: str | None = None
    end_time_ms: int | None = None
    volume_usdt: float | None = None
    liquidity_usdt: float | None = None
    fee_rate_bps: int | None = None
    outcome_token_ids: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def unread_fields(self) -> tuple[str, ...]:
        missing = []
        if self.title is None:
            missing.append("titre")
        if self.status is None:
            missing.append("statut")
        if self.end_time_ms is None:
            missing.append("échéance")
        if self.fee_rate_bps is None:
            missing.append("taux de frais")
        return tuple(missing)


def _collect_token_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Ramasse les identifiants de branche, quelle que soit leur enveloppe."""
    for key in ("outcomeTokenIds", "tokenIds", "outcomes", "variants"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return (value.strip(),)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            found: list[str] = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    found.append(item.strip())
                elif isinstance(item, Mapping):
                    token = _opt_str(item, "tokenId", "outcomeTokenId", "id")
                    if token:
                        found.append(token)
            if found:
                return tuple(found)
    return ()


def parse_market(payload: Any) -> PredictionMarket:
    """Lit un marché. Lève si aucun identifiant n'est trouvable.

    Un marché sans identifiant n'est pas un marché dégradé, c'est une ligne
    dont on ne pourra jamais demander le carnet : le laisser passer
    remplirait le classement d'entrées inertes.
    """
    if not isinstance(payload, Mapping):
        raise BinanceSchemaError("marché : objet attendu")

    market_id = _opt_int(payload, "marketId", "id", "topicId", "market_id")
    if market_id is None:
        raise BinanceSchemaError(
            "marché sans identifiant lisible (essayés : marketId, id, topicId)"
        )

    return PredictionMarket(
        market_id=market_id,
        title=_opt_str(payload, "title", "topic", "question", "name"),
        category=_opt_str(payload, "categorySlug", "category", "categoryName"),
        # ORDRE CORRIGÉ le 2026-08-19, mesuré sur 241 marchés : un marché porte
        # les DEUX champs. `status` vaut `REGISTERED` (état de cycle de vie,
        # identique sur les 241) tandis que `tradingStatus` vaut `OPEN` — c'est
        # ce second qui dit si l'on peut négocier. Lire `status` en premier
        # faisait rejeter TOUT l'univers comme « non ouvert », et le rejet
        # ressemblait à un marché fermé au lieu d'un champ mal choisi.
        status=_opt_str(payload, "tradingStatus", "status", "marketStatus"),
        # `endDate` est le nom RÉEL (mesuré) ; les autres restent essayés au cas
        # où une route voisine emploierait la convention SAPI habituelle.
        end_time_ms=_opt_int(
            payload, "endDate", "endTime", "endTimeMs", "closeTime", "expireTime"
        ),
        volume_usdt=_opt_float(
            payload, "tradeVolume", "volume", "volume24hUsd", "volumeUsd"
        ),
        liquidity_usdt=_opt_float(payload, "liquidity", "totalLiquidityUsd"),
        fee_rate_bps=_opt_int(payload, "feeRateBps", "feeRate", "takerFeeBps"),
        outcome_token_ids=_collect_token_ids(payload),
        raw=dict(payload),
    )


# Champs qui n'existent QUE sur le topic et sans lesquels un marché ne se
# calcule pas. Recopiés sur chaque marché à l'aplatissement, sans jamais
# écraser une valeur propre au marché : le topic agrège, ses chiffres sont des
# totaux, et les afficher par branche serait faux.
TOPIC_CONTEXT_FIELDS = (
    "endDate",
    "startDate",
    "feeRateBps",
    "slippageBps",
    "collateral",
    "chainId",
    "vendor",
    "topicType",
    "chartType",
    "symbol",
    "slug",
    "variantData",
    "exchangeContractAddress",
    "participantCount",
)


def flatten_market_topics(
    payload: Any, *, where: str
) -> tuple[Mapping[str, Any], ...]:
    """Aplatit `marketTopics[].markets[]` en une liste de marchés négociables.

    MESURÉ le 2026-08-18 (première lecture authentifiée) : `/market/list` rend
    une structure à DEUX niveaux. Le *topic* porte la question, l'échéance et
    le taux de frais ; le *marché* porte l'identifiant, les branches et leurs
    jetons. Aucun des deux ne suffit seul :

      - demander un carnet avec un `marketTopicId` échoue d'une façon qui
        ressemble à un marché disparu ;
      - garder le marché nu le prive d'échéance et de taux de frais, donc de
        tout calcul de rendement.

    Un topic sans `markets` LÈVE au lieu de disparaître : c'est une anomalie de
    schéma, et l'escamoter ferait passer une liste tronquée pour une liste
    complète — exactement le genre de silence qui a coûté 56 séries de prix le
    2026-08-01.
    """
    topics = extract_rows(payload, where=where)
    marches: list[Mapping[str, Any]] = []
    for topic in topics:
        inner = topic.get("markets")
        if not isinstance(inner, list) or not inner:
            raise BinanceSchemaError(
                f"{where} : topic {topic.get('marketTopicId')!r} sans marché "
                "négociable — le schéma à deux niveaux a changé, ou cette "
                "ligne n'est pas un topic"
            )
        contexte = {
            k: topic[k] for k in TOPIC_CONTEXT_FIELDS if topic.get(k) is not None
        }
        for marche in inner:
            if not isinstance(marche, Mapping):
                raise BinanceSchemaError(f"{where} : marché non-objet dans un topic")
            # Le contexte d'abord, la ligne ensuite : à nom égal, c'est la
            # valeur du MARCHÉ qui gagne.
            fusion = dict(contexte)
            fusion.update(marche)
            fusion.setdefault("marketTopicId", topic.get("marketTopicId"))
            marches.append(fusion)
    return tuple(marches)


def extract_rows(payload: Any, *, where: str) -> tuple[Mapping[str, Any], ...]:
    """Sort la liste de lignes d'une réponse SAPI, quelle que soit l'enveloppe.

    SAPI n'est pas uniforme : selon la route, les lignes sont à la racine,
    sous `data`, sous `rows`, ou sous `data.list`. Faute de schéma publié, on
    essaie ces formes et on LÈVE si aucune ne colle — plutôt que de rendre une
    liste vide, qui se lirait « aucun marché » au lieu de « je n'ai pas su
    lire la réponse ».
    """
    if isinstance(payload, list):
        candidates: Any = payload
    elif isinstance(payload, Mapping):
        candidates = None
        for key in (
            "data",
            "rows",
            "list",
            "items",
            # MESURÉ le 2026-08-18 : `/market/list` enveloppe sous
            # `marketTopics`, `/category/list` sous `categories`,
            # `/balance/payment-options` sous `items`. Aucun de ces noms n'est
            # documenté ; ils viennent tous d'une lecture en direct.
            "marketTopics",
            "markets",
            "categories",
            "orders",
            "positions",
            "wallets",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
            if isinstance(value, Mapping):
                for sub in ("list", "rows", "data", "items"):
                    nested = value.get(sub)
                    if isinstance(nested, list):
                        candidates = nested
                        break
                if candidates is not None:
                    break
        if candidates is None:
            raise BinanceSchemaError(
                f"{where} : aucune liste trouvée (essayés : data, rows, list, "
                f"items — clés reçues : {sorted(payload)[:8]})"
            )
    else:
        raise BinanceSchemaError(f"{where} : objet ou liste attendu")

    return tuple(row for row in candidates if isinstance(row, Mapping))
