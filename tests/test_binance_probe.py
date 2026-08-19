"""Tests de la sonde de remplissage teneur (`donmarket/binance/probe.py`).

Ce qui est testé ici n'est PAS « l'API répond » : c'est la logique qui décide
où poser un ordre, et la lecture du remplissage. Les 60 tests d'API existants
couvraient la mécanique (signature, schéma, unités) sans jamais couvrir la
DÉCISION — exactement le trou qui avait laissé passer un modèle mal calibré le
2026-08-09.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from donmarket.binance.api import BinancePredictionClient, Credentials
from donmarket.binance.model import (
    PredictionBook,
    PredictionLevel,
    PredictionMarket,
)
from donmarket.binance.probe import (
    END_MARGIN_MINUTES,
    Fill,
    maker_price,
    observe_order,
    read_fill,
    read_order_id,
    run_probe,
    select_post,
)

FAKE = Credentials(api_key="cle-de-test", api_secret="secret-de-test")
MAINTENANT_MS = 1_787_000_000_000


def _carnet(bid: float, ask: float, *, outcome: str = "Up", token: str = "t1") -> PredictionBook:
    return PredictionBook(
        market_id=1,
        bids=(PredictionLevel(price=bid, size=100.0),),
        asks=(PredictionLevel(price=ask, size=100.0),),
        token_id=token,
        raw={"outcome": outcome},
    )


def _marche(
    market_id: int = 1,
    *,
    minutes_restantes: int = 60,
    volume: float | None = 1000.0,
    statut: str = "OPEN",
    tokens: tuple[str, ...] = ("t1",),
) -> PredictionMarket:
    return PredictionMarket(
        market_id=market_id,
        title=f"marché {market_id}",
        status=statut,
        end_time_ms=MAINTENANT_MS + minutes_restantes * 60_000,
        volume_usdt=volume,
        outcome_token_ids=tokens,
    )


# --------------------------------------------------------------------------
# Prix teneur
# --------------------------------------------------------------------------


def test_le_prix_ameliore_le_bid_quand_lecart_le_permet() -> None:
    """Écart de 3 ticks : on se place seul devant, un tick au-dessus."""
    assert maker_price(_carnet(0.20, 0.23)) == pytest.approx(0.21)


def test_le_prix_ne_franchit_jamais_lask() -> None:
    """Écart d'un seul tick : améliorer TOUCHERAIT l'ask et nous rendrait
    preneur — on rejoint la file au meilleur bid à la place."""
    assert maker_price(_carnet(0.22, 0.23)) == pytest.approx(0.22)


def test_un_carnet_a_un_seul_cote_ne_rend_aucun_prix() -> None:
    """Sans ask, « rester sous l'ask » n'a pas de sens : None, pas 0."""
    unilateral = PredictionBook(
        market_id=1, bids=(PredictionLevel(price=0.4, size=10.0),), asks=()
    )
    assert maker_price(unilateral) is None


def test_le_prix_ne_depasse_jamais_le_plafond_de_probabilite() -> None:
    """Bid 0,99 : améliorer donnerait 1,00, qui n'est pas une probabilité. On
    rejoint la file à 0,99 — le refus ne doit surtout pas remonter plus loin
    sous forme d'exception depuis le modèle d'ordre."""
    prix = maker_price(_carnet(0.99, 1.0))
    assert prix == pytest.approx(0.99)
    assert prix is not None and prix < 1.0


# --------------------------------------------------------------------------
# Sélection
# --------------------------------------------------------------------------


def test_la_taille_decoule_du_notionnel_voulu() -> None:
    post, _ = select_post(
        [_marche()],
        {"t1": _carnet(0.20, 0.25)},
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is not None
    assert post.price == pytest.approx(0.21)
    assert post.size == pytest.approx(round(2.0 / 0.21, 2))
    assert post.notional_usdt == pytest.approx(2.0, abs=0.01)


def test_une_echeance_trop_proche_est_refusee_avec_son_motif() -> None:
    """Un marché qui se résout pendant la mesure ne mesure rien."""
    trop_court = _marche(7, minutes_restantes=10 + END_MARGIN_MINUTES - 1)
    post, rejets = select_post(
        [trop_court],
        {"t1": _carnet(0.20, 0.25)},
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is None
    assert rejets and rejets[0][0] == 7
    assert "échéance" in rejets[0][1]


def test_un_marche_ferme_est_refuse_avant_tout_calcul() -> None:
    post, rejets = select_post(
        [_marche(9, statut="CLOSED")],
        {"t1": _carnet(0.20, 0.25)},
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is None
    assert rejets == ((9, "statut CLOSED"),)


def test_le_classement_suit_le_volume_pas_lordre_de_lecture() -> None:
    """Le remplissage dépend du flux qui frappe l'ordre : c'est le volume qui
    l'approche, et il doit primer sur l'ordre où l'API a rendu les lignes."""
    petit = _marche(1, volume=10.0, tokens=("a",))
    gros = _marche(2, volume=9999.0, tokens=("b",))
    post, _ = select_post(
        [petit, gros],
        {"a": _carnet(0.20, 0.25, token="a"), "b": _carnet(0.30, 0.35, token="b")},
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is not None
    assert post.market.market_id == 2


def test_chaque_branche_est_un_candidat_distinct() -> None:
    """Deux carnets par marché (mesuré 2026-08-18) : les deux branches doivent
    concourir, sinon l'une est perdue en silence."""
    marche = _marche(3, tokens=("up", "down"))
    post, rejets = select_post(
        [marche],
        {
            "up": _carnet(0.70, 0.79, outcome="Up", token="up"),
            "down": _carnet(0.21, 0.30, outcome="Down", token="down"),
        },
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is not None
    assert rejets == ()
    assert post.outcome in {"Up", "Down"}


def test_un_carnet_manquant_est_un_rejet_motive_pas_un_plantage() -> None:
    post, rejets = select_post(
        [_marche(4, tokens=("absent",))],
        {},
        notional_usdt=2.0,
        now_ms=MAINTENANT_MS,
        minutes_needed=10,
    )
    assert post is None
    assert rejets == ((4, "carnet absent"),)


# --------------------------------------------------------------------------
# Lecture du remplissage
# --------------------------------------------------------------------------


def test_fill_percentage_est_une_fraction_pas_un_pourcentage() -> None:
    """Mesuré sur les 6 ordres du compte : un ordre plein porte `"1"`."""
    fill = read_fill(
        "26081800001812494226",
        [
            {
                "orderId": "26081800001812494226",
                "status": "FILLED",
                "filledShareQty": "8.21",
                "filledUsdtAmount": "7",
                "fillPercentage": "1",
            }
        ],
    )
    assert fill is not None
    assert fill.fraction == pytest.approx(1.0)
    assert fill.filled_shares == pytest.approx(8.21)
    assert fill.is_terminal


def test_un_ordre_absent_de_la_liste_rend_none() -> None:
    assert read_fill("X", [{"orderId": "Y", "status": "FILLED"}]) is None


def test_un_statut_inconnu_nest_pas_traite_comme_terminal() -> None:
    """Arrêter la mesure sur un nom mal deviné la tronquerait sans le dire."""
    assert Fill(status="SOMETHING_NEW", filled_shares=0, filled_usdt=0, fraction=0).is_terminal is False
    assert Fill(status="NEW", filled_shares=0, filled_usdt=0, fraction=0).is_terminal is False


def test_lidentifiant_dordre_se_lit_sous_son_enveloppe() -> None:
    assert read_order_id({"data": {"orderId": "42"}}) == "42"
    assert read_order_id({"orderIds": ["7", "8"]}) == "7"
    assert read_order_id({"data": {"orderId": 91}}) == "91"


def test_un_identifiant_introuvable_rend_none_sans_lever() -> None:
    """Lever ici perdrait la trace d'un ordre DÉJÀ passé."""
    assert read_order_id({"data": {"status": "OK"}}) is None


# --------------------------------------------------------------------------
# Boucle d'observation et chemin complet
# --------------------------------------------------------------------------


def _client(handler) -> BinancePredictionClient:
    return BinancePredictionClient(
        credentials=FAKE, transport=httpx.MockTransport(handler)
    )


# `walletId` ajouté le 2026-08-19 : `place-order-bundle` l'exige en plus de
# l'adresse, et sans lui la sonde ne peut plus poser d'ordre du tout.
WALLET = {"data": [{"walletAddress": "0xabc", "walletId": "w-1", "chainId": "56"}]}


def test_lobservation_sarrete_des_que_letat_est_terminal() -> None:
    """Un ordre rempli n'a plus rien à observer : continuer à interroger
    l'API pour rien allongerait la mesure sans l'améliorer."""
    appels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        appels.append(request.url.path)
        if request.url.path.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET)
        if request.url.path.endswith("/order/list"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "orderId": "42",
                            "status": "FILLED",
                            "filledShareQty": "9.5",
                            "filledUsdtAmount": "2",
                            "fillPercentage": "1",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/order-book"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "marketId": 1,
                        "bids": [{"price": "0.21", "size": "5"}],
                        "asks": [{"price": "0.25", "size": "5"}],
                    }
                },
            )
        raise AssertionError(f"route inattendue : {request.url.path}")

    dodos: list[float] = []

    async def faux_sleep(duree: float) -> None:
        dodos.append(duree)

    async def scenario():
        post, _ = select_post(
            [_marche()],
            {"t1": _carnet(0.20, 0.25)},
            notional_usdt=2.0,
            now_ms=MAINTENANT_MS,
            minutes_needed=10,
        )
        async with _client(handler) as client:
            return await observe_order(
                client, "42", post, minutes=10, interval_s=20, sleep=faux_sleep
            )

    releves = asyncio.run(scenario())
    assert len(releves) == 1
    assert releves[0].fill is not None and releves[0].fill.is_terminal
    assert dodos == []  # aucun sommeil : on n'a pas rebouclé


def test_une_panne_dapi_pendant_lobservation_ne_perd_pas_lordre() -> None:
    """L'ordre est déjà posé : abandonner la boucle le laisserait au carnet
    sans surveillance. Le relevé porte alors un trou, pas une absence."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET)
        return httpx.Response(200, json={"code": "-3026", "msg": "boom"})

    async def faux_sleep(duree: float) -> None:
        return None

    horloge = iter([0.0, 0.0, 600.0, 600.0])

    async def scenario():
        post, _ = select_post(
            [_marche()],
            {"t1": _carnet(0.20, 0.25)},
            notional_usdt=2.0,
            now_ms=MAINTENANT_MS,
            minutes_needed=10,
        )
        async with _client(handler) as client:
            return await observe_order(
                client,
                "42",
                post,
                minutes=10,
                interval_s=20,
                sleep=faux_sleep,
                now=lambda: next(horloge),
            )

    releves = asyncio.run(scenario())
    assert releves
    assert all(r.fill is None for r in releves)


def test_desarmee_la_sonde_obtient_un_devis_et_ne_passe_rien() -> None:
    chemins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chemins.append(request.url.path)
        if request.url.path.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET)
        if request.url.path.endswith("/market/list"):
            # Enveloppe RÉELLE : `marketTopics` au premier niveau, pas sous
            # `data` (mesuré le 2026-08-18).
            return httpx.Response(
                200,
                json={
                    "marketTopics": [
                        {
                            "marketTopicId": 99,
                            "title": "BTC Up or Down",
                            "endDate": MAINTENANT_MS + 3_600_000,
                            "feeRateBps": 200,
                            "tradeVolume": "500",
                            "markets": [
                                {
                                    "marketId": 1,
                                    "tradingStatus": "OPEN",
                                    "outcomes": [
                                        {"name": "Up", "tokenId": "t1"},
                                    ],
                                }
                            ],
                        }
                    ],
                    "total": 1,
                    "hasMore": False,
                },
            )
        if request.url.path.endswith("/order-book"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "marketId": 1,
                        "tokenId": "t1",
                        "outcome": "Up",
                        "bids": [{"price": "0.20", "size": "50"}],
                        "asks": [{"price": "0.25", "size": "50"}],
                    }
                },
            )
        if request.url.path.endswith("/trade/get-quote"):
            return httpx.Response(
                200, json={"data": {"quoteId": "Q-9", "amountOut": "9.5"}}
            )
        raise AssertionError(f"route inattendue : {request.url.path}")

    async def scenario():
        async with _client(handler) as client:
            return await run_probe(
                client,
                notional_usdt=2.0,
                minutes=10,
                interval_s=20,
                armed=False,
                max_markets=5,
                now_ms=MAINTENANT_MS,
            )

    resultat = asyncio.run(scenario())
    assert resultat.armed is False
    assert resultat.post is not None
    assert resultat.order_id is None
    # RÉTABLI le 2026-08-19 (soir). Ce test avait été réécrit dans la journée
    # sur la conclusion — fausse — que `get-quote` refusait les LIMIT. Le refus
    # venait du nom du champ de prix : `priceLimit` et non `price`. Le devis
    # existe donc bien en mode désarmé, et c'est lui qui chiffre le coût avant
    # tout engagement.
    assert resultat.quote is not None and resultat.quote.quote_id == 'Q-9'
    assert resultat.problem is None
    assert all("place-order-bundle" not in chemin for chemin in chemins)


# --------------------------------------------------------------------------
# Portier : le plafond par marché doit VOIR les marchés Binance
# --------------------------------------------------------------------------


def test_le_plafond_par_marche_distingue_deux_marches_binance() -> None:
    """Le portier groupait sur `condition_id`, absent des ordres Binance : les
    deux ordres tombaient dans la même clé et le second était refusé alors
    qu'il porte sur un AUTRE marché."""
    from donmarket.binance.trade import PredictionOrder
    from donmarket.execute.limits import ExecutionLimits, gate

    limites = ExecutionLimits(max_total_usd=4.0, max_per_market_usd=2.0, max_orders=2)
    decision = gate(
        [
            PredictionOrder(market_id=1, token_id="a", price=0.5, size=4.0),
            PredictionOrder(market_id=2, token_id="b", price=0.5, size=4.0),
        ],
        limits=limites,
    )
    assert decision.refused_count == 0
    assert decision.allowed_count == 2


def test_le_plafond_par_marche_refuse_toujours_la_concentration() -> None:
    from donmarket.binance.trade import PredictionOrder
    from donmarket.execute.limits import ExecutionLimits, gate

    limites = ExecutionLimits(max_total_usd=4.0, max_per_market_usd=2.0, max_orders=2)
    decision = gate(
        [
            PredictionOrder(market_id=1, token_id="a", price=0.5, size=4.0),
            PredictionOrder(market_id=1, token_id="b", price=0.5, size=4.0),
        ],
        limits=limites,
    )
    assert decision.allowed_count == 1
    assert decision.refused_count == 1
    assert "ce marché" in decision.refused[0][1]


# --------------------------------------------------------------------------
# Les DEUX champs d'état d'un marché (mesuré 2026-08-19)
# --------------------------------------------------------------------------


def test_la_negociabilite_se_lit_sur_tradingstatus_pas_sur_status() -> None:
    """Mesuré sur 241 marchés : chacun porte `status = REGISTERED` (cycle de
    vie) ET `tradingStatus = OPEN` (négociabilité). Lire `status` d'abord
    faisait rejeter l'univers entier, avec un motif qui accusait le marché."""
    from donmarket.binance.model import parse_market

    marche = parse_market(
        {
            "marketId": 7078114,
            "status": "REGISTERED",
            "tradingStatus": "OPEN",
            "outcomes": [{"name": "Up", "tokenId": "t1"}],
        }
    )
    assert marche.status == "OPEN"


def test_un_marche_sans_tradingstatus_retombe_sur_status() -> None:
    """Les routes voisines n'exposent pas toutes `tradingStatus` : le repli
    doit rester, sinon leur état devient illisible."""
    from donmarket.binance.model import parse_market

    marche = parse_market({"marketId": 1, "status": "CLOSED"})
    assert marche.status == "CLOSED"


def test_une_panne_reseau_pendant_lobservation_annule_quand_meme_lordre() -> None:
    """ANCRAGE de la garantie EXISTANTE, ajouté en revue le 2026-08-19.

    `observe_order` attrape déjà les erreurs d'API relevé par relevé, et c'est
    le bon choix : abandonner la boucle laisserait l'ordre au carnet sans
    surveillance. Ce test fige ce comportement, qui n'était gardé par rien —
    quelqu'un qui « nettoierait » ce try/except en le croyant trop large
    rouvrirait le trou sans qu'aucun test ne tombe.
    """
    chemins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        chemins.append(chemin)
        if chemin.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET)
        if chemin.endswith("/market/list"):
            return httpx.Response(
                200,
                json={
                    "marketTopics": [
                        {
                            "marketTopicId": 1,
                            "vendor": "PREDICT_FUN",
                            "endDate": MAINTENANT_MS + 86_400_000,
                            "feeRateBps": 200,
                            "slippageBps": 1000,
                            "markets": [
                                {
                                    "marketId": 1,
                                    "title": "essai",
                                    "tradingStatus": "OPEN",
                                    "liquidity": "5000",
                                    "outcomes": [
                                        {"name": "Up", "price": "0.22",
                                         "tokenId": "t1", "index": 0},
                                        {"name": "Down", "price": "0.78",
                                         "tokenId": "t2", "index": 1},
                                    ],
                                }
                            ],
                        }
                    ],
                    "total": 1,
                    "hasMore": False,
                },
            )
        if chemin.endswith("/order-book"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "marketId": 1, "tokenId": "t1", "outcome": "Up",
                        "bids": [{"price": "0.20", "size": "50"}],
                        "asks": [{"price": "0.25", "size": "50"}],
                    }
                },
            )
        if chemin.endswith("/trade/get-quote"):
            return httpx.Response(200, json={"data": {"quoteId": "Q-9"}})
        if chemin.endswith("/trade/place-order-bundle"):
            return httpx.Response(200, json={"data": {"orderId": "O-1"}})
        if chemin.endswith("/trade/batch-cancel"):
            return httpx.Response(200, json={"data": {"cancelled": ["O-1"]}})
        # LA PANNE : l'observation tombe dès le premier relevé.
        if chemin.endswith("/order/list"):
            raise httpx.ConnectError("réseau coupé pendant l'observation")
        raise AssertionError(f"route inattendue : {chemin}")

    async def scenario():
        async with _client(handler) as client:
            return await run_probe(
                client,
                notional_usdt=2.0,
                # Court exprès : chaque relevé en panne consomme le réessai
                # x3 du client, dont l'attente est RÉELLE (elle ne passe pas
                # par le `sleep` injecté). Dix minutes de mesure feraient deux
                # minutes de test pour la même garantie.
                minutes=1,
                interval_s=20,
                armed=True,
                max_markets=5,
                now_ms=MAINTENANT_MS,
                sleep=_faux_sleep_zero,
                # Horloge FICTIVE qui avance : avec sleep=0 et un temps réel,
                # la boucle d'observation ne finit jamais.
                now=lambda: next(_horloge),
            )

    try:
        asyncio.run(scenario())
    except Exception:
        # Que l'exception remonte est acceptable ; laisser l'ordre au carnet
        # ne l'est pas. C'est le nettoyage qui est testé, pas le silence.
        pass

    assert any("batch-cancel" in c for c in chemins), (
        "ordre passé puis panne pendant l'observation : le reliquat n'a JAMAIS "
        f"été annulé. Routes appelées : {chemins}"
    )


async def _faux_sleep_zero(_secondes: float) -> None:
    return None


_horloge = iter(range(0, 100_000, 30))


def test_une_interruption_annule_le_reliquat_avant_de_remonter() -> None:
    """LE TROU RÉEL, plus étroit que celui d'abord annoncé.

    `observe_order` ne rattrape que les erreurs d'API. Tout le reste — Ctrl-C
    pendant les dix minutes d'observation, ce qui est le geste le plus naturel
    du monde devant une commande qui semble figée, ou un simple défaut de
    programmation dans le décodage — remonte et saute l'annulation. L'ordre
    reste alors vivant au carnet.

    Le nettoyage doit donc être garanti par `finally`, pas par l'absence
    d'imprévu. L'exception, elle, a le droit de remonter : la masquer ferait
    croire à une mesure réussie.
    """
    chemins: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        chemins.append(chemin)
        if chemin.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET)
        if chemin.endswith("/market/list"):
            return httpx.Response(200, json=_UN_MARCHE_OUVERT)
        if chemin.endswith("/order-book"):
            return httpx.Response(200, json=_UN_CARNET)
        if chemin.endswith("/trade/get-quote"):
            return httpx.Response(200, json={"data": {"quoteId": "Q-9"}})
        if chemin.endswith("/trade/place-order-bundle"):
            return httpx.Response(200, json={"data": {"orderId": "O-1"}})
        if chemin.endswith("/trade/batch-cancel"):
            return httpx.Response(200, json={"data": {"cancelled": ["O-1"]}})
        raise AssertionError(f"route inattendue : {chemin}")

    async def observation_interrompue(*_a, **_kw):
        raise KeyboardInterrupt("l'utilisateur a fait Ctrl-C")

    async def scenario():
        async with _client(handler) as client:
            return await run_probe(
                client,
                notional_usdt=2.0,
                minutes=1,
                interval_s=20,
                armed=True,
                max_markets=5,
                now_ms=MAINTENANT_MS,
            )

    import donmarket.binance.probe as module

    original = module.observe_order
    module.observe_order = observation_interrompue
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(scenario())
    finally:
        module.observe_order = original

    assert any("batch-cancel" in c for c in chemins), (
        "ordre passé puis interruption : le reliquat est resté au carnet. "
        f"Routes appelées : {chemins}"
    )


_UN_MARCHE_OUVERT = {
    "marketTopics": [
        {
            "marketTopicId": 1,
            "vendor": "PREDICT_FUN",
            "endDate": MAINTENANT_MS + 86_400_000,
            "feeRateBps": 200,
            "slippageBps": 1000,
            "markets": [
                {
                    "marketId": 1,
                    "title": "essai",
                    "tradingStatus": "OPEN",
                    "liquidity": "5000",
                    "outcomes": [
                        {"name": "Up", "price": "0.22", "tokenId": "t1", "index": 0},
                        {"name": "Down", "price": "0.78", "tokenId": "t2", "index": 1},
                    ],
                }
            ],
        }
    ],
    "total": 1,
    "hasMore": False,
}
_UN_CARNET = {
    "data": {
        "marketId": 1,
        "tokenId": "t1",
        "outcome": "Up",
        "bids": [{"price": "0.20", "size": "50"}],
        "asks": [{"price": "0.25", "size": "50"}],
    }
}


# --- Sélection : le rebate dépend du PRIX, pas seulement du volume ----------


def _post_factice(market_id: int, prix: float, volume: float) -> object:
    """Un candidat minimal, tel que `select_post` en fabrique."""
    from donmarket.binance.model import PredictionBook, PredictionLevel

    return PredictionBook(
        market_id=market_id,
        bids=(PredictionLevel(price=prix - 0.01, size=100.0),),
        asks=(PredictionLevel(price=prix + 0.01, size=100.0),),
        token_id=f"t{market_id}",
        raw={"outcome": "Up", "tokenId": f"t{market_id}"},
    )


def test_la_selection_prefere_un_prix_proche_de_la_moitie() -> None:
    """Le rebate teneur vaut `0,25 × taux × min(p, 1−p)`.

    À p = 0,04 il vaut donc 25 fois MOINS qu'à p = 0,50, à volume égal. Le
    classement par volume seul a fait choisir « aliens exist before 2027 » à
    0,04 le 2026-08-19 — le pire marché possible pour la stratégie, sur le
    critère qui décide de tout le revenu.

    Le volume reste un critère (il approche le flux qui viendra nous frapper),
    mais il ne peut plus écraser un prix qui divise le gain par 25.
    """
    from donmarket.binance.model import PredictionMarket
    from donmarket.binance.probe import select_post

    lointain = PredictionMarket(
        market_id=1, status="OPEN", end_time_ms=MAINTENANT_MS + 86_400_000,
        volume_usdt=1_000_000.0, outcome_token_ids=("t1",),
    )
    central = PredictionMarket(
        market_id=2, status="OPEN", end_time_ms=MAINTENANT_MS + 86_400_000,
        volume_usdt=1_000.0, outcome_token_ids=("t2",),
    )
    carnets = {"t1": _post_factice(1, 0.04, 0), "t2": _post_factice(2, 0.50, 0)}

    choisi, _rejets = select_post(
        [lointain, central], carnets,
        notional_usdt=2.0, now_ms=MAINTENANT_MS, minutes_needed=10,
    )
    assert choisi is not None
    assert choisi.market.market_id == 2, (
        "un marché à 0,04 avec 1000× le volume a été préféré à un marché à "
        "0,50 : le classement ignore encore le prix, donc le rebate"
    )
