"""Tests du verificateur de portefeuille — l'outil doit etre juste ou se taire.

Cet outil existe pour dire si l'historique annonce par un portefeuille est
reel. Un verificateur qui se trompe est pire qu'inutile : il donne l'autorite
de la mesure a un chiffre faux. D'ou ces tests, sur les trois erreurs qui l'ont
reellement fait mentir le 2026-08-29, sur notre propre portefeuille.

1. Il annoncait +16,16 $ de gain realise sur un compte dont on croyait qu'il
   perdait. Cause : on solde ses GAGNANTES (il y a un acheteur) et on garde ses
   PERDANTES (pas de contrepartie). Les pertes s'accumulent donc dans les
   positions ouvertes et ne sont jamais comptees. Le gain realise est une
   borne haute, jamais un resultat — et le plancher est le seul chiffre garanti.

2. Un REDEEM ne porte pas toujours ses parts : le remboursement Solana du 24/08
   rend 4,99 $ avec `shares` vide. Sans rattrapage, une position REMBOURSEE,
   donc fermee par definition, restait comptee ouverte pour toujours.

3. Les depots sont le denominateur. « 313 $ devenus 438 k$ » ne veut rien dire
   sans eux, et ils ne doivent jamais etre confondus avec le capital deploye :
   notre propre compte a 8,01 $ de depots pour 24,56 $ d'achats cumules, et
   confondre les deux nous a fait croire pendant des jours qu'on perdait 25 %
   quand on gagnait 41 %.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent


@pytest.fixture
def outil():
    chemin = RACINE / "tools" / "verifier_portefeuille.py"
    spec = importlib.util.spec_from_file_location("verifier_portefeuille", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Acte:
    """Un acte d'activite, reduit aux champs que l'outil lit."""

    def __init__(self, type, side=None, shares=None, amount=None,
                 token_id="jeton", title="Marche exemple"):
        self.type, self.side = type, side
        self.shares = Decimal(str(shares)) if shares is not None else None
        self.amount = Decimal(str(amount)) if amount is not None else None
        self.token_id, self.title = token_id, title


def test_les_depots_ne_sont_pas_le_capital_deploye(outil):
    """8 $ deposes puis reinvestis trois fois font 24 $ d'achats, pas 24 $
    d'apport. Confondre les deux transforme un gain en perte."""
    actes = [
        Acte("DEPOSIT", amount=8),
        Acte("TRADE", "BUY", shares=10, amount=8, token_id="a"),
        Acte("TRADE", "SELL", shares=10, amount=8, token_id="a"),
        Acte("TRADE", "BUY", shares=10, amount=8, token_id="b"),
        Acte("TRADE", "SELL", shares=10, amount=8, token_id="b"),
    ]

    compta = outil.comptabiliser(actes)

    assert compta["depots"] == Decimal(8), "le depot doit rester 8, pas 16"


def test_un_redeem_sans_parts_solde_quand_meme_la_position(outil):
    """Mesure du 2026-08-29 : le remboursement Solana rend 4,99 $ et un champ
    `shares` vide. La position est fermee ; l'outil doit le voir."""
    actes = [
        Acte("TRADE", "BUY", shares=5, amount=2.15, token_id="sol"),
        Acte("REDEEM", amount=4.99, token_id="sol"),
    ]

    soldes, ouverts = outil.trier(outil.comptabiliser(actes)["jetons"])

    assert len(soldes) == 1, "une position remboursee est fermee, pas ouverte"
    assert not ouverts
    assert soldes[0]["gain"] == Decimal("2.84")


def test_une_perdante_jamais_vendue_ne_disparait_pas_du_resultat(outil):
    """Le biais central. Une gagnante soldee et une perdante invendable : le
    gain realise ne voit que la gagnante, le PLANCHER voit les deux."""
    actes = [
        Acte("DEPOSIT", amount=10),
        Acte("TRADE", "BUY", shares=10, amount=2, token_id="gagnante"),
        Acte("TRADE", "SELL", shares=10, amount=5, token_id="gagnante"),
        Acte("TRADE", "BUY", shares=10, amount=4, token_id="morte"),
    ]

    compta = outil.comptabiliser(actes)
    soldes, ouverts = outil.trier(compta["jetons"])
    realise = sum(s["gain"] for s in soldes)
    engage = sum(o["achat_usdc"] - o["vente_usdc"] - o["redeem_usdc"]
                 for o in ouverts)

    assert realise == Decimal(3), "la gagnante seule rend +3"
    assert len(ouverts) == 1 and engage == Decimal(4), "la morte pese 4"
    assert realise - engage == Decimal(-1), (
        "plancher negatif : le compte perd, alors que le gain realise "
        "affiche +3. C'est exactement le mensonge a empecher.")


def test_le_taux_de_reussite_ne_compte_que_les_soldees(outil):
    """Consequence du meme biais : 100 % de reussite avec une position morte
    au bilan. L'outil doit pouvoir le dire, donc le calcul doit rester lisible."""
    actes = [
        Acte("TRADE", "BUY", shares=10, amount=2, token_id="gagnante"),
        Acte("TRADE", "SELL", shares=10, amount=5, token_id="gagnante"),
        Acte("TRADE", "BUY", shares=10, amount=4, token_id="morte"),
    ]

    soldes, ouverts = outil.trier(outil.comptabiliser(actes)["jetons"])
    gagnants = [s for s in soldes if s["gain"] > 0]

    assert len(gagnants) == len(soldes) == 1
    assert len(ouverts) == 1, (
        "100 % de reussite affichable avec une perdante au bilan : "
        "le taux sur soldees seules est structurellement surestime")


# --------------------------------------------------------------------------
# 4. LE PLAFOND LOCAL — l'erreur du 2026-09-03, evitee de justesse.
#
# Lance sans `--max`, l'outil s'arrete a 5000 actes, imprime « ATTENTION :
# plafond local atteint » au milieu du rapport, puis annonce quand meme
# « PLANCHER GARANTI : -16,3 % ». Relance a 60000 actes sur le MEME
# portefeuille : 32 346 actes, 74 jours au lieu de 11, et un plancher de
# -4,2 %. Le premier chiffre etait faux d'un facteur 4 et se presentait comme
# une garantie.
#
# Le plafond de l'API, lui, etait deja traite avec rigueur (« NON VERIFIABLE »,
# titre du plancher change). Deux fois la meme ignorance, deux traitements
# opposes : c'est cette asymetrie qui a produit le chiffre faux.
# --------------------------------------------------------------------------


def _rapport_texte(outil, capsys, actes, limite, plafond=False):
    # Les actes DOIVENT porter un horodatage : le bloc qui annonce une
    # troncature vit sous `if dates:`. Une doublure sans timestamp ne
    # l'atteint jamais et le test passerait au vert sans rien verifier.
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for n, acte in enumerate(actes):
        acte.timestamp = base + timedelta(days=n)
    outil.rapport("0xtest", actes, outil.comptabiliser(actes), None,
                  limite, plafond)
    return capsys.readouterr().out


def test_un_plancher_atteint_localement_n_est_JAMAIS_dit_garanti(outil, capsys):
    """Le mot « garanti » sur une fenetre partielle est l'abus qu'on denonce."""
    actes = [Acte("DEPOSIT", amount=100) for _ in range(3)]
    texte = _rapport_texte(outil, capsys, actes, limite=3)
    assert "PLANCHER GARANTI" not in texte


def test_le_plafond_local_est_annonce_comme_un_refus_pas_un_conseil(outil, capsys):
    actes = [Acte("DEPOSIT", amount=100) for _ in range(3)]
    texte = _rapport_texte(outil, capsys, actes, limite=3)
    assert "NON VERIFIABLE" in texte


def test_le_plafond_local_dit_qu_il_est_RATTRAPABLE(outil, capsys):
    """Difference reelle avec le plafond de l'API : celui-la se releve.

    Ne pas les confondre. Sur le plafond de l'API, conseiller `--max` serait
    conseiller une action impossible.
    """
    actes = [Acte("DEPOSIT", amount=100) for _ in range(3)]
    texte = _rapport_texte(outil, capsys, actes, limite=3)
    assert "--max" in texte


def test_un_historique_complet_garde_son_plancher_garanti(outil, capsys):
    """Le garde-fou ne doit pas se declencher quand tout a ete lu."""
    actes = [Acte("DEPOSIT", amount=100)]
    texte = _rapport_texte(outil, capsys, actes, limite=5000)
    assert "PLANCHER GARANTI" in texte
    assert "NON VERIFIABLE" not in texte
