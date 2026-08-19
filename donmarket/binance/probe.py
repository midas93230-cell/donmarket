"""Sonde de REMPLISSAGE côté teneur — la seule inconnue restante.

Depuis le 2026-08-09 tout le reste est mesuré : les carnets se lisent, les
devis se calculent au centime, la formule de frais est vérifiée. Ce qui n'a
JAMAIS été mesuré, c'est le terme qui décide de tout : **un ordre LIMIT posé
au meilleur bid se remplit-il, et en combien de temps ?**

Pourquoi ça ne se déduit pas des carnets : la leçon du 2026-07-28 sur
Polymarket est que les écarts AFFICHÉS ne se retrouvent pas dans les
exécutions. Un carnet dit ce qu'on pourrait obtenir, pas ce qu'on obtient.
Seul un ordre réellement posé répond.

## Ce que la sonde fait, et dans quel ordre

1. choisit UNE branche d'UN marché, avec motif écrit pour chaque écarté ;
2. calcule le prix le plus haut qui reste STRICTEMENT sous le meilleur ask —
   poser au niveau de l'ask ferait de nous un PRENEUR, qui paie 2 % au lieu
   d'encaisser un rebate, et la mesure porterait alors sur autre chose ;
3. passe l'ordre (si armée) ;
4. relève l'état à intervalle fixe : rempli combien, à quel moment, et où en
   est le carnet pendant ce temps ;
5. **annule le reliquat**. Un ordre laissé au carnet après la mesure est une
   position ouverte qu'on n'a pas décidé de prendre.

## Ce qu'elle ne fait pas

Elle ne cherche aucun edge et n'en annoncera aucun. À 8,73 $ de capital, un
remplissage complet rapporte ~4 centimes de rebate (25 % du frais preneur,
soit 0,45 % du notionnel apparié, mesuré le 2026-08-18). C'est un test de
PLOMBERIE : le chiffre utile est le taux de remplissage, pas le gain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..execute.limits import ExecutionLimits, gate
from .api import BinancePredictionClient
from .model import (
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionMarket,
)
from .trade import LIMIT, PredictionOrder, PredictionTrader, Quote

logger = logging.getLogger(__name__)

# Pas de cotation observé sur les carnets Binance/Predict.fun : 0,01. Les prix
# rendus par `/order-book` sont tous des centièmes (mesuré 2026-08-18).
TICK = 0.01

# Marge exigée entre la fin de la sonde et l'échéance du marché. Un marché qui
# se résout pendant la mesure ne mesure rien : l'ordre disparaît sans qu'on
# sache s'il a été rempli ou emporté par la résolution.
END_MARGIN_MINUTES = 5

# Statuts terminaux connus. `FILLED` est mesuré sur les 6 ordres du compte ;
# les autres sont défensifs — un statut inconnu est traité comme NON terminal
# pour ne pas arrêter la mesure sur un nom qu'on aurait mal deviné.
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"})


@dataclass(frozen=True)
class MakerPost:
    """L'ordre teneur retenu, avec de quoi expliquer pourquoi celui-là."""

    market: PredictionMarket
    token_id: str
    outcome: str
    price: float
    size: float
    best_bid: float
    best_ask: float
    improves_bid: bool

    @property
    def notional_usdt(self) -> float:
        return self.price * self.size

    def as_order(self) -> PredictionOrder:
        return PredictionOrder(
            market_id=self.market.market_id,
            token_id=self.token_id,
            side="BUY",
            order_type=LIMIT,
            price=self.price,
            size=self.size,
        )

    @property
    def description(self) -> str:
        place = (
            "AMELIORE le meilleur bid"
            if self.improves_bid
            else "REJOINT la file au meilleur bid"
        )
        return (
            f"marché {self.market.market_id} / branche {self.outcome} — "
            f"LIMIT BUY {self.size:.2f} parts @ {self.price:.2f} "
            f"({self.notional_usdt:.2f} USDT), {place} "
            f"(bid {self.best_bid:.2f} / ask {self.best_ask:.2f})"
        )


def maker_price(book: PredictionBook, *, tick: float = TICK) -> float | None:
    """Le prix le plus haut qui laisse l'ordre du côté TENEUR.

    On tente `meilleur bid + 1 tick` : être seul au meilleur prix place devant
    toute la file, ce qui maximise l'information tirée d'un seul ordre. Si ce
    prix atteint l'ask, on retombe sur le meilleur bid — franchir l'écart
    exécuterait immédiatement contre l'ask et ferait de nous un PRENEUR, ce qui
    mesurerait le contraire de ce qu'on veut mesurer.

    Rend `None` plutôt qu'un prix approximatif si le carnet n'a pas deux côtés :
    sans ask, « rester sous l'ask » n'a pas de sens.
    """
    bid = book.best_bid
    ask = book.best_ask
    if bid is None or ask is None:
        return None
    ameliore = round(bid.price + tick, 4)
    prix = ameliore if ameliore < ask.price else bid.price
    if not 0.0 < prix < 1.0:
        return None
    return prix


def select_post(
    markets: Sequence[PredictionMarket],
    books: Mapping[str, PredictionBook],
    *,
    notional_usdt: float,
    now_ms: int,
    minutes_needed: int,
    tick: float = TICK,
) -> tuple[MakerPost | None, tuple[tuple[int, str], ...]]:
    """Choisit une branche, et dit pourquoi chaque autre a été écartée.

    Les rejets sont RENDUS, pas journalisés : « aucun marché éligible » sans la
    liste des motifs est le genre de message qui envoie chercher une panne de
    clé alors que c'est le filtre d'échéance qui a tout pris.

    Classement des survivants : par VOLUME décroissant. Le remplissage d'un
    ordre teneur dépend du flux qui vient le frapper, pas de la profondeur
    affichée — c'est le flux qu'on approche par le volume. Réserve honnête :
    `tradeVolume` peut être un total de topic recopié sur chaque branche
    (aplatissement du 2026-08-18), donc il classe, il ne chiffre pas.
    """
    limite_ms = now_ms + (minutes_needed + END_MARGIN_MINUTES) * 60_000
    rejets: list[tuple[int, str]] = []
    candidats: list[MakerPost] = []

    for market in markets:
        statut = (market.status or "").upper()
        if statut and statut != "OPEN":
            rejets.append((market.market_id, f"statut {statut}"))
            continue
        if market.end_time_ms is None:
            rejets.append((market.market_id, "échéance illisible"))
            continue
        if market.end_time_ms < limite_ms:
            reste = (market.end_time_ms - now_ms) / 60_000
            rejets.append(
                (
                    market.market_id,
                    f"échéance dans {reste:.0f} min — moins que les "
                    f"{minutes_needed} min de mesure + {END_MARGIN_MINUTES} de marge",
                )
            )
            continue

        for token_id in market.outcome_token_ids:
            book = books.get(token_id)
            if book is None:
                rejets.append((market.market_id, "carnet absent"))
                continue
            prix = maker_price(book, tick=tick)
            if prix is None:
                rejets.append((market.market_id, "carnet à un seul côté"))
                continue
            meilleur_bid = book.best_bid
            meilleur_ask = book.best_ask
            assert meilleur_bid is not None and meilleur_ask is not None
            candidats.append(
                MakerPost(
                    market=market,
                    token_id=token_id,
                    outcome=str(book.raw.get("outcome") or "?"),
                    price=prix,
                    size=round(notional_usdt / prix, 2),
                    best_bid=meilleur_bid.price,
                    best_ask=meilleur_ask.price,
                    improves_bid=prix > meilleur_bid.price,
                )
            )

    if not candidats:
        return None, tuple(rejets)

    # CLASSEMENT CORRIGÉ le 2026-08-19. Le tri par volume seul avait choisi un
    # marché à p = 0,04 (« aliens exist before 2027 », bid = ask) — le pire
    # possible, parce que le rebate teneur vaut `0,25 × taux × min(p, 1−p)` :
    # à 0,04 il rapporte 25 fois moins qu'à 0,50, quel que soit le volume.
    #
    # Le gain attendu par part est donc le critère PREMIER, et le volume ne
    # départage plus qu'à gain comparable — il approche le flux qui viendra
    # nous frapper, ce qui reste utile mais ne rattrape jamais un facteur 25.
    # Les deux sont gardés dans la clé plutôt que fondus dans un score
    # arbitraire : un score inventerait une pondération que rien ne mesure.
    def rebate_par_part(c: MakerPost) -> float:
        return min(c.price, 1.0 - c.price)

    candidats.sort(
        key=lambda c: (round(rebate_par_part(c), 3), c.market.volume_usdt or 0.0),
        reverse=True,
    )
    return candidats[0], tuple(rejets)


@dataclass(frozen=True)
class Fill:
    """Ce qu'on sait du remplissage à un instant donné."""

    status: str
    filled_shares: float
    filled_usdt: float
    fraction: float

    @property
    def is_terminal(self) -> bool:
        return self.status.upper() in TERMINAL_STATUSES


def read_fill(order_id: str, rows: Sequence[Mapping[str, Any]]) -> Fill | None:
    """Retrouve notre ordre dans une liste rendue par l'API, ou `None`.

    PIÈGE D'UNITÉ MESURÉ sur les 6 ordres du compte : `fillPercentage` vaut
    `"1"` pour un ordre entièrement rempli. C'est une FRACTION, pas un
    pourcentage. L'afficher tel quel annoncerait « 1 % rempli » sur un ordre
    complet — erreur d'un facteur 100 dans le sens qui fait abandonner une
    piste qui marche.
    """
    for row in rows:
        if str(row.get("orderId") or "").strip() != order_id:
            continue

        def nombre(*noms: str) -> float:
            for nom in noms:
                valeur = row.get(nom)
                if isinstance(valeur, (int, float, str)) and not isinstance(
                    valeur, bool
                ):
                    try:
                        return float(valeur)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        return Fill(
            status=str(row.get("status") or "INCONNU"),
            filled_shares=nombre("filledShareQty", "filledQuantity"),
            filled_usdt=nombre("filledUsdtAmount", "filledAmount"),
            fraction=nombre("fillPercentage"),
        )
    return None


def read_order_id(payload: Mapping[str, Any]) -> str | None:
    """Extrait l'identifiant d'ordre de la réponse de `place-order-bundle`.

    Le schéma de cette réponse n'est PAS documenté et n'avait jamais été vu
    avant aujourd'hui : on cherche donc plusieurs noms, on descend dans `data`,
    et on rend `None` sans lever si rien ne ressemble à un identifiant.
    L'appelant a un plan B (retrouver l'ordre dans `order/list`), et lever ici
    perdrait la trace d'un ordre DÉJÀ passé — le pire résultat possible.
    """
    source: Mapping[str, Any] = payload
    inner = payload.get("data")
    if isinstance(inner, Mapping):
        source = inner
    for nom in ("orderId", "order_id", "orderIds", "id"):
        valeur = source.get(nom)
        if isinstance(valeur, (str, int)) and not isinstance(valeur, bool):
            if str(valeur).strip():
                return str(valeur).strip()
        if isinstance(valeur, (list, tuple)) and valeur:
            premier = valeur[0]
            if isinstance(premier, (str, int)) and str(premier).strip():
                return str(premier).strip()
    return None


@dataclass(frozen=True)
class Snapshot:
    """Un relevé. `elapsed_s` compte depuis l'envoi de l'ordre."""

    elapsed_s: float
    fill: Fill | None
    best_bid: float | None
    best_ask: float | None

    @property
    def line(self) -> str:
        if self.fill is None:
            etat = "introuvable (ni ouvert, ni dans l'historique)"
        else:
            etat = (
                f"{self.fill.status} — {self.fill.filled_shares:.2f} parts "
                f"({self.fill.fraction * 100:.0f} %)"
            )
        carnet = (
            f"bid {self.best_bid:.2f} / ask {self.best_ask:.2f}"
            if self.best_bid is not None and self.best_ask is not None
            else "carnet illisible"
        )
        return f"  t+{self.elapsed_s:>5.0f}s  {etat}  [{carnet}]"


@dataclass
class ProbeResult:
    """Le compte-rendu complet.

    `armed` y figure exprès : un rapport qui ne dit pas s'il décrit une
    répétition ou de vrais ordres finit par être mal lu.
    """

    armed: bool
    post: MakerPost | None = None
    quote: Quote | None = None
    order_id: str | None = None
    snapshots: tuple[Snapshot, ...] = ()
    cancelled: bool = False
    rejects: tuple[tuple[int, str], ...] = ()
    problem: str | None = None
    placed_raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def final_fill(self) -> Fill | None:
        for snap in reversed(self.snapshots):
            if snap.fill is not None:
                return snap.fill
        return None

    @property
    def time_to_first_fill_s(self) -> float | None:
        for snap in self.snapshots:
            if snap.fill is not None and snap.fill.filled_shares > 0:
                return snap.elapsed_s
        return None


async def observe_order(
    client: BinancePredictionClient,
    order_id: str,
    post: MakerPost,
    *,
    minutes: int,
    interval_s: int,
    sleep=asyncio.sleep,
    now=time.monotonic,
) -> tuple[Snapshot, ...]:
    """Relève l'état de l'ordre jusqu'à terminaison ou fin du temps imparti.

    On interroge `order/list` PUIS `order/history` : un ordre entièrement
    rempli quitte la liste des ouverts, et ne le chercher qu'à un endroit
    ferait lire « disparu » là où il faut lire « rempli ».

    Une erreur d'API pendant l'observation n'interrompt PAS la mesure : l'ordre
    est déjà posé, et abandonner la boucle le laisserait au carnet sans
    surveillance. Le relevé porte alors `fill=None`, ce qui se lit comme un
    trou dans la mesure et non comme un ordre absent.
    """
    debut = now()
    limite_s = minutes * 60
    releves: list[Snapshot] = []

    while True:
        ecoule = now() - debut
        fill: Fill | None = None
        bid: float | None = None
        ask: float | None = None
        try:
            ouverts = await client.active_orders()
            fill = read_fill(order_id, ouverts)
            if fill is None:
                fill = read_fill(order_id, await client.order_history())
            book = await client.fetch_book(
                post.market.market_id, token_id=post.token_id
            )
            bid = book.best_bid.price if book.best_bid else None
            ask = book.best_ask.price if book.best_ask else None
        except (BinanceApiError, BinanceSchemaError) as exc:
            logger.warning("relevé à t+%.0fs incomplet : %s", ecoule, exc)

        releves.append(Snapshot(elapsed_s=ecoule, fill=fill, best_bid=bid, best_ask=ask))

        if fill is not None and fill.is_terminal:
            break
        if ecoule + interval_s > limite_s:
            break
        await sleep(interval_s)

    return tuple(releves)


async def run_probe(
    client: BinancePredictionClient,
    *,
    notional_usdt: float,
    minutes: int,
    interval_s: int,
    armed: bool,
    max_markets: int,
    now_ms: int | None = None,
    sleep=asyncio.sleep,
    now=time.monotonic,
) -> ProbeResult:
    """Le chemin complet. Rien ne bouge si `armed` est faux.

    Le devis est demandé dans les DEUX modes : c'est une lecture, et c'est la
    seule façon de voir ce que l'ordre coûterait vraiment avant de le passer.
    """
    marches = await client.list_markets(limit=max_markets)
    carnets = await client.fetch_books(marches)
    post, rejets = select_post(
        marches,
        carnets,
        notional_usdt=notional_usdt,
        # L'horloge de LA MACHINE avance de plusieurs secondes (mesuré +6 160 ms
        # ce jour). Ça ne fausse pas le filtre d'échéance, qui se compte en
        # minutes — mais le paramètre reste injectable pour que le filtre soit
        # testable sans dépendre de la date du jour.
        now_ms=int(time.time() * 1000) if now_ms is None else now_ms,
        minutes_needed=minutes,
    )
    if post is None:
        return ProbeResult(
            armed=armed,
            rejects=rejets,
            problem=(
                f"aucune branche éligible sur {len(marches)} marchés lus — "
                "voir les motifs de rejet"
            ),
        )

    limites = ExecutionLimits(
        max_total_usd=notional_usdt,
        max_per_market_usd=notional_usdt,
        max_orders=1,
    )
    trader = PredictionTrader(client, limits=limites, armed=armed)
    ordre = post.as_order()

    # CHEMIN LIMIT DIRECT, câblé le 2026-08-19. `get-quote` refuse
    # `orderType: LIMIT` (mesuré, y compris sans prix), donc la boucle
    # devis → ordre est impraticable ici : il n'y a pas de devis à obtenir.
    # On passe l'ordre directement à `place-order-bundle`, qui exige
    # `walletAddress` et sait donc peut-être le construire seul.
    #
    # Le portier tourne QUAND MÊME, en amont : c'est la seule protection qui
    # reste une fois le devis perdu.
    refus = gate([ordre], limits=limites)
    if refus.refused:
        _, motif = refus.refused[0]
        return ProbeResult(armed=armed, post=post, rejects=rejets, problem=motif)

    if not armed:
        return ProbeResult(
            armed=False,
            post=post,
            rejects=rejets,
            problem=(
                "DÉSARMÉE — et sur ce chemin il n'y a rien de plus à montrer : "
                "get-quote refuse les LIMIT, donc aucun coût ne peut être "
                "chiffré avant l'engagement. C'est --arm qui tranche"
            ),
        )

    try:
        brut = await trader.place_limit_direct(
            ordre, vendor=str(post.market.raw.get("vendor") or "") or None
        )
    except (BinanceApiError, BinanceSchemaError, ValueError) as exc:
        return ProbeResult(
            armed=True,
            post=post,
            rejects=rejets,
            problem=f"LIMIT direct refusé : {exc}",
        )

    devis = None
    order_id = read_order_id(brut)
    if order_id is None:
        # L'ordre EST parti : on le retrouve par sa signature plutôt que
        # d'abandonner. Ne rien faire ici laisserait un ordre vivant au carnet.
        ouverts = await client.active_orders()
        candidats = [
            str(o.get("orderId"))
            for o in ouverts
            if str(o.get("marketId") or "") == str(post.market.market_id)
        ]
        order_id = candidats[0] if len(candidats) == 1 else None

    if order_id is None:
        return ProbeResult(
            armed=True,
            post=post,
            quote=devis,
            rejects=rejets,
            placed_raw=brut,
            problem=(
                "ORDRE PASSÉ mais identifiant introuvable — il faut l'annuler "
                "à la main dans l'application Binance"
            ),
        )

    # NETTOYAGE GARANTI, corrigé le 2026-08-19. `observe_order` rattrape les
    # erreurs d'API relevé par relevé, mais rien d'autre : un Ctrl-C pendant
    # les dix minutes d'observation — le geste le plus naturel devant une
    # commande qui paraît figée — remontait en sautant l'annulation, et
    # laissait un ordre LIMIT vivant au carnet. Une position ouverte que
    # personne n'a décidé de prendre, sur un marché qui peut se résoudre
    # pendant qu'on regarde ailleurs.
    #
    # L'exception REMONTE quand même : la masquer ferait passer une mesure
    # interrompue pour une mesure réussie.
    releves: tuple[Snapshot, ...] = ()
    annule = False
    try:
        releves = await observe_order(
            client,
            order_id,
            post,
            minutes=minutes,
            interval_s=interval_s,
            sleep=sleep,
            now=now,
        )
    finally:
        dernier = next(
            (s.fill for s in reversed(releves) if s.fill is not None), None
        )
        if dernier is None or not dernier.is_terminal:
            try:
                await trader.batch_cancel([order_id])
                annule = True
            except (BinanceApiError, BinanceSchemaError) as exc:
                # Dernier recours : on n'a plus que la parole pour le dire.
                logger.error(
                    "ANNULATION REFUSÉE pour l'ordre %s (%s) — il est peut-être "
                    "ENCORE AU CARNET. À vérifier dans l'application Binance",
                    order_id,
                    exc,
                )

    return ProbeResult(
        armed=True,
        post=post,
        quote=devis,
        order_id=order_id,
        snapshots=releves,
        cancelled=annule,
        rejects=rejets,
        placed_raw=brut,
    )
