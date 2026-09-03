"""Tests du coût de suivi — ce qu'un suiveur paie vraiment, pas ce qu'on lui annonce.

Ces tests existent pour une mesure promise le 2026-09-02 à `SpookyDegenaro`,
dont le « Whale Bot » annonce 67,4 % de réussite et +13,1 % de ROI sur des
paris suivis à un prix d'entrée moyen de ~0,60.

L'arithmétique de son bilan est cohérente (vérifié : 289-139-1 → espérance
+0,076/pari → +32,6 u contre +33,59 annoncés). Le seuil de rentabilité à 0,60
est 60 % de réussite, donc l'avantage est d'environ 7 points.

MAIS son 0,60 est le prix À L'ALERTE. Le pari de la baleine consomme le carnet ;
le suiveur remplit après, plus haut. À 0,62 l'avantage est divisé par deux, à
0,65 il n'existe plus. **Tout l'avantage tient dans cinq centimes**, et personne
ne publie ces cinq centimes. C'est le chiffre que ce module calcule.

Règle tenue ici, la même que dans `verifier_portefeuille.py` : quand le carnet
ne peut pas remplir la taille demandée, on ne rend PAS un prix moyen inventé
sur la partie manquante. On dit ce qui est remplissable et on le signale.
"""

from __future__ import annotations

import pytest

from donmarket.api.clob import Book, Level
from donmarket.analysis.slippage import breakeven_win_rate, taker_fill


def _carnet(asks=(), bids=()) -> Book:
    return Book(
        token_id="jeton",
        bids=tuple(Level(price=p, size=s) for p, s in bids),
        asks=tuple(Level(price=p, size=s) for p, s in asks),
        # Valeurs réelles de Polymarket, pas des zéros : un tick de 0,001 est
        # ce qui rend un glissement d'un palier presque invisible, et c'est
        # justement l'illusion que ce module mesure.
        tick_size=0.001,
        min_order_size=5.0,
    )


# ------------------------------------------------------------------ remplissage


def test_une_taille_qui_tient_au_meilleur_palier_ne_glisse_pas():
    carnet = _carnet(asks=[(0.60, 100)])
    f = taker_fill(carnet, "BUY", 50)
    assert f.effective == pytest.approx(0.60)
    assert f.slippage == pytest.approx(0.0)
    assert f.exhausted is False


def test_le_prix_effectif_est_la_MOYENNE_PONDEREE_des_paliers_consommes():
    # 50 parts à 0,60 puis 50 à 0,64 = 62 $ pour 100 parts = 0,62 de moyenne.
    carnet = _carnet(asks=[(0.60, 50), (0.64, 50)])
    f = taker_fill(carnet, "BUY", 100)
    assert f.effective == pytest.approx(0.62)
    assert f.quoted == pytest.approx(0.60)
    assert f.slippage == pytest.approx(0.02)


def test_LES_CINQ_CENTIMES_le_glissement_grandit_avec_la_taille():
    """Le cœur de la mesure promise. Même carnet, trois tailles."""
    carnet = _carnet(asks=[(0.60, 25), (0.62, 25), (0.66, 50), (0.75, 200)])
    petit = taker_fill(carnet, "BUY", 25)
    moyen = taker_fill(carnet, "BUY", 100)
    gros = taker_fill(carnet, "BUY", 300)
    assert petit.slippage < moyen.slippage < gros.slippage
    assert petit.slippage == pytest.approx(0.0)


def test_une_vente_glisse_vers_le_BAS_et_le_glissement_reste_positif():
    """Un glissement est toujours un COÛT, quel que soit le sens.

    Le signer par le sens de l'ordre ferait apparaître les ventes comme
    profitables au glissement — une erreur de signe qui inverserait la
    conclusion de toute la mesure.
    """
    carnet = _carnet(bids=[(0.60, 50), (0.56, 50)])
    f = taker_fill(carnet, "SELL", 100)
    assert f.effective == pytest.approx(0.58)
    assert f.quoted == pytest.approx(0.60)
    assert f.slippage == pytest.approx(0.02)


# -------------------------------------------------------- refus d'inventer


def test_un_carnet_trop_mince_NE_REND_PAS_un_prix_moyen_invente():
    """La règle de `verifier_portefeuille.py`, appliquée ici.

    Compléter mentalement les parts manquantes au dernier prix connu produit
    un chiffre précis et faux. On rend ce qui est remplissable, et on le dit.
    """
    carnet = _carnet(asks=[(0.60, 10)])
    f = taker_fill(carnet, "BUY", 100)
    assert f.exhausted is True
    assert f.filled == pytest.approx(10)
    assert f.effective == pytest.approx(0.60)  # sur les 10 parts, pas sur 100


def test_un_carnet_vide_ne_rend_rien_du_tout():
    assert taker_fill(_carnet(), "BUY", 10) is None


def test_une_taille_nulle_ou_negative_ne_rend_rien():
    carnet = _carnet(asks=[(0.60, 50)])
    assert taker_fill(carnet, "BUY", 0) is None
    assert taker_fill(carnet, "BUY", -5) is None


# ------------------------------------------------- ce que ça coûte au parieur


def test_le_seuil_de_rentabilite_est_le_prix_lui_meme():
    """Payer 0,60 pour recevoir 1,00 exige 60 % de réussite. Rien de plus."""
    assert breakeven_win_rate(0.60) == pytest.approx(0.60)
    assert breakeven_win_rate(0.75) == pytest.approx(0.75)


def test_LE_CHIFFRE_QUI_TUE_L_AVANTAGE_le_seuil_monte_avec_le_glissement():
    """67,4 % de réussite contre un seuil de 60 % = 7,4 points d'avantage.

    Le carnet fait monter le prix réel à 0,65, donc le seuil à 65 %, donc
    l'avantage à 2,4 points. C'est la phrase de la mesure : cinq centimes
    mangent les deux tiers de l'avantage.
    """
    carnet = _carnet(asks=[(0.60, 25), (0.70, 200)])
    f = taker_fill(carnet, "BUY", 100)
    seuil_annonce = breakeven_win_rate(f.quoted)
    seuil_reel = breakeven_win_rate(f.effective)
    assert seuil_reel > seuil_annonce
    avantage_annonce = 0.674 - seuil_annonce
    avantage_reel = 0.674 - seuil_reel
    assert avantage_reel < avantage_annonce / 2


def test_un_prix_hors_bornes_est_refuse():
    """Un prix Polymarket vit dans ]0, 1[. Hors de là, ce n'est pas un prix."""
    for muet in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            breakeven_win_rate(muet)
