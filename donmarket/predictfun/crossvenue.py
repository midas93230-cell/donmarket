"""Le pont Predict.fun ↔ Polymarket, et ce qu'il vaut vraiment.

Predict.fun publie lui-même, sur certains marchés, le `conditionId` du marché
Polymarket équivalent (`polymarketConditionIds`). C'est le SEUL lien structurel
entre les deux carnets — il n'y a rien d'autre à quoi se raccrocher, ni ticker
commun, ni identifiant partagé. Et DONmarket possède déjà toute la moitié
Polymarket, donc l'écart de prix entre les deux places est calculable sans
aucun accès supplémentaire.

CE QUE CE MODULE NE PRÉTEND PAS ÊTRE
------------------------------------
Un écart de prix entre deux places n'est PAS un arbitrage. Le capter suppose :
  - du capital des deux côtés simultanément (USDC sur Polygon *et* USDT sur BNB
    Chain), sans passerelle instantanée entre les deux ;
  - la capacité d'exécuter sur Predict.fun, qui exige une signature de
    portefeuille BNB Chain — non branchée à ce jour ;
  - que les deux marchés portent bien la MÊME question et la même polarité.
Le module mesure l'écart et énumère ces obstacles ; il ne classe rien en
« opportunité ».

DEUX PIÈGES MESURÉS LE 2026-08-09
---------------------------------
1. `gamma-api.polymarket.com/markets` IGNORE SILENCIEUSEMENT un paramètre mal
   nommé. `?condition_ids=0x…` filtre correctement (1 ligne) ; `?conditionIds=`
   et `?condition_id=` renvoient **HTTP 200 et 20 marchés non filtrés**. Un
   `rows[0]` naïf prendrait donc un marché sans aucun rapport pour le jumeau.
   D'où la revérification explicite du `conditionId` rendu.
2. Sur le testnet Predict.fun, les `polymarketConditionIds` sont FICTIFS : les
   5 identifiants publiés n'existent pas sur Polymarket (vérifié un par un).
   Le pont n'est donc pas validable de bout en bout avant l'accès mainnet, et
   `resolve_twins` doit traiter « jumeau introuvable » comme le cas courant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

import httpx

from ..config import GAMMA_BASE, SETTINGS
from ..model import Market, parse_gamma_market
from .model import PredictMarket

logger = logging.getLogger(__name__)

# Nom de paramètre VÉRIFIÉ. Les variantes voisines sont ignorées sans erreur.
CONDITION_PARAM = "condition_ids"


@dataclass(frozen=True)
class TwinQuote:
    """Un marché Predict.fun et son jumeau Polymarket, comparés.

    `polarity_checked` dit si l'on a pu confirmer que les deux places nomment la
    branche positive de la même façon. Sans cette confirmation, un écart de
    prix peut n'être que la lecture inversée du même marché — c'est-à-dire un
    écart entièrement faux.
    """

    predict: PredictMarket
    polymarket: Market
    predict_mid: float | None
    polymarket_mid: float | None
    polarity_checked: bool

    @property
    def divergence(self) -> float | None:
        """Prix Predict.fun moins prix Polymarket, en points de probabilité.

        None si l'un des deux carnets ne cote pas, ou si la polarité n'a pas pu
        être confirmée — rendre un nombre dans ce cas serait pire que rien.
        """
        if not self.polarity_checked:
            return None
        if self.predict_mid is None or self.polymarket_mid is None:
            return None
        return self.predict_mid - self.polymarket_mid

    @property
    def blockers(self) -> tuple[str, ...]:
        """Ce qui empêche de capter cet écart, même s'il est réel."""
        reasons = ["exécution Predict.fun non branchée (signature BNB Chain requise)"]
        if self.polymarket.closed:
            reasons.append("le marché Polymarket est clos")
        elif not self.polymarket.is_tradable:
            reasons.append("le marché Polymarket n'accepte pas d'ordres")
        if not self.polarity_checked:
            reasons.append("polarité des branches non confirmée entre les deux places")
        reasons.append("capital requis des deux côtés (USDC/Polygon et USDT/BNB Chain)")
        return tuple(reasons)


def _polymarket_mid(market: Market) -> float | None:
    if market.best_bid is None or market.best_ask is None:
        return None
    return (market.best_bid + market.best_ask) / 2.0


def _same_polarity(predict: PredictMarket, polymarket: Market) -> bool:
    """Les deux places appellent-elles « Yes » la même chose ?

    On compare les NOMS de branches. C'est faible mais vérifiable ; déduire la
    polarité de l'ORDRE des branches serait une supposition, et cet ordre n'est
    garanti par aucune des deux API.
    """
    predict_names = {o.name.casefold() for o in predict.outcomes if o.name}
    poly_names = {o.name.casefold() for o in polymarket.outcomes if o.name}
    if not predict_names or not poly_names:
        return False
    return predict_names == poly_names


async def fetch_polymarket_by_condition(
    condition_ids: Sequence[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Market]:
    """Récupère des marchés Polymarket par `conditionId`, en refusant les faux positifs.

    Chaque ligne rendue est confrontée au `conditionId` demandé. Sans ce
    contrôle, une API qui ignore le filtre livrerait des marchés arbitraires
    présentés comme des jumeaux.
    """
    wanted = [cid for cid in dict.fromkeys(condition_ids) if cid]
    if not wanted:
        return {}

    owns_client = client is None
    http = client or httpx.AsyncClient(
        base_url=GAMMA_BASE,
        timeout=SETTINGS.http_timeout,
        headers={"User-Agent": SETTINGS.user_agent, "Accept": "application/json"},
    )

    found: dict[str, Market] = {}
    try:
        for condition_id in wanted:
            try:
                response = await http.get("/markets", params={CONDITION_PARAM: condition_id})
                response.raise_for_status()
                rows = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("jumeau %s introuvable : %s", condition_id[:14], exc)
                continue

            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # LE contrôle qui compte : l'API a-t-elle vraiment filtré ?
                if row.get("conditionId") != condition_id:
                    logger.debug(
                        "réponse non filtrée ignorée pour %s (reçu %s)",
                        condition_id[:14],
                        str(row.get("conditionId"))[:14],
                    )
                    continue
                market = parse_gamma_market(row)
                if market is not None:
                    found[condition_id] = market
                break
    finally:
        if owns_client:
            await http.aclose()

    missing = len(wanted) - len(found)
    if missing:
        logger.info(
            "%d jumeau(x) Polymarket sur %d introuvables — attendu sur testnet, "
            "où les identifiants publiés par Predict.fun sont fictifs",
            missing,
            len(wanted),
        )
    return found


def pair_markets(
    predict_markets: Iterable[PredictMarket],
    twins: dict[str, Market],
    predict_mids: dict[int, float | None] | None = None,
) -> tuple[TwinQuote, ...]:
    """Assemble les paires à partir des jumeaux déjà résolus. Fonction PURE."""
    mids = predict_mids or {}
    quotes: list[TwinQuote] = []
    for market in predict_markets:
        for condition_id in market.polymarket_condition_ids:
            twin = twins.get(condition_id)
            if twin is None:
                continue
            quotes.append(
                TwinQuote(
                    predict=market,
                    polymarket=twin,
                    predict_mid=mids.get(market.market_id),
                    polymarket_mid=_polymarket_mid(twin),
                    polarity_checked=_same_polarity(market, twin),
                )
            )
    return tuple(quotes)


async def resolve_twins(
    predict_markets: Sequence[PredictMarket],
    predict_mids: dict[int, float | None] | None = None,
) -> tuple[TwinQuote, ...]:
    """Résout les jumeaux Polymarket des marchés donnés et les compare."""
    condition_ids = [cid for m in predict_markets for cid in m.polymarket_condition_ids]
    if not condition_ids:
        return ()
    twins = await fetch_polymarket_by_condition(condition_ids)
    return pair_markets(predict_markets, twins, predict_mids)


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "  -  "


def describe(quotes: Sequence[TwinQuote]) -> tuple[str, ...]:
    """Rend le comparatif en lignes lisibles, obstacles compris."""
    if not quotes:
        return (
            "aucun jumeau Polymarket résolu — soit les marchés ne publient pas "
            "de `polymarketConditionIds`, soit ces identifiants n'existent pas "
            "(cas du testnet, où ils sont fictifs)",
        )

    lines: list[str] = []
    for quote in quotes:
        divergence = quote.divergence
        if divergence is None:
            verdict = "écart non calculable"
        else:
            verdict = f"écart {divergence:+.4f} ({divergence * 100:+.2f} pts)"
        lines.append(
            f"{quote.predict.market_id:>7}  predict={_fmt(quote.predict_mid)} "
            f"poly={_fmt(quote.polymarket_mid)}  {verdict}  "
            f"{quote.predict.title[:34]}"
        )
        lines.append(f"          bloqué par : {' ; '.join(quote.blockers)}")
    return tuple(lines)
