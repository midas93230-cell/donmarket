"""Les exécutions réellement survenues, lues sur l'API publique de données.

`data-api.polymarket.com/trades` est le seul endroit qui dise ce qui s'est
VRAIMENT échangé : prix, taille, sens du preneur, horodatage. Les carnets
disent ce qui est proposé ; celui-ci dit ce qui est fait. C'est la différence
entre une fourchette affichée et une fourchette obtenue — piège mesuré le
2026-07-28, où des « +2 % » de carnet donnaient des exécutions perdantes.

Pas de clé, pas d'authentification : vérifié en direct depuis cette machine.

## Pourquoi on lit le flux GLOBAL et non marché par marché

Un appel rend les 500 dernières exécutions tous marchés confondus, soit une
vingtaine de secondes de flux. Filtrer localement sur nos jetons coûte un seul
appel quel que soit le nombre de marchés suivis ; interroger chaque marché en
coûterait un par marché, pour la même information. Le prix à payer est qu'un
sondage trop espacé laisse passer des exécutions : à 500 lignes par appel, il
faut interroger plus vite que le flux ne les chasse.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

from ..paper.fills import MarketTrade

log = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"

# Le maximum servi par l'API. En demander plus ne rend pas plus.
MAX_TRADES_PER_CALL = 500


def parse_trade(raw: dict[str, Any]) -> MarketTrade | None:
    """Convertit une ligne brute, ou rend None si elle est inexploitable.

    Rien n'est deviné : une ligne sans jeton, sans taille ou sans prix est
    écartée. Lui inventer une valeur par défaut ferait entrer une exécution
    fantôme dans un compte.
    """
    token_id = raw.get("asset")
    side = raw.get("side")
    if not token_id or not side:
        return None
    try:
        price = float(raw["price"])
        size = float(raw["size"])
        stamp = int(raw["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    if size <= 0 or not (0.0 < price < 1.0):
        return None
    return MarketTrade(
        token_id=str(token_id),
        price=price,
        size=size,
        taker_side=str(side).upper(),
        traded_at=datetime.fromtimestamp(stamp, tz=timezone.utc),
    )


def parse_trades(payload: Any) -> tuple[MarketTrade, ...]:
    if not isinstance(payload, list):
        return ()
    parsed = (parse_trade(row) for row in payload if isinstance(row, dict))
    return tuple(trade for trade in parsed if trade is not None)


def only_tokens(
    trades: Iterable[MarketTrade], token_ids: Sequence[str]
) -> tuple[MarketTrade, ...]:
    """Ne garde que les exécutions des jetons suivis."""
    wanted = set(token_ids)
    return tuple(trade for trade in trades if trade.token_id in wanted)


async def fetch_recent_trades(
    client: httpx.AsyncClient, *, limit: int = MAX_TRADES_PER_CALL
) -> tuple[MarketTrade, ...]:
    """Les dernières exécutions, tous marchés confondus, de la plus récente.

    Une panne de ce point d'accès ne doit pas arrêter une session de
    démonstration en cours : elle est journalisée et rend un tuple vide, ce qui
    se traduit par « aucun remplissage ce tour-ci », pas par un plantage.
    """
    try:
        response = await client.get(
            f"{DATA_API_BASE}/trades", params={"limit": min(limit, MAX_TRADES_PER_CALL)}
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Flux d'exécutions indisponible : %s", type(exc).__name__)
        return ()
    return parse_trades(response.json())
