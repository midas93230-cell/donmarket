"""Tests de la boucle de tenue de marché Binance.

Le module est PUR : `eligible()` et `plan()` ne touchent ni le réseau ni le
disque. C'est délibéré — la partie qui décide où poser de l'argent doit être
vérifiable sans place de marché en face.
"""

from __future__ import annotations

import pytest

from donmarket.binance.mm import (
    MIN_MINUTES_LEFT,
    MIN_SPREAD_TICKS,
    Inventory,
    eligible,
    plan,
)
from donmarket.binance.model import PredictionBook, PredictionLevel, PredictionMarket

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
