"""Flux WebSocket du CLOB — la surveillance continue, par opposition au scan.

Pourquoi ce module existe, en un chiffre : la concurrence sur un pool de
récompenses bouge en minutes. Un candidat observé le 2026-07-28 est passé de
461 $ à 256 $ de liquidité concurrente entre deux balayages espacés de quelques
minutes, son net de 22 à 42 %/jour — et il avait disparu du classement une
heure plus tard. Un scan ponctuel arrive donc systématiquement après la
bataille.

Protocole relevé EN DIRECT le 2026-07-29 (25 s d'écoute sur 16 jetons), pas lu
dans une documentation :

    book              76 messages   instantané complet, même forme que le REST
    price_change     872 messages   lot de modifications de paliers
    last_trade_price  29 messages   exécution réelle

Deux points valent d'être notés parce qu'ils se paient cher :

- Les `bids` arrivent à 0,01 en tête, exactement comme en REST : le meilleur
  prix est en fin de liste. `Book.__post_init__` remet l'ordre, ici comme là.
- Dans un `price_change`, `size` est la NOUVELLE taille du palier, pas une
  variation. Une taille nulle supprime le palier. C'est vérifié par
  réconciliation avec un instantané REST, pas supposé — se tromper de
  convention ferait diverger le carnet lentement, sans jamais lever d'erreur.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence

import websockets

from ..model import to_float
from .clob import Book, Level, parse_book

logger = logging.getLogger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Le serveur coupe les connexions muettes ; le ping garde le tuyau ouvert.
PING_INTERVAL_SECONDS = 10.0

# Après une coupure, on attend avant de revenir — et de plus en plus longtemps,
# pour ne pas marteler un service qui a peut-être un problème.
RECONNECT_BASE_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 60.0

# Le flux ne renvoie jamais d'instantané spontanément après l'abonnement
# initial : au-delà de ce nombre de jetons, mieux vaut plusieurs connexions.
MAX_TOKENS_PER_CONNECTION = 250

BID_SIDE = "BUY"


def _levels_to_map(levels: Iterable[Level]) -> dict[float, float]:
    return {level.price: level.size for level in levels}


def _map_to_levels(sizes: Mapping[float, float]) -> tuple[Level, ...]:
    return tuple(
        Level(price=price, size=size) for price, size in sizes.items() if size > 0
    )


def apply_price_change(book: Book, changes: Sequence[dict[str, Any]]) -> Book:
    """Applique un lot de modifications de paliers et rend un NOUVEAU carnet.

    Fonction pure : c'est elle qui porte toute la logique risquée du flux, donc
    elle doit être testable sans réseau. Une modification illisible est ignorée
    plutôt que de corrompre le carnet — perdre une mise à jour fait vieillir un
    prix, en inventer une le fausse.
    """
    bids = _levels_to_map(book.bids)
    asks = _levels_to_map(book.asks)

    for change in changes:
        if not isinstance(change, dict):
            continue
        price = to_float(change.get("price"))
        size = to_float(change.get("size"))
        if price is None or size is None or size < 0:
            continue
        side = bids if str(change.get("side", "")).upper() == BID_SIDE else asks
        if size == 0:
            side.pop(price, None)
        else:
            side[price] = size

    return Book(
        token_id=book.token_id,
        bids=_map_to_levels(bids),
        asks=_map_to_levels(asks),
        tick_size=book.tick_size,
        min_order_size=book.min_order_size,
    )


def changes_by_token(message: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Regroupe les modifications d'un `price_change` par jeton.

    Un seul message en porte plusieurs, souvent sur les deux branches du même
    marché (un ordre à 0,21 sur le « Yes » apparaît en 0,79 sur le « No »).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for change in message.get("price_changes") or []:
        if not isinstance(change, dict):
            continue
        token_id = change.get("asset_id")
        if isinstance(token_id, str) and token_id:
            grouped.setdefault(token_id, []).append(change)
    return grouped


def _iter_messages(payload: Any) -> Iterable[dict[str, Any]]:
    """Le flux envoie tantôt un objet, tantôt une liste d'objets."""
    rows = payload if isinstance(payload, list) else [payload]
    return (row for row in rows if isinstance(row, dict))


def apply_message(
    books: dict[str, Book], message: Mapping[str, Any]
) -> tuple[str, ...]:
    """Intègre un message dans un dictionnaire de carnets. Rend les jetons touchés.

    Le dictionnaire est modifié sur place : c'est un cache, pas un modèle. Les
    carnets qu'il contient, eux, restent immuables et remplacés en bloc.
    """
    kind = message.get("event_type")

    if kind == "book":
        book = parse_book(dict(message))
        if book is None:
            return ()
        books[book.token_id] = book
        return (book.token_id,)

    if kind == "price_change":
        touched: list[str] = []
        for token_id, changes in changes_by_token(message).items():
            known = books.get(token_id)
            # Sans instantané de départ, appliquer des modifications
            # construirait un carnet partiel qui a l'air complet.
            if known is None:
                continue
            books[token_id] = apply_price_change(known, changes)
            touched.append(token_id)
        return tuple(touched)

    return ()


async def stream_books(
    token_ids: Sequence[str], *, url: str = MARKET_WS_URL
) -> AsyncIterator[tuple[dict[str, Book], tuple[str, ...]]]:
    """Tient à jour les carnets des jetons demandés, indéfiniment.

    Produit à chaque message le cache complet et la liste des jetons touchés.
    Se reconnecte seule : une coupure de réseau ne doit pas arrêter une
    surveillance censée durer des heures. Le cache SURVIT à la reconnexion,
    puis est écrasé par les instantanés que le serveur renvoie à l'abonnement.
    """
    tokens = list(dict.fromkeys(tid for tid in token_ids if tid))
    if not tokens:
        return

    books: dict[str, Book] = {}
    delay = RECONNECT_BASE_SECONDS

    while True:
        try:
            async with websockets.connect(
                url, ping_interval=PING_INTERVAL_SECONDS
            ) as socket:
                await socket.send(
                    json.dumps({"assets_ids": tokens, "type": "market"})
                )
                logger.info("Flux temps réel : %d jetons suivis", len(tokens))
                delay = RECONNECT_BASE_SECONDS

                async for raw in socket:
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    for message in _iter_messages(payload):
                        touched = apply_message(books, message)
                        if touched:
                            yield books, touched

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # coupure, DNS, TLS, protocole…
            logger.info("Flux interrompu (%s) — reprise dans %.0f s", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
