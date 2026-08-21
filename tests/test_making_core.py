"""Tests du coeur de tenue de marché Polymarket.

Le module est PUR : ni réseau ni disque. C'est délibéré — la partie qui décide
où poser de l'argent doit être vérifiable sans place de marché en face.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from donmarket.making.core import (
    MAX_SPREAD_TICKS,
    MIN_HOURS_TO_RESOLUTION,
    MIN_DEPTH_SHARES,
    DesiredOrder,
    Inventory,
    eligible,
    plan,
)


@dataclass(frozen=True)
class _Niveau:
    price: float
    size: float


@dataclass(frozen=True)
class _Carnet:
    """Carnet minimal, PIRE PRIX EN PREMIER comme sur Polymarket."""

    best_bid: float
    best_ask: float
    bids: tuple = field(default_factory=tuple)
    asks: tuple = field(default_factory=tuple)


MAINTENANT = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
LOINTAIN = MAINTENANT + timedelta(days=30)


@dataclass(frozen=True)
class _Marche:
    condition_id: str = "0xC"
    question: str = "une question ?"
    token_ids: tuple = ("t1",)
    min_order_size: float = 5.0
    min_tick_size: float = 0.01
    # Actif par defaut : les tests d ecart et de profondeur ne portent pas sur
    # le volume, et un defaut a zero les ferait tous echouer pour la mauvaise
    # raison.
    volume_24h: float = 100_000.0
    # Echeance lointaine par defaut : les tests d ecart et de profondeur ne
    # portent pas sur elle, et une valeur nulle les ferait echouer pour la
    # mauvaise raison.
    end_date: object = LOINTAIN


def _carnet(bid: float, ask: float, taille: float = 100.0) -> _Carnet:
    return _Carnet(
        best_bid=bid,
        best_ask=ask,
        # Pire prix en premier : le MEILLEUR est en dernier.
        bids=(_Niveau(bid - 0.05, 999.0), _Niveau(bid, taille)),
        asks=(_Niveau(ask + 0.05, 999.0), _Niveau(ask, taille)),
    )


def test_un_ecart_dun_seul_pas_est_ecarte() -> None:
    rungs, rejets = eligible([_Marche()], {"t1": _carnet(0.50, 0.51)}, capital_usd=8.73, now=MAINTENANT)
    assert rungs == []
    assert "rien à capturer" in rejets[0][1]


def test_un_ecart_beant_est_ecarte_et_cest_le_piege_principal() -> None:
    """LE PIÈGE DU 2026-07-28, mesuré à nouveau le 2026-08-20.

    Bid 0,002 contre ask 0,565 affiche 28 000 % de gain brut. Ce n'est pas une
    aubaine : c'est un carnet VIDE où personne ne viendra servir un ordre. Sans
    ce filtre, la mesure trouvait 1608 branches « cotables » au lieu de 351, et
    la quasi-totalité était ce mirage.
    """
    rungs, rejets = eligible(
        [_Marche()], {"t1": _carnet(0.20, 0.20 + (MAX_SPREAD_TICKS + 5) * 0.01)},
        capital_usd=8.73, now=MAINTENANT,
    )
    assert rungs == []
    assert "béant" in rejets[0][1]


def test_un_prix_extreme_est_ecarte() -> None:
    rungs, rejets = eligible([_Marche()], {"t1": _carnet(0.02, 0.06)}, capital_usd=8.73, now=MAINTENANT)
    assert rungs == []
    assert "hors bande" in rejets[0][1]


def test_un_carnet_sans_contrepartie_est_ecarte() -> None:
    """Sans taille en face, on ne peut ni être rempli à l'achat ni ressortir."""
    rungs, rejets = eligible(
        [_Marche()], {"t1": _carnet(0.50, 0.53, taille=MIN_DEPTH_SHARES - 1)},
        capital_usd=8.73, now=MAINTENANT,
    )
    assert rungs == []
    assert "contrepartie" in rejets[0][1]


def test_la_profondeur_se_lit_au_MEILLEUR_prix_pas_au_pire() -> None:
    """ANCRAGE. Les carnets Polymarket arrivent PIRE PRIX EN PREMIER (mesuré le
    2026-07-26). Lire `bids[0]` donnerait la profondeur du pire palier — ici
    999 parts — et laisserait passer un carnet sans contrepartie réelle."""
    creux = _Carnet(
        best_bid=0.50,
        best_ask=0.53,
        bids=(_Niveau(0.45, 999.0), _Niveau(0.50, 1.0)),
        asks=(_Niveau(0.58, 999.0), _Niveau(0.53, 1.0)),
    )
    rungs, rejets = eligible([_Marche()], {"t1": creux}, capital_usd=8.73, now=MAINTENANT)
    assert rungs == [], "la profondeur a été lue au pire prix"
    assert "contrepartie" in rejets[0][1]


def test_une_branche_saine_est_retenue_avec_son_ticket() -> None:
    rungs, _ = eligible([_Marche()], {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73, now=MAINTENANT)
    assert len(rungs) == 1
    rung = rungs[0]
    assert rung.buy_price == 0.20 and rung.sell_price == 0.24
    assert rung.ticket_usd == pytest.approx(1.0)  # 5 parts x 0,20
    assert rung.gross_edge == pytest.approx(0.04 / 0.20)


def test_un_ticket_au_dessus_du_capital_est_ecarte() -> None:
    rungs, rejets = eligible([_Marche()], {"t1": _carnet(0.50, 0.54)}, capital_usd=1.0, now=MAINTENANT)
    assert rungs == []
    assert "capital" in rejets[0][1]


def test_le_classement_met_le_meilleur_gain_devant() -> None:
    marches = [
        _Marche(condition_id="0xA", token_ids=("tA",)),
        _Marche(condition_id="0xB", token_ids=("tB",)),
    ]
    carnets = {"tA": _carnet(0.50, 0.53), "tB": _carnet(0.20, 0.24)}
    rungs, _ = eligible(marches, carnets, capital_usd=8.73, now=MAINTENANT)
    assert [r.token_id for r in rungs] == ["tB", "tA"]


# --- Planification ---------------------------------------------------------


def _rungs(bid=0.20, ask=0.24):
    rungs, _ = eligible([_Marche()], {"t1": _carnet(bid, ask)}, capital_usd=8.73, now=MAINTENANT)
    return rungs


def test_sans_inventaire_on_achete_au_bid() -> None:
    ordres = plan(_rungs(), Inventory(), notional_per_market=2.0, max_markets=3)
    assert len(ordres) == 1
    assert ordres[0].side == "BUY" and ordres[0].price == 0.20
    assert ordres[0].size == 10.0  # 2 $ / 0,20


def test_les_parts_sont_entieres() -> None:
    """Polymarket refuse une fraction de part ; l'arrondi doit se faire ici,
    pas au refus du serveur."""
    ordres = plan(_rungs(bid=0.13, ask=0.17), Inventory(),
                  notional_per_market=2.0, max_markets=3)
    assert ordres[0].size == float(int(ordres[0].size))


def test_avec_inventaire_on_revend_a_lask() -> None:
    inv = Inventory()
    inv.add("t1", 10.0)
    ordres = plan(_rungs(), inv, notional_per_market=2.0, max_markets=3)
    assert len(ordres) == 1
    assert ordres[0].side == "SELL" and ordres[0].price == 0.24


def test_on_ne_cote_jamais_les_deux_cotes_de_la_meme_branche() -> None:
    """ANCRAGE DE SÉCURITÉ. Poser un achat ET une vente sur la même branche,
    c'est se croiser soi-même : le carnet nous apparie contre nous et on PAIE
    l'écart au lieu de l'encaisser. Le seul cas où la stratégie perd à coup
    sûr."""
    inv = Inventory()
    inv.add("t1", 10.0)
    ordres = plan(_rungs(), inv, notional_per_market=2.0, max_markets=3)
    assert len({(o.token_id, o.side) for o in ordres}) == 1


def test_un_reliquat_sous_le_minimum_nest_pas_propose_a_la_vente() -> None:
    """Un ordre sous `orderMinSize` sera refusé : ne pas l'émettre vaut mieux
    que de le voir rejeter à l'envoi, marché par marché."""
    inv = Inventory()
    inv.add("t1", 2.0)
    ordres = plan(_rungs(), inv, notional_per_market=2.0, max_markets=3)
    assert ordres == []


def test_un_notionnel_trop_petit_ne_produit_pas_dordre() -> None:
    ordres = plan(_rungs(), Inventory(), notional_per_market=0.5, max_markets=3)
    assert ordres == []


def test_le_nombre_de_marches_est_plafonne() -> None:
    marches = [_Marche(condition_id=f"0x{i}", token_ids=(f"t{i}",)) for i in range(5)]
    carnets = {f"t{i}": _carnet(0.20, 0.24) for i in range(5)}
    rungs, _ = eligible(marches, carnets, capital_usd=8.73, now=MAINTENANT)
    ordres = plan(rungs, Inventory(), notional_per_market=2.0, max_markets=2)
    assert len(ordres) == 2


def test_le_cout_dun_ordre_est_prix_fois_taille() -> None:
    ordre = DesiredOrder(
        condition_id="0xC", token_id="t1", side="BUY", price=0.20, size=10.0
    )
    assert ordre.cost_usd == pytest.approx(2.0)


# --- Volume : le piege du carnet LENT --------------------------------------


def test_un_marche_endormi_est_ecarte() -> None:
    """MESURE DU 2026-08-21, et elle a coute une nuit. Un ordre pose au
    meilleur bid sur « Somaliland join the Abraham Accords » a passe QUATORZE
    HEURES au carnet sans le moindre remplissage.

    Le carnet n'etait pas vide : profondeur des deux cotes, ecart de 8 pas. Il
    etait LENT. C'est le piege suivant celui du carnet beant, et il est plus
    sournois -- un ecart large signale souvent qu'il ne se passe rien, puisque
    personne ne vient le resserrer.
    """
    endormi = _Marche()
    object.__setattr__(endormi, "volume_24h", 10.0)
    rungs, rejets = eligible(
        [endormi], {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73
    )
    assert rungs == []
    assert "endormi" in rejets[0][1]


def test_un_marche_actif_reste_retenu() -> None:
    actif = _Marche()
    object.__setattr__(actif, "volume_24h", 50_000.0)
    rungs, _ = eligible([actif], {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73, now=MAINTENANT)
    assert len(rungs) == 1


# --- Priorite dans la file : rejoindre ou ameliorer -------------------------


def test_par_defaut_on_rejoint_la_file_au_meilleur_prix() -> None:
    """Comportement historique, conserve par defaut : l'ecart entier est
    preserve, mais on passe DERRIERE tous ceux deja en file."""
    rungs, _ = eligible([_Marche()], {"t1": _carnet(0.20, 0.26)}, capital_usd=8.73, now=MAINTENANT)
    assert rungs[0].buy_price == 0.20 and rungs[0].sell_price == 0.26


def test_ameliorer_dun_pas_prend_la_priorite_et_coute_un_pas() -> None:
    """Le choix n'est pas tranche par le raisonnement mais par la mesure.

    Sur un ecart de 6 pas, ameliorer des deux cotes coute 2 pas -- un tiers du
    gain -- contre la priorite dans la file. Ca peut valoir tres cher ou rien
    du tout selon le taux de remplissage, qui n'est pas encore connu. D'ou un
    parametre plutot qu'une valeur en dur.
    """
    rungs, _ = eligible(
        [_Marche()], {"t1": _carnet(0.20, 0.26)}, capital_usd=8.73, improve_ticks=1
    )
    assert rungs[0].buy_price == pytest.approx(0.21)
    assert rungs[0].sell_price == pytest.approx(0.25)
    # Le gain brut diminue : c'est le prix de la priorite, et il doit se voir.
    assert rungs[0].gross_edge < (0.06 / 0.20)


def test_une_amelioration_trop_agressive_est_refusee() -> None:
    """ANCRAGE DE SECURITE. Ameliorer des deux cotes peut refermer l'ecart au
    point de se croiser SOI-MEME : le carnet nous apparie contre nous et on
    PAIE l'ecart au lieu de l'encaisser. C'est le seul cas ou la strategie perd
    de facon garantie -- mieux vaut ne pas coter."""
    rungs, rejets = eligible(
        [_Marche()], {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73, improve_ticks=2
    )
    assert rungs == []
    assert "croisent" in rejets[0][1]


# --- Echeance : la mesure la plus chere du projet ---------------------------

def _marche_echeance(heures: float):
    m = _Marche()
    object.__setattr__(m, "end_date", MAINTENANT + timedelta(hours=heures))
    return m


def test_un_marche_qui_se_resout_bientot_est_ecarte() -> None:
    """MESURE DU 2026-08-21, et elle a coute 10 $ sur 16.

    La boucle a achete sept positions en cinq heures ; QUATRE sont tombees a
    zero parce que le marche s'est resolu -- un match de Dota, un de foot, un
    tournoi CS2. Achetees a 0,11-0,13, revenues a 0,00.

    La tenue de marche suppose de pouvoir REVENDRE. Un marche qui se resout
    pendant qu'on le cote ne le permet pas : la position ne vaut plus un prix,
    elle vaut un resultat.
    """
    rungs, rejets = eligible(
        [_marche_echeance(1.0)], {"t1": _carnet(0.20, 0.24)},
        capital_usd=8.73, now=MAINTENANT,
    )
    assert rungs == []
    assert "pari" in rejets[0][1]


def test_un_marche_a_echeance_lointaine_reste_cotable() -> None:
    rungs, _ = eligible(
        [_marche_echeance(MIN_HOURS_TO_RESOLUTION + 10)],
        {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73, now=MAINTENANT,
    )
    assert len(rungs) == 1


def test_une_echeance_illisible_fait_renoncer() -> None:
    """Sur CE filtre, le doute doit faire renoncer. Une valeur optimiste par
    defaut ferait coter precisement les marches dont on ignore la fermeture."""
    muet = _Marche()
    object.__setattr__(muet, "end_date", None)
    rungs, rejets = eligible(
        [muet], {"t1": _carnet(0.20, 0.24)}, capital_usd=8.73, now=MAINTENANT
    )
    assert rungs == []
    assert "illisible" in rejets[0][1]


# --- Sorties : la correction la plus importante -----------------------------


def test_toute_position_recoit_un_ordre_de_vente() -> None:
    """LA CORRECTION QUI A COUTE 10 $ SUR 16.

    `plan()` ne parcourait que les branches ELIGIBLES. Une position dont le
    marche sort des filtres -- echeance qui approche, ecart qui se resserre,
    volume qui tombe -- disparait de cette liste et ne recoit donc JAMAIS
    d'ordre de vente. Elle devient orpheline, et sur un marche de prediction
    une position orpheline finit par valoir 0 ou 1. Quatre l'ont fait le meme
    jour.

    L'eligibilite gouverne l'ACHAT, jamais la SORTIE.
    """
    from donmarket.making.core import exits

    inv = Inventory()
    inv.add("t-orphelin", 20.0)
    # Aucune branche eligible pour ce jeton : le carnet suffit.
    ordres, soucis = exits(inv, {"t-orphelin": _carnet(0.30, 0.34)})
    assert soucis == []
    assert len(ordres) == 1
    assert ordres[0].side == "SELL" and ordres[0].price == 0.34
    assert ordres[0].size == pytest.approx(20.0)


def test_une_position_sans_carnet_est_signalee_pas_oubliee() -> None:
    """Une position qu'on ne sait pas solder doit etre DITE. La taire
    reviendrait a l'abandonner une seconde fois."""
    from donmarket.making.core import exits

    inv = Inventory()
    inv.add("t-muet", 20.0)
    ordres, soucis = exits(inv, {})
    assert ordres == []
    assert soucis and "sortie impossible" in soucis[0][1]


def test_un_reliquat_sous_le_minimum_est_signale() -> None:
    from donmarket.making.core import exits

    inv = Inventory()
    inv.add("t1", 2.0)
    ordres, soucis = exits(inv, {"t1": _carnet(0.30, 0.34)})
    assert ordres == []
    assert soucis and "invendable" in soucis[0][1]
