"""Tests de la boucle de tenue de marché Binance.

Le module est PUR : `eligible()` et `plan()` ne touchent ni le réseau ni le
disque. C'est délibéré — la partie qui décide où poser de l'argent doit être
vérifiable sans place de marché en face.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from donmarket.binance.api import BinancePredictionClient, Credentials

from donmarket.binance.mm import (
    MIN_MINUTES_LEFT,
    MIN_SPREAD_TICKS,
    Inventory,
    LiveOrder,
    eligible,
    plan,
    read_inventory,
    read_live_orders,
    reconcile,
    run_market_maker,
)
from donmarket.binance.trade import LIMIT, PredictionOrder
from donmarket.binance.model import PredictionBook, PredictionLevel, PredictionMarket

FAKE = Credentials(api_key="cle_test", api_secret="secret_test")

MAINTENANT_MS = 1_787_000_000_000
LOIN = MAINTENANT_MS + 86_400_000


def _marche(market_id: int, *, tokens=("t1",), fin_ms=LOIN, statut="OPEN"):
    return PredictionMarket(
        market_id=market_id,
        status=statut,
        end_time_ms=fin_ms,
        outcome_token_ids=tuple(tokens),
    )


def _carnet(token_id: str, bid: float, ask: float, outcome: str = "Up"):
    return PredictionBook(
        market_id=1,
        bids=(PredictionLevel(price=bid, size=100.0),),
        asks=(PredictionLevel(price=ask, size=100.0),),
        token_id=token_id,
        raw={"outcome": outcome, "tokenId": token_id},
    )


def test_un_ecart_trop_serre_est_ecarte_avec_son_motif() -> None:
    """Sous deux pas, l'aller-retour rapporte moins qu'un pas de cotation.

    Le moindre décalage du carnet transforme alors le gain en perte, donc il
    n'y a rien à capturer — et le dire explicitement évite de chercher une
    panne là où c'est un filtre qui a tout pris.
    """
    rungs, rejets = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.51)}, now_ms=MAINTENANT_MS
    )
    assert rungs == []
    assert rejets and "pas" in rejets[0][1]


def test_un_ecart_suffisant_est_retenu() -> None:
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.52)}, now_ms=MAINTENANT_MS
    )
    assert len(rungs) == 1
    assert rungs[0].buy_price == 0.50
    assert rungs[0].sell_price == 0.52


def test_le_gain_brut_est_lecart_rapporte_au_prix_dachat() -> None:
    """Sans frais — `feeRateBps: 0` sur un LIMIT, mesuré le 2026-08-19 — le
    gain d'un aller-retour EST l'écart. C'est toute l'économie du module, et
    elle ne tenait pas tant qu'on payait 1,8 % de chaque côté."""
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.39, 0.40)}, now_ms=MAINTENANT_MS
    )
    # écart d'un seul pas : écarté. On vérifie la formule sur un écart valide.
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.40, 0.42)}, now_ms=MAINTENANT_MS
    )
    assert rungs[0].gross_edge == pytest.approx(0.02 / 0.40)


def test_une_echeance_proche_est_ecartee() -> None:
    """Une position non revendue à l'échéance n'est plus un stock, c'est un
    pari sur le résultat — et ce n'est pas la stratégie."""
    bientot = MAINTENANT_MS + (MIN_MINUTES_LEFT - 10) * 60_000
    rungs, rejets = eligible(
        [_marche(1, fin_ms=bientot)],
        {"t1": _carnet("t1", 0.50, 0.53)},
        now_ms=MAINTENANT_MS,
    )
    assert rungs == []
    assert "pari" in rejets[0][1]


def test_un_marche_ferme_est_ecarte() -> None:
    rungs, rejets = eligible(
        [_marche(1, statut="RESOLVED")],
        {"t1": _carnet("t1", 0.50, 0.53)},
        now_ms=MAINTENANT_MS,
    )
    assert rungs == []
    assert "RESOLVED" in rejets[0][1]


def test_le_classement_met_le_meilleur_ecart_devant() -> None:
    """À remplissage égal, l'écart EST le revenu."""
    rungs, _ = eligible(
        [_marche(1, tokens=("t1",)), _marche(2, tokens=("t2",))],
        {"t1": _carnet("t1", 0.50, 0.52), "t2": _carnet("t2", 0.50, 0.56)},
        now_ms=MAINTENANT_MS,
    )
    assert [r.token_id for r in rungs] == ["t2", "t1"]


# --- Planification ---------------------------------------------------------


def test_sans_inventaire_on_achete_au_bid() -> None:
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.53)}, now_ms=MAINTENANT_MS
    )
    ordres = plan(rungs, Inventory(), notional_per_market=2.0, max_markets=5)
    assert len(ordres) == 1
    assert ordres[0].side == "BUY"
    assert ordres[0].price == 0.50
    assert ordres[0].size == pytest.approx(4.0)


def test_avec_inventaire_on_revend_a_lask() -> None:
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.53)}, now_ms=MAINTENANT_MS
    )
    inv = Inventory()
    inv.add_fill("t1", shares=4.0, usdt=2.0)
    ordres = plan(rungs, inv, notional_per_market=2.0, max_markets=5)
    assert len(ordres) == 1
    assert ordres[0].side == "SELL"
    assert ordres[0].price == 0.53
    assert ordres[0].size == pytest.approx(4.0)


def test_on_ne_cote_jamais_les_deux_cotes_de_la_meme_branche() -> None:
    """ANCRAGE DE SÉCURITÉ. Poser un achat et une vente sur la même branche,
    c'est se croiser soi-même : le carnet nous apparie contre nous, et on PAIE
    l'écart au lieu de l'encaisser. Le seul cas où la stratégie perd de
    l'argent de façon garantie."""
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.53)}, now_ms=MAINTENANT_MS
    )
    inv = Inventory()
    inv.add_fill("t1", shares=4.0, usdt=2.0)
    ordres = plan(rungs, inv, notional_per_market=2.0, max_markets=5)
    cotes = {(o.token_id, o.side) for o in ordres}
    assert len(cotes) == 1, f"deux cotations sur la même branche : {cotes}"


def test_le_nombre_de_marches_est_plafonne() -> None:
    """Le capital est fini : coter partout reviendrait à promettre plus d'argent
    qu'on n'en a, et le refus tomberait à l'envoi, marché par marché."""
    marches = [_marche(i, tokens=(f"t{i}",)) for i in range(1, 6)]
    carnets = {f"t{i}": _carnet(f"t{i}", 0.50, 0.53) for i in range(1, 6)}
    rungs, _ = eligible(marches, carnets, now_ms=MAINTENANT_MS)
    ordres = plan(rungs, Inventory(), notional_per_market=2.0, max_markets=2)
    assert len(ordres) == 2


def test_un_notionnel_trop_petit_ne_produit_pas_dordre_vide() -> None:
    rungs, _ = eligible(
        [_marche(1)], {"t1": _carnet("t1", 0.50, 0.53)}, now_ms=MAINTENANT_MS
    )
    ordres = plan(rungs, Inventory(), notional_per_market=0.0, max_markets=5)
    assert ordres == []


def test_les_deux_branches_dun_marche_sont_cotables_separement() -> None:
    """Chaque branche a son carnet propre (mesuré le 2026-08-18) : les traiter
    comme une seule cotation perdrait la moitié des occasions."""
    rungs, _ = eligible(
        [_marche(1, tokens=("t1", "t2"))],
        {
            "t1": _carnet("t1", 0.50, 0.53, outcome="Up"),
            "t2": _carnet("t2", 0.44, 0.47, outcome="Down"),
        },
        now_ms=MAINTENANT_MS,
    )
    assert {r.outcome for r in rungs} == {"Up", "Down"}


# --- Réconciliation --------------------------------------------------------


def _voulu(price: float, side: str = "BUY", token: str = "t1", market: int = 1):
    return PredictionOrder(
        market_id=market, token_id=token, side=side,
        order_type=LIMIT, price=price, size=4.0,
    )


def _vivant(price: float, side: str = "BUY", token: str = "t1", market: int = 1):
    return LiveOrder(
        order_id=f"O-{price}", market_id=market, token_id=token,
        side=side, price=price,
    )


def test_un_ordre_deja_au_bon_prix_est_garde_pas_rejoue() -> None:
    """ANCRAGE. Réémettre un ordre identique lui fait perdre sa place dans la
    file — et la place dans la file est exactement ce qui décide d'être rempli
    ou non. C'est le seul avantage d'un teneur arrivé tôt ; le gaspiller à
    chaque tour reviendrait à ne jamais être servi."""
    a_poser, a_annuler, a_garder = reconcile([_voulu(0.50)], [_vivant(0.50)])
    assert a_poser == []
    assert a_annuler == []
    assert len(a_garder) == 1


def test_un_ordre_au_mauvais_prix_est_annule_et_repose() -> None:
    a_poser, a_annuler, a_garder = reconcile([_voulu(0.51)], [_vivant(0.50)])
    assert len(a_poser) == 1 and a_poser[0].price == 0.51
    assert len(a_annuler) == 1 and a_annuler[0].price == 0.50
    assert a_garder == []


def test_un_ordre_qui_nest_plus_voulu_est_annule() -> None:
    """Un marché sorti du plan — écart resserré, échéance approchée — laisse un
    ordre vivant qui n'est plus surveillé par personne."""
    a_poser, a_annuler, _ = reconcile([], [_vivant(0.50)])
    assert a_poser == []
    assert len(a_annuler) == 1


def test_lachat_et_la_vente_sont_des_cles_distinctes() -> None:
    """Un ordre d'achat vivant ne doit pas passer pour une vente voulue."""
    a_poser, a_annuler, _ = reconcile(
        [_voulu(0.53, side="SELL")], [_vivant(0.53, side="BUY")]
    )
    assert len(a_poser) == 1 and a_poser[0].side == "SELL"
    assert len(a_annuler) == 1 and a_annuler[0].side == "BUY"


def test_un_ordre_vivant_illisible_est_ignore_pas_devine() -> None:
    """Une ligne qu'on ne sait pas relire est une ligne qu'on ne saura pas
    annuler. L'inventer serait pire que l'ignorer."""
    lignes = [
        {"orderId": "A", "marketId": 1, "tokenId": "t1", "side": "BUY", "price": "0.5"},
        {"orderId": "B"},
        {"marketId": 2, "side": "SELL", "price": "x"},
    ]
    vivants = read_live_orders(lignes)
    assert len(vivants) == 1 and vivants[0].order_id == "A"


# --- Inventaire ------------------------------------------------------------


def test_un_portefeuille_vide_est_une_lecture_valide() -> None:
    inv, motif = read_inventory({"positions": []})
    assert motif is None and inv.shares == {}


def test_un_champ_positions_absent_est_signale_pas_pris_pour_du_vide() -> None:
    """« Je ne détiens rien » et « je n'ai pas su lire » mènent à des décisions
    opposées : la première fait acheter, la seconde doit faire s'abstenir."""
    inv, motif = read_inventory({"summary": {}})
    assert motif is not None


def test_une_position_illisible_suspend_la_vente() -> None:
    """NON VÉRIFIÉ EN DIRECT : `position/list` n'a jamais rendu de ligne non
    vide sur ce compte. Le schéma est donc supposé, et le code doit le DIRE
    plutôt que de vendre sur une supposition."""
    inv, motif = read_inventory({"positions": [{"quelque": "chose"}]})
    assert motif is not None and "illisibles" in motif


def test_une_position_lisible_alimente_linventaire() -> None:
    inv, motif = read_inventory(
        {"positions": [{"tokenId": "t1", "shareQty": "4.5", "costBasis": "2.0"}]}
    )
    assert motif is None
    assert inv.held("t1") == pytest.approx(4.5)


# --- Boucle ----------------------------------------------------------------


UNIVERS = {
    "marketTopics": [
        {
            "marketTopicId": 1,
            "vendor": "PREDICT_FUN",
            "endDate": LOIN,
            "feeRateBps": 200,
            "slippageBps": 1000,
            "markets": [
                {
                    "marketId": 1,
                    "title": "essai",
                    "tradingStatus": "OPEN",
                    "liquidity": "5000",
                    "outcomes": [
                        {"name": "Up", "price": "0.50", "tokenId": "t1", "index": 0},
                        {"name": "Down", "price": "0.47", "tokenId": "t2", "index": 1},
                    ],
                }
            ],
        }
    ],
    "total": 1,
    "hasMore": False,
}
CARNET = {
    "data": {
        "marketId": 1, "tokenId": "t1", "outcome": "Up",
        "bids": [{"price": "0.50", "size": "50"}],
        "asks": [{"price": "0.53", "size": "50"}],
    }
}
PORTEFEUILLE = {"data": [{"walletAddress": "0xabc", "walletId": "w-1"}]}


def _routeur(vus: list[str], *, positions, ordres_ouverts=None):
    def handler(request: httpx.Request) -> httpx.Response:
        chemin = request.url.path
        vus.append(chemin)
        if chemin.endswith("/wallet/list"):
            return httpx.Response(200, json=PORTEFEUILLE)
        if chemin.endswith("/market/list"):
            return httpx.Response(200, json=UNIVERS)
        if chemin.endswith("/order-book"):
            return httpx.Response(200, json=CARNET)
        if chemin.endswith("/position/list"):
            return httpx.Response(200, json=positions)
        if chemin.endswith("/order/list"):
            return httpx.Response(200, json={"orders": ordres_ouverts or []})
        if chemin.endswith("/trade/get-quote"):
            return httpx.Response(200, json={"data": {"quoteId": "Q"}})
        if chemin.endswith("/trade/place-order-bundle"):
            return httpx.Response(200, json={"data": {"orderId": "O-1"}})
        if chemin.endswith("/trade/batch-cancel"):
            return httpx.Response(200, json={"data": {}})
        raise AssertionError(f"route inattendue : {chemin}")

    return httpx.MockTransport(handler)


async def _dors(_s: float) -> None:
    return None


def _lance(transport, **kw):
    async def scenario():
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            horloge = iter(range(0, 100_000, 40))
            return await run_market_maker(
                client, bankroll=4.0, minutes=1, interval_s=30,
                max_markets=2, now_ms=MAINTENANT_MS,
                sleep=_dors, now=lambda: next(horloge), **kw,
            )

    return asyncio.run(scenario())


def test_desarmee_la_boucle_planifie_et_nenvoie_rien() -> None:
    vus: list[str] = []
    rapport = _lance(_routeur(vus, positions={"positions": []}), armed=False)
    assert rapport.armed is False
    assert rapport.placed > 0, "aucun ordre planifié : le plan est vide"
    assert not any("place-order-bundle" in c for c in vus)
    assert not any("batch-cancel" in c for c in vus)


def test_un_inventaire_illisible_fait_abstenir_la_boucle() -> None:
    """ANCRAGE DE SÉCURITÉ, et c'est le choix le plus important du module.

    Une machine qui achète sans savoir ce qu'elle détient ne sait pas revendre :
    elle accumule. L'accumulation n'est pas la stratégie, c'est son échec — et
    sur un marché de prédiction, une position gardée jusqu'à la résolution vaut
    0 ou 1, pas le prix payé.

    Le schéma des positions n'a jamais été observé rempli sur ce compte : il
    est donc supposé, et une supposition ne doit pas déclencher d'achat.
    """
    vus: list[str] = []
    rapport = _lance(
        _routeur(vus, positions={"positions": [{"champ": "inconnu"}]}), armed=False
    )
    assert rapport.inventory_problem is not None
    assert rapport.placed == 0, "des ordres ont été planifiés sur un inventaire illisible"


def test_armee_la_boucle_pose_puis_nettoie_ses_propres_ordres() -> None:
    """Un ordre laissé au carnet après l'arrêt est une position que plus
    personne ne surveille — mais seuls les NÔTRES sont à nettoyer.

    Le routeur devient volontairement dynamique : l'ordre que la boucle vient
    de poser apparaît ensuite dans les ordres ouverts, comme en vrai. Sans ça
    le test ne verrait jamais son propre ordre et ne prouverait rien.
    """
    vus: list[str] = []
    poses: list[str] = []

    def handler(request):
        chemin = request.url.path
        vus.append(chemin)
        if chemin.endswith("/wallet/list"):
            return httpx.Response(200, json=PORTEFEUILLE)
        if chemin.endswith("/market/list"):
            return httpx.Response(200, json=UNIVERS)
        if chemin.endswith("/order-book"):
            return httpx.Response(200, json=CARNET)
        if chemin.endswith("/position/list"):
            return httpx.Response(200, json={"positions": []})
        if chemin.endswith("/order/list"):
            lignes = [
                {"orderId": "O-ETRANGER", "marketId": 99, "tokenId": "tX",
                 "side": "BUY", "price": "0.70"}
            ] + [
                {"orderId": oid, "marketId": 1, "tokenId": "t1",
                 "side": "BUY", "price": "0.50"}
                for oid in poses
            ]
            return httpx.Response(200, json={"orders": lignes})
        if chemin.endswith("/trade/get-quote"):
            return httpx.Response(200, json={"data": {"quoteId": "Q"}})
        if chemin.endswith("/trade/place-order-bundle"):
            poses.append("O-1")
            return httpx.Response(200, json={"data": {"orderId": "O-1"}})
        if chemin.endswith("/trade/batch-cancel"):
            return httpx.Response(200, json={"data": {}})
        raise AssertionError(f"route inattendue : {chemin}")

    rapport = _lance(httpx.MockTransport(handler), armed=True)

    assert rapport.armed is True
    assert any("place-order-bundle" in c for c in vus), "aucun ordre posé"
    annulations = [c for c in vus if "batch-cancel" in c]
    assert annulations, "aucun nettoyage de nos propres ordres"
    assert "O-ETRANGER" not in rapport.left_open


def test_un_ordre_etranger_nest_jamais_annule() -> None:
    """ANCRAGE DE SÉCURITÉ, et il vient d'une vraie alerte.

    Le 2026-08-19, la boucle a tenté trente fois d'annuler l'ordre que le
    propriétaire du compte avait posé À LA MAIN dans l'application. Elle ne l'a
    pas fait uniquement parce que `batch-cancel` était cassé par ailleurs — un
    bug qui protégeait d'un autre bug, et qui venait d'être corrigé.

    Une machine n'a pas à défaire ce qu'un humain a décidé, et « je ne l'ai pas
    reconnu » n'est pas une raison de supprimer.
    """
    etranger = _vivant(0.70, token="tX", market=99)
    a_nous = _vivant(0.42, token="t1", market=1)

    _poser, a_annuler, _garder = reconcile(
        [], [etranger, a_nous], nous=frozenset({a_nous.order_id})
    )
    assert [o.order_id for o in a_annuler] == [a_nous.order_id]


def test_sans_perimetre_lancien_comportement_est_conserve() -> None:
    """`nous=None` reste réservé aux tests d'appariement : la boucle, elle,
    passe toujours son périmètre."""
    _poser, a_annuler, _garder = reconcile([], [_vivant(0.50)])
    assert len(a_annuler) == 1
