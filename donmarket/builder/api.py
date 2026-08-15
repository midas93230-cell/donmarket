"""Les trois sources publiques du programme Builders, et leurs pièges mesurés.

Aucune clé n'est requise pour lire quoi que ce soit ici (MESURÉ le 2026-08-15,
depuis cette machine, sans rien contourner). Trois endpoints, sur DEUX hôtes
différents — c'est la première chose qui surprend :

    clob.polymarket.com/builder/trades                exécutions attribuées
    data-api.polymarket.com/v1/builders/leaderboard   classement par volume
    data-api.polymarket.com/v1/builders/volume        série quotidienne

## Piège n° 1 — le silence porte sur la VALEUR, pas sur la clé

`?builder_code=` est le SEUL nom accepté : `builderCode`, `builder`, `code` et
l'absence de paramètre rendent tous un **HTTP 400** `{"error":"builder code is
required"}`. Bonne nouvelle, et différence nette avec Gamma, qui sert 200 lignes
non filtrées quand on se trompe de nom de paramètre.

Mais une VALEUR malformée passe en silence : `0x01`, `0X00…01`, `00…01`,
`0xZZ…01` et `CHOUCROUTE` rendent tous
`{"data":[],"next_cursor":"LTE=","limit":300,"count":0}` — strictement la même
réponse qu'un code valide sans volume. D'où la validation locale AVANT tout
appel, dans `builder.codes`.

## Piège n° 2 — le curseur avance ici, contrairement à tout le reste du dépôt

Gamma plafonne à offset 2100, le curseur Predict.fun ne bouge jamais. Celui-ci
fonctionne : il encode l'offset en base64 (« MzAw » = 300) et signale la fin par
base64 de « −1 », soit `LTE=`. Une boucle qui traite `LTE=` comme un curseur
ordinaire redemande l'offset −1 et tourne sans fin. `limit` est IGNORÉ : la page
fait 300 lignes, qu'on en demande 5 ou 1000.

## Piège n° 3 — les exécutions ne sont PAS triées par date

MESURÉ sur trois builders, 12 000 lignes chacun : `matchTime` n'est ni croissant
ni décroissant. Filtrer sur une période en s'arrêtant à la première ligne trop
ancienne perdrait donc des lignes silencieusement. Et comme il n'existe aucun
paramètre de date, il n'y a pas de moyen bon marché de mesurer une fenêtre :
`fetch_builder_trades` rend un ÉCHANTILLON, ce que `TradeSample.truncated` dit
explicitement.

## Ce qui reste NON MESURÉ, et qu'on refuse de deviner

L'unité du champ `volume` du classement n'est pas établie. Pour la vérifier il
faudrait aspirer un builder entier et comparer à la somme des `sizeUsdc` — or
même les plus petits du top 50 dépassent 12 000 exécutions, et l'aspiration
reste tronquée. `LeaderboardEntry.volume_unit_is_assumed` porte cette réserve
jusque dans le rapport, plutôt que de la laisser au fond d'un README.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import (
    BUILDER_CURSOR_END,
    BUILDER_MAX_PAGES,
    CLOB_BASE,
    DATA_API_BASE,
    SETTINGS,
)
from .codes import BuilderCode

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

LEADERBOARD_PERIODS = ("DAY", "WEEK", "MONTH", "ALL")

# MESURÉ : le classement plafonne à 50 lignes par page (le schéma publié donne
# `limit` max 50, `offset` max 1000).
LEADERBOARD_MAX_LIMIT = 50


class BuilderApiError(RuntimeError):
    """Échec irrécupérable côté API builder."""


def _to_float(raw: Any, default: float = 0.0) -> float:
    """Les montants arrivent en CHAÎNES (`"5.059999"`), pas en nombres."""
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BuilderTrade:
    """Une exécution attribuée à un code builder."""

    trade_id: str
    trade_type: str  # TAKER | MAKER
    market: str
    price: float
    shares: float
    notional_usd: float  # `sizeUsdc` — la base des frais builder
    platform_fee_usd: float  # `feeUsdc` — ne va PAS au builder
    builder_fee_usd: float
    builder_code: str
    match_time: int
    outcome: str
    side: str

    @staticmethod
    def parse(row: dict[str, Any]) -> "BuilderTrade":
        return BuilderTrade(
            trade_id=str(row.get("id") or ""),
            trade_type=str(row.get("tradeType") or "").upper(),
            market=str(row.get("market") or ""),
            price=_to_float(row.get("price")),
            shares=_to_float(row.get("size")),
            notional_usd=_to_float(row.get("sizeUsdc")),
            platform_fee_usd=_to_float(row.get("feeUsdc")),
            builder_fee_usd=_to_float(row.get("builderFee")),
            builder_code=str(row.get("builderCode") or ""),
            match_time=int(_to_float(row.get("matchTime"))),
            outcome=str(row.get("outcome") or ""),
            side=str(row.get("side") or ""),
        )


@dataclass(frozen=True)
class TradeSample:
    """Des exécutions, et l'aveu de ce qu'elles ne couvrent pas."""

    code: str
    trades: tuple[BuilderTrade, ...]
    pages: int
    truncated: bool

    def __len__(self) -> int:
        return len(self.trades)

    @property
    def is_complete(self) -> bool:
        """Vrai seulement si le flux s'est terminé de lui-même.

        Sur un échantillon tronqué, toute somme (volume, frais encaissés) est un
        PLANCHER et jamais un total. Le rapport doit le dire.
        """
        return not self.truncated

    @property
    def notional_usd(self) -> float:
        return sum(t.notional_usd for t in self.trades)

    @property
    def builder_fee_usd(self) -> float:
        return sum(t.builder_fee_usd for t in self.trades)


@dataclass(frozen=True)
class LeaderboardEntry:
    """Une ligne du classement des builders."""

    rank: int
    builder: str
    code: str
    volume: float
    active_users: int
    verified: bool

    # L'unité de `volume` n'a pas pu être vérifiée (cf. l'en-tête du module).
    # Le champ est porté par l'objet pour que rien en aval ne puisse présenter
    # un revenu estimé comme une mesure.
    volume_unit_is_assumed: bool = True

    @property
    def has_usable_code(self) -> bool:
        return self.code.startswith("0x") and len(self.code) == 66

    @staticmethod
    def parse(row: dict[str, Any]) -> "LeaderboardEntry":
        return LeaderboardEntry(
            rank=int(_to_float(row.get("rank"))),
            builder=str(row.get("builder") or ""),
            code=str(row.get("builderCode") or ""),
            volume=_to_float(row.get("volume")),
            active_users=int(_to_float(row.get("activeUsers"))),
            verified=bool(row.get("verified")),
        )


@dataclass(frozen=True)
class DailyVolume:
    """Le volume d'un builder sur une journée."""

    day: str  # ISO 8601 UTC, ex. « 2026-08-15T00:00:00Z »
    builder: str
    code: str
    volume: float
    active_users: int

    @staticmethod
    def parse(row: dict[str, Any]) -> "DailyVolume":
        return DailyVolume(
            day=str(row.get("dt") or ""),
            builder=str(row.get("builder") or ""),
            code=str(row.get("builderCode") or ""),
            volume=_to_float(row.get("volume")),
            active_users=int(_to_float(row.get("activeUsers"))),
        )


def build_clob_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=CLOB_BASE,
        timeout=SETTINGS.http_timeout,
        headers={"User-Agent": SETTINGS.user_agent, "Accept": "application/json"},
    )


def build_data_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=DATA_API_BASE,
        timeout=SETTINGS.http_timeout,
        headers={"User-Agent": SETTINGS.user_agent, "Accept": "application/json"},
    )


async def _get_json(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    """GET avec réessais sur erreurs transitoires (réseau, 429, 5xx)."""
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.get(path, params=params)
            if response.status_code == 400:
                # Un 400 ici est structurel (nom de paramètre), pas transitoire :
                # le réessayer trois fois ne fait que retarder le diagnostic.
                raise BuilderApiError(
                    f"GET {path} refusé (400) : {response.text.strip()[:200]}"
                )
            if response.status_code in TRANSIENT_STATUS:
                raise httpx.HTTPStatusError(
                    f"statut transitoire {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except BuilderApiError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == RETRY_ATTEMPTS:
                break
            delay = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "builder %s a échoué (essai %d/%d) : %s — nouvelle tentative dans %.0fs",
                path,
                attempt,
                RETRY_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise BuilderApiError(
        f"GET {path} a échoué après {RETRY_ATTEMPTS} essais"
    ) from last_error


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Le classement rend une LISTE nue, `/builder/trades` une enveloppe."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    return []


async def fetch_builder_trades(
    client: httpx.AsyncClient,
    code: BuilderCode | str,
    *,
    max_pages: int = BUILDER_MAX_PAGES,
) -> TradeSample:
    """Aspire les exécutions attribuées à un code, page par page.

    Le code est validé AVANT le premier appel : un code malformé rendrait une
    page vide indiscernable d'un builder sans volume, et le compte à zéro qui
    en découle serait tenu pour vrai.
    """
    validated = code if isinstance(code, BuilderCode) else BuilderCode(str(code))

    trades: list[BuilderTrade] = []
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    exhausted = False

    while pages < max_pages:
        params: dict[str, Any] = {"builder_code": validated.value}
        if cursor:
            params["next_cursor"] = cursor
        payload = await _get_json(client, "/builder/trades", params)
        rows = _rows(payload)
        pages += 1

        for row in rows:
            trade = BuilderTrade.parse(row)
            # Déduplication par identifiant : le curseur avance ici, mais on ne
            # fait pas reposer l'exactitude d'un total sur cette bonne volonté.
            if trade.trade_id and trade.trade_id in seen:
                continue
            if trade.trade_id:
                seen.add(trade.trade_id)
            trades.append(trade)

        next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None

        # Fin PROPRE : le serveur dit explicitement qu'il n'y a plus rien.
        if not rows or next_cursor in (None, "", BUILDER_CURSOR_END):
            exhausted = True
            break

        # Curseur qui PIÉTINE — le mode de panne de Gamma et de Predict.fun.
        # On s'arrête aussi, mais surtout PAS en se déclarant complet : rien ne
        # dit qu'on a tout vu, et un total présenté comme définitif sur un flux
        # cassé est exactement le genre de chiffre faux et crédible qu'on
        # refuse de produire.
        if next_cursor == cursor:
            logger.warning(
                "builder %s : curseur bloqué sur %r après %d pages — "
                "échantillon déclaré tronqué",
                validated.short,
                cursor,
                pages,
            )
            break

        cursor = str(next_cursor)

    return TradeSample(
        code=validated.value,
        trades=tuple(trades),
        pages=pages,
        truncated=not exhausted,
    )


async def fetch_leaderboard(
    client: httpx.AsyncClient,
    *,
    period: str = "ALL",
    limit: int = LEADERBOARD_MAX_LIMIT,
) -> tuple[LeaderboardEntry, ...]:
    """Classement des builders par volume routé sur la période demandée."""
    key = period.upper()
    if key not in LEADERBOARD_PERIODS:
        raise BuilderApiError(
            f"période inconnue : {period!r} (attendu {', '.join(LEADERBOARD_PERIODS)})"
        )
    payload = await _get_json(
        client,
        "/v1/builders/leaderboard",
        {"timePeriod": key, "limit": min(limit, LEADERBOARD_MAX_LIMIT)},
    )
    return tuple(LeaderboardEntry.parse(r) for r in _rows(payload))


async def fetch_daily_volume(client: httpx.AsyncClient) -> tuple[DailyVolume, ...]:
    """Série quotidienne, tous builders confondus.

    MESURÉ : l'endpoint ignore `limit` et sert l'historique ENTIER d'un coup —
    38 304 lignes, 479 builders, 310 journées du 2025-10-10 au 2026-08-15. Il
    n'est donc pas paginé et n'accepte pas de filtre : on prend tout, ou rien.
    """
    payload = await _get_json(client, "/v1/builders/volume", {})
    return tuple(DailyVolume.parse(r) for r in _rows(payload))
