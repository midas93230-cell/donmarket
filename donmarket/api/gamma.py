"""Client de l'API Gamma — découverte et métadonnées des marchés.

Gamma est la source la plus riche en métadonnées (volumes, liquidité, meilleures
limites, récompenses). C'est le point de départ de la « lecture de tout
Polymarket » : on y récupère l'univers complet des marchés, page par page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import httpx

from ..config import GAMMA_BASE, GAMMA_PAGE_SIZE, SETTINGS
from ..model import Market, parse_gamma_markets

logger = logging.getLogger(__name__)

# Une page vide signale la fin ; on garde une borne dure pour ne jamais
# boucler indéfiniment si l'API changeait de comportement de pagination.
MAX_PAGES = 500
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRANSIENT_STATUS = (429, 500, 502, 503, 504)


class GammaError(RuntimeError):
    """Échec irrécupérable côté API Gamma."""


class PaginationExhausted(GammaError):
    """Gamma refuse d'aller plus loin (422) : fin de l'univers accessible.

    Ce n'est pas une panne — c'est la borne dure de l'API, atteinte vers
    offset=2100. Il faut la distinguer d'une vraie erreur, sinon on réessaie
    trois fois pour rien avant de conclure à tort à un échec de scan.
    """


def build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GAMMA_BASE,
        timeout=SETTINGS.http_timeout,
        headers={"User-Agent": SETTINGS.user_agent, "Accept": "application/json"},
    )


async def _get_json(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
    """GET avec réessais sur erreurs transitoires (réseau, 429, 5xx)."""
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.get(path, params=params)
            if response.status_code == 422:
                raise PaginationExhausted(
                    f"Gamma refuse offset={params.get('offset')} (borne de l'API)"
                )
            if response.status_code in TRANSIENT_STATUS:
                raise httpx.HTTPStatusError(
                    f"statut transitoire {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == RETRY_ATTEMPTS:
                break
            delay = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "Gamma %s a échoué (essai %d/%d) : %s — nouvelle tentative dans %.0fs",
                path,
                attempt,
                RETRY_ATTEMPTS,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise GammaError(f"GET {path} a échoué après {RETRY_ATTEMPTS} essais") from last_error


async def iter_markets(
    client: httpx.AsyncClient,
    *,
    closed: bool | None = False,
    page_size: int = GAMMA_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> AsyncIterator[list[Market]]:
    """Parcourt l'univers des marchés, une page normalisée à la fois.

    `closed=False` limite aux marchés encore ouverts ; `closed=None` prend tout
    (utile pour constituer un historique de backtest).
    """
    for page in range(max_pages):
        params: dict[str, Any] = {
            "limit": page_size,
            "offset": page * page_size,
            "order": "volumeNum",
            "ascending": "false",
        }
        if closed is not None:
            params["closed"] = "true" if closed else "false"

        try:
            payload = await _get_json(client, "/markets", params)
        except PaginationExhausted as exc:
            logger.info("Fin de l'univers accessible : %s", exc)
            return

        rows = payload if isinstance(payload, list) else payload.get("data", [])
        if not rows:
            return

        yield parse_gamma_markets(rows)

        # Gamma sert exactement `page_size` lignes tant qu'il en reste : une
        # page courte signale la dernière.
        if len(rows) < page_size:
            return

    logger.warning(
        "Arrêt de la pagination Gamma à la borne de sécurité de %d pages", max_pages
    )


async def fetch_all_markets(
    *, closed: bool | None = False, max_pages: int = MAX_PAGES
) -> list[Market]:
    """Récupère l'univers complet en mémoire (pratique pour un scan ponctuel).

    Déduplique par `condition_id` : les pages successives peuvent se recouvrir
    quand le classement par volume bouge entre deux requêtes.
    """
    by_condition: dict[str, Market] = {}
    async with build_client() as client:
        async for page in iter_markets(client, closed=closed, max_pages=max_pages):
            for market in page:
                by_condition[market.condition_id] = market
    return list(by_condition.values())
