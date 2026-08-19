"""Tenue de marché sur Binance Prediction — la boucle qui cote en continu.

Ce que la sonde (`probe.py`) fait UNE fois, ce module le fait en boucle et sur
plusieurs marchés. C'est la différence entre un thermomètre et une machine.

## D'où vient l'argent, et pourquoi seulement depuis le 2026-08-19

Un ordre LIMIT ne paie AUCUN frais (`feeRateBps: 0`, mesuré), là où un MARKET
en paie 180 bps. Le revenu est donc l'ÉCART : acheter au meilleur bid, revendre
au meilleur ask. Sur un carnet à 0,39 / 0,40 cela fait 2,56 % par aller-retour.
En preneur, les 1,8 % payés à chaque sens engloutissaient cet écart deux fois —
c'est exactement ce qui a produit les −2,49 $ du compte.

## Ce que la boucle ne sait pas, et qu'elle mesure

Le TAUX DE REMPLISSAGE. Un écart affiché n'est pas un écart obtenu : la leçon
du 2026-07-28 sur Polymarket est que les +2 % lus dans les carnets ne se sont
jamais retrouvés dans les exécutions. La boucle tient donc un registre de
chaque remplissage réel, et c'est lui — pas le carnet — qui dira si la
stratégie vaut quelque chose.

## Sélection adverse : le risque qu'on ne peut pas supprimer

Être rempli à l'achat signifie que quelqu'un a voulu vendre à notre prix. Sur
un marché de prédiction, ce quelqu'un en sait parfois plus que nous. La branche
achetée peut valoir 0 à la résolution. D'où le plafond d'inventaire par marché,
et le refus de coter un marché dont l'échéance approche : une position qu'on
n'a pas eu le temps de revendre devient un pari, pas un stock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from ..execute.limits import ExecutionLimits, gate
from .model import (
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionMarket,
)
from .trade import LIMIT, PredictionOrder

if TYPE_CHECKING:  # pragma: no cover
    from .api import BinancePredictionClient

logger = logging.getLogger(__name__)

TICK = 0.01

# Sous deux pas de cotation, il n'y a rien à capturer : acheter au bid et
# revendre à l'ask rapporterait moins qu'un pas, et le moindre décalage du
# carnet transforme le gain en perte.
MIN_SPREAD_TICKS = 2

# Marge d'échéance. Une position qu'on n'a pas le temps de revendre n'est plus
# un stock mais un pari sur le résultat.
MIN_MINUTES_LEFT = 90


@dataclass(frozen=True)
class Rung:
    """Une cotation voulue sur une branche : ce qu'on veut acheter et revendre."""

    market: PredictionMarket
    token_id: str
    outcome: str
    buy_price: float
    sell_price: float
    best_bid: float
    best_ask: float

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def gross_edge(self) -> float:
        """Gain brut d'un aller-retour complet, en fraction du capital engagé.

        Sans frais (mesuré : `feeRateBps: 0` sur un LIMIT), c'est exactement
        l'écart rapporté au prix d'achat. BRUT : il suppose les DEUX côtés
        remplis, ce que rien ne garantit — c'est même l'inconnue centrale.
        """
        if self.buy_price <= 0:
            return 0.0
        return (self.sell_price - self.buy_price) / self.buy_price


@dataclass
class Inventory:
    """Ce qu'on détient réellement, par jeton."""

    shares: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)

    def add_fill(self, token_id: str, shares: float, usdt: float) -> None:
        self.shares[token_id] = self.shares.get(token_id, 0.0) + shares
        self.cost[token_id] = self.cost.get(token_id, 0.0) + usdt

    def held(self, token_id: str) -> float:
        return self.shares.get(token_id, 0.0)


def eligible(
    markets: Sequence[PredictionMarket],
    books: Mapping[str, PredictionBook],
    *,
    now_ms: int,
    tick: float = TICK,
) -> tuple[list[Rung], list[tuple[int, str]]]:
    """Les branches cotables, et le motif de chaque écartée.

    Les rejets sont RENDUS et non journalisés : « aucun marché éligible » sans
    les motifs envoie chercher une panne là où c'est un filtre qui a tout pris.
    """
    retenues: list[Rung] = []
    rejets: list[tuple[int, str]] = []

    for market in markets:
        statut = (market.status or "").upper()
        if statut and statut != "OPEN":
            rejets.append((market.market_id, "statut " + statut))
            continue
        if market.end_time_ms is None:
            rejets.append((market.market_id, "échéance illisible"))
            continue
        reste_min = (market.end_time_ms - now_ms) / 60_000
        if reste_min < MIN_MINUTES_LEFT:
            rejets.append(
                (
                    market.market_id,
                    f"échéance dans {reste_min:.0f} min — une position non "
                    "revendue deviendrait un pari",
                )
            )
            continue

        for token_id in market.outcome_token_ids:
            book = books.get(token_id)
            if book is None or book.best_bid is None or book.best_ask is None:
                rejets.append((market.market_id, "carnet incomplet"))
                continue
            bid, ask = book.best_bid.price, book.best_ask.price
            ecart_ticks = round((ask - bid) / tick)
            if ecart_ticks < MIN_SPREAD_TICKS:
                rejets.append(
                    (
                        market.market_id,
                        f"écart {ecart_ticks} pas — moins de "
                        f"{MIN_SPREAD_TICKS}, rien à capturer",
                    )
                )
                continue
            retenues.append(
                Rung(
                    market=market,
                    token_id=token_id,
                    outcome=str(book.raw.get("outcome") or "?"),
                    # On REJOINT la file au meilleur prix plutôt que de
                    # l'améliorer d'un pas : améliorer resserre l'écart qu'on
                    # cherche justement à encaisser.
                    buy_price=round(bid, 2),
                    sell_price=round(ask, 2),
                    best_bid=bid,
                    best_ask=ask,
                )
            )

    # Le meilleur écart d'abord : c'est lui le revenu, à remplissage égal.
    retenues.sort(key=lambda r: r.gross_edge, reverse=True)
    return retenues, rejets


def plan(
    rungs: Sequence[Rung],
    inventory: Inventory,
    *,
    notional_per_market: float,
    max_markets: int,
) -> list[PredictionOrder]:
    """Les ordres voulus, à partir de l'état. Fonction PURE : aucun réseau.

    Règle : une branche où l'on ne détient rien se fait ACHETER au bid ; une
    branche où l'on détient des parts se fait REVENDRE à l'ask. Jamais les deux
    à la fois sur la même branche — ce serait se croiser soi-même, et le carnet
    nous apparierait contre nous, ce qui paie l'écart au lieu de l'encaisser.
    """
    ordres: list[PredictionOrder] = []
    for rung in rungs[:max_markets]:
        detenu = inventory.held(rung.token_id)
        if detenu > 0:
            ordres.append(
                PredictionOrder(
                    market_id=rung.market.market_id,
                    token_id=rung.token_id,
                    side="SELL",
                    order_type=LIMIT,
                    price=rung.sell_price,
                    size=detenu,
                )
            )
            continue
        parts = round(notional_per_market / rung.buy_price, 2)
        if parts <= 0:
            continue
        ordres.append(
            PredictionOrder(
                market_id=rung.market.market_id,
                token_id=rung.token_id,
                side="BUY",
                order_type=LIMIT,
                price=rung.buy_price,
                size=parts,
            )
        )
    return ordres


# --- Moteur ----------------------------------------------------------------


@dataclass(frozen=True)
class LiveOrder:
    """Un ordre à nous, vivant au carnet."""

    order_id: str
    market_id: int
    token_id: str
    side: str
    price: float

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.market_id, self.token_id, self.side)


@dataclass
class MMReport:
    """Ce que la boucle a fait, et ce qu'elle n'a pas su faire.

    `armed` y figure exprès : un rapport qui ne dit pas s'il décrit une
    répétition ou un engagement réel est un rapport dangereux.
    """

    armed: bool
    ticks: int = 0
    placed: int = 0
    cancelled: int = 0
    kept: int = 0
    rejects: tuple[tuple[int, str], ...] = ()
    inventory_problem: str | None = None
    problem: str | None = None
    left_open: tuple[str, ...] = ()


def read_live_orders(rows: Sequence[Mapping[str, object]]) -> list[LiveOrder]:
    """Lit nos ordres ouverts. Une ligne illisible est IGNORÉE, pas devinée.

    Un ordre qu'on ne sait pas relire est un ordre qu'on ne saura pas annuler :
    il ressort dans `left_open` du rapport plutôt que de disparaître.
    """
    vivants: list[LiveOrder] = []
    for row in rows:
        try:
            vivants.append(
                LiveOrder(
                    order_id=str(row["orderId"]),
                    market_id=int(row["marketId"]),  # type: ignore[arg-type]
                    token_id=str(row.get("tokenId") or ""),
                    side=str(row["side"]).upper(),
                    price=float(row["price"]),  # type: ignore[arg-type]
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return vivants


def read_inventory(payload: object) -> tuple[Inventory, str | None]:
    """Lit l'inventaire détenu. Rend aussi le MOTIF s'il est illisible.

    NON VÉRIFIÉ EN DIRECT : `position/list` n'a jamais rendu de ligne non vide
    sur ce compte, donc le schéma des positions n'a jamais été observé rempli.
    On lit défensivement et on DIT qu'on n'a pas su lire, plutôt que de rendre
    un inventaire vide — qui se lirait « je ne détiens rien » et ferait vendre
    à découvert, ou plutôt refuser de vendre ce qu'on détient vraiment.
    """
    inv = Inventory()
    if not isinstance(payload, Mapping):
        return inv, "réponse de positions illisible"
    lignes = payload.get("positions")
    if not isinstance(lignes, list):
        return inv, "champ `positions` absent de la réponse"
    if not lignes:
        return inv, None  # rien en portefeuille : lecture valide

    inconnues = 0
    for row in lignes:
        if not isinstance(row, Mapping):
            inconnues += 1
            continue
        jeton = row.get("tokenId") or row.get("outcomeTokenId")
        parts = row.get("shareQty") or row.get("quantity") or row.get("shares")
        cout = row.get("costBasis") or row.get("totalCost") or 0
        if jeton is None or parts is None:
            inconnues += 1
            continue
        try:
            inv.add_fill(str(jeton), float(parts), float(cout))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            inconnues += 1

    if inconnues:
        return inv, (
            f"{inconnues} position(s) sur {len(lignes)} illisibles — schéma "
            "des positions jamais observé rempli, la vente est suspendue"
        )
    return inv, None


def reconcile(
    voulus: Sequence[PredictionOrder], vivants: Sequence[LiveOrder]
) -> tuple[list[PredictionOrder], list[LiveOrder], list[LiveOrder]]:
    """Compare le voulu au vivant. Rend (à poser, à annuler, à garder).

    Un ordre déjà au bon prix est GARDÉ, pas rejoué : le réémettre perdrait sa
    place dans la file, et la place dans la file est précisément ce qui décide
    d'être rempli ou non. C'est le seul avantage d'un teneur qui arrive tôt.
    """
    par_cle = {o.key: o for o in vivants}
    a_poser: list[PredictionOrder] = []
    a_garder: list[LiveOrder] = []
    utilises: set[tuple[int, str, str]] = set()

    for voulu in voulus:
        cle = (voulu.market_id, voulu.token_id, voulu.side.upper())
        vivant = par_cle.get(cle)
        if vivant is not None and abs(vivant.price - voulu.price) < 1e-9:
            a_garder.append(vivant)
            utilises.add(cle)
            continue
        a_poser.append(voulu)

    a_annuler = [o for o in vivants if o.key not in utilises]
    return a_poser, a_annuler, a_garder


async def run_market_maker(
    client: "BinancePredictionClient",
    *,
    bankroll: float,
    minutes: float,
    interval_s: float,
    max_markets: int,
    armed: bool,
    universe: int = 40,
    now_ms: int | None = None,
    sleep=None,
    now=None,
) -> MMReport:
    """La boucle. Rien ne bouge si `armed` est faux — même chemin, même plan.

    ORDRE DES OPÉRATIONS, et il compte : on lit l'inventaire AVANT de planifier.
    Planifier d'abord reviendrait à décider d'acheter sans savoir ce qu'on
    détient déjà, donc à empiler des positions sur un marché qu'on croyait vide.

    SI L'INVENTAIRE EST ILLISIBLE, LA BOUCLE S'ABSTIENT. Elle annule ce qui est
    vivant et ne pose plus rien. C'est plus dur que de continuer en aveugle,
    mais une machine qui achète sans savoir ce qu'elle détient ne sait pas
    revendre : elle accumule, et l'accumulation n'est pas la stratégie, c'est
    son échec. Le schéma des positions n'a jamais été observé rempli sur ce
    compte — on ne le suppose donc pas.

    Le nettoyage final est dans un `finally` : un ordre laissé au carnet après
    l'arrêt est une position que plus personne ne surveille.
    """
    import asyncio as _asyncio
    import time as _time

    from .api import BinancePredictionClient  # noqa: F401  (typage seulement)
    from .trade import PredictionTrader

    sleep = sleep or _asyncio.sleep
    now = now or _time.monotonic
    horloge_ms = int(_time.time() * 1000) if now_ms is None else now_ms

    par_marche = bankroll / max(max_markets, 1)
    limites = ExecutionLimits(
        max_total_usd=bankroll,
        max_per_market_usd=par_marche,
        max_orders=max_markets,
    )
    trader = PredictionTrader(client, limits=limites, armed=armed)
    rapport = MMReport(armed=armed)

    debut = now()
    limite_s = minutes * 60
    vivants: list[LiveOrder] = []

    try:
        while True:
            rapport.ticks += 1
            try:
                marches = await client.list_markets(limit=universe)
                carnets = await client.fetch_books(marches)
                lignes_pos = await client.positions()
                inventaire, motif = read_inventory(
                    {"positions": [dict(r) for r in lignes_pos]}
                )
                vivants = read_live_orders(await client.active_orders())
            except (BinanceApiError, BinanceSchemaError) as exc:
                # Un relevé raté n'arrête pas la boucle : les ordres posés
                # restent au carnet et il faut continuer à les surveiller.
                logger.warning("relevé incomplet au tour %d : %s", rapport.ticks, exc)
                if now() - debut + interval_s > limite_s:
                    break
                await sleep(interval_s)
                continue

            rapport.inventory_problem = motif
            if motif is not None:
                logger.error(
                    "inventaire illisible (%s) — la boucle s'abstient et annule", motif
                )
                voulus: list[PredictionOrder] = []
            else:
                rungs, rejets = eligible(
                    marches, carnets, now_ms=horloge_ms + int((now() - debut) * 1000)
                )
                rapport.rejects = tuple(rejets[:20])
                voulus = plan(
                    rungs,
                    inventaire,
                    notional_per_market=par_marche,
                    max_markets=max_markets,
                )

            a_poser, a_annuler, a_garder = reconcile(voulus, vivants)
            rapport.kept = len(a_garder)

            refus = gate(a_poser, limits=limites)
            for _ordre, motif_refus in refus.refused:
                logger.info("ordre refusé par le portier : %s", motif_refus)

            if armed and a_annuler:
                try:
                    await trader.batch_cancel([o.order_id for o in a_annuler])
                    rapport.cancelled += len(a_annuler)
                except (BinanceApiError, BinanceSchemaError) as exc:
                    logger.error("annulation refusée : %s", exc)

            for ordre in refus.allowed:
                if not armed:
                    rapport.placed += 1
                    continue
                try:
                    devis = await trader.get_quote(ordre)
                    await trader.place(ordre, devis)
                    rapport.placed += 1
                except (BinanceApiError, BinanceSchemaError, ValueError) as exc:
                    logger.warning(
                        "ordre non passé sur %s : %s", ordre.market_id, exc
                    )

            if now() - debut + interval_s > limite_s:
                break
            await sleep(interval_s)
    finally:
        if armed and vivants:
            try:
                await trader.batch_cancel([o.order_id for o in vivants])
                rapport.cancelled += len(vivants)
            except (BinanceApiError, BinanceSchemaError) as exc:
                logger.error(
                    "NETTOYAGE FINAL REFUSÉ (%s) — des ordres sont peut-être "
                    "ENCORE au carnet, à vérifier dans l'application", exc
                )
                rapport.left_open = tuple(o.order_id for o in vivants)

    return rapport
