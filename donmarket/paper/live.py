"""Faire vivre une session : sonder le marché en boucle et avancer le compte.

La partie impure du paquet, et volontairement la plus mince possible. Elle ne
décide rien : elle va chercher des carnets et des exécutions, les passe à
`PaperSession.tick`, et recommence. Toute la mécanique du compte est dans
`session.py`, testable sans réseau.

## Le rythme, et pourquoi il n'est pas libre

Chaque tour coûte deux appels : un `POST /books` groupé (tous les jetons d'un
coup) et un `GET /trades` (500 dernières exécutions, tous marchés confondus).
Le second impose la cadence : 500 exécutions représentent une vingtaine de
secondes de flux total. Sonder plus lentement, c'est laisser passer des
exécutions — donc rater des remplissages, donc afficher un compte trop pauvre.
D'où un intervalle par défaut nettement sous cette limite.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Sequence

from ..api import clob
from ..api.data import fetch_recent_trades, only_tokens
from .session import PaperSession, SessionSnapshot, snapshot

log = logging.getLogger(__name__)

# Sous la vingtaine de secondes que couvrent 500 exécutions, avec de la marge
# pour une rafale de marché. Ce n'est pas un réglage de confort : au-delà, des
# remplissages sont perdus en silence.
DEFAULT_INTERVAL_SECONDS = 5.0


async def run_session(
    session: PaperSession,
    *,
    duration_seconds: float,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    on_tick: Callable[[SessionSnapshot], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> PaperSession:
    """Fait tourner la session jusqu'à épuisement du temps imparti.

    Rend la session dans son état final. `on_tick` reçoit un instantané après
    chaque tour — c'est par là que passent l'affichage console et le web, sans
    qu'aucun des deux ne touche à l'état interne.
    """
    tokens: Sequence[str] = session.token_ids
    if not tokens:
        log.warning("Aucun jeton à suivre : la session n'a rien à observer.")
        return session

    started_at = datetime.now(timezone.utc)
    deadline = started_at.timestamp() + duration_seconds
    previous = started_at

    async with clob.build_client() as client:
        while datetime.now(timezone.utc).timestamp() < deadline:
            if stop is not None and stop():
                log.info("Session interrompue à la demande.")
                break

            now = datetime.now(timezone.utc)
            elapsed = (now - previous).total_seconds()
            previous = now

            books, trades = await asyncio.gather(
                clob.fetch_books(tokens),
                fetch_recent_trades(client),
            )
            session = session.tick(
                books=books,
                trades=only_tokens(trades, tokens),
                seconds=elapsed,
            )

            if on_tick is not None:
                on_tick(snapshot(session, started_at=started_at, observed_at=now))

            await asyncio.sleep(interval_seconds)

    return session
