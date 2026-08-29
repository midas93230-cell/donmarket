"""Tests de l'archive quotidienne des carnets — le seul actif non rattrapable.

`docs/history/` est ce que ce dépôt a de plus difficile à reconstituer. Un
concurrent réécrit le code en un après-midi ; il ne peut pas mesurer hier.
Un membre du groupe Builders l'a dit lui-même le 2026-08-27 : la valeur est
dans l'HISTORIQUE des verdicts, pas dans le code.

Deux dégâts possibles, un test chacun :

1. L'archive n'a longtemps gardé QUE le verdict, en jetant le soir même les
   prix qui le justifient. Sans prix, on ne peut pas mesurer à quelle
   fréquence le montage d'entrée apparaît — la question qui décide s'il faut
   continuer d'en chercher un. Quatre jours ont été écrits comme ça et sont
   définitivement amputés ; le test existe pour qu'il n'y en ait pas un
   cinquième.

2. Le jour où le format a changé, les relevés du 26 au 29 août 2026 sont
   devenus des fichiers d'un AUTRE format. Un lecteur qui ne comprendrait que
   le nouveau les jetterait EN SILENCE : la page continuerait de s'afficher,
   avec un historique amputé et des persistances fausses. C'est la perte
   qu'on ne remarque que des mois plus tard, quand plus rien ne la répare.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent


def _module():
    """Charge `tools/sante_carnets.py`, qui n'est pas dans un paquet."""
    chemin = RACINE / "tools" / "sante_carnets.py"
    spec = importlib.util.spec_from_file_location("sante_carnets", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sante():
    return _module()


def test_l_archive_garde_les_prix_pas_seulement_le_verdict(sante):
    """Le verdict dit qu'un carnet était mort ; les prix disent à quoi
    ressemblait un carnet VIVANT ce jour-là. Seul le second permet de compter
    les occasions d'entrée passées, et aucun des deux ne se rattrape."""
    releve = {
        "slug": "marche-exemple-2026",
        "question": "Texte volumineux qu'on ne garde pas",
        "verdict": "tradable",
        "bid": 0.07,
        "ask": 0.08,
        "tick": 0.01,
        "prof_bid": 120.0,
        "prof_ask": 45.0,
        "ticket_min": 2.0,
        "volume24h": 310.0,
        "ecart_pct": 14.3,
        "persistance": 3,
    }

    garde = sante.ligne_archivee(releve)

    assert garde["verdict"] == "tradable"
    for colonne in ("bid", "ask", "tick", "prof_bid", "prof_ask",
                    "ticket_min", "volume24h"):
        assert colonne in garde, f"{colonne} perdu : l'archive redevient muette"
    # Recalculables ou volumineux : les garder gonflerait le fichier sans
    # ajouter d'information.
    assert "question" not in garde
    assert "ecart_pct" not in garde
    assert "persistance" not in garde


def test_un_carnet_sans_bid_s_archive_quand_meme(sante):
    """Mesuré le 2026-08-29 : un carnet mort rend `bid` à None. Si l'absence
    de prix faisait sauter la ligne, l'archive perdrait précisément les
    marchés morts — la catégorie que cette page existe pour signaler."""
    garde = sante.ligne_archivee(
        {"slug": "carnet-mort-2026", "verdict": "mort", "bid": None,
         "ask": 0.001, "tick": 0.001, "prof_bid": 0.0, "prof_ask": 1411717.26,
         "ticket_min": 0.0, "volume24h": 2345314.92}
    )

    assert garde["bid"] is None
    assert garde["verdict"] == "mort"


def test_les_deux_formats_d_archive_se_lisent(sante, tmp_path, monkeypatch):
    """Ancien format (verdict seul) et nouveau (objet avec prix) cohabitent
    dans `docs/history/`. Les lire tous les deux n'est pas du confort : les
    quatre premiers jours n'existent qu'à l'ancien format."""
    monkeypatch.setattr(sante, "HISTORIQUE", str(tmp_path))
    (tmp_path / "2026-08-26.json").write_text(
        json.dumps({"marche-exemple-2026": "tradable"}), encoding="utf-8")
    (tmp_path / "2026-08-27.json").write_text(
        json.dumps({"marche-exemple-2026": "tradable"}), encoding="utf-8")
    (tmp_path / "2026-08-28.json").write_text(
        json.dumps({"marche-exemple-2026": {
            "verdict": "tradable", "bid": 0.07, "ask": 0.08, "tick": 0.01,
            "prof_bid": 120.0, "prof_ask": 45.0, "ticket_min": 2.0,
            "volume24h": 310.0}}), encoding="utf-8")

    series = sante.charger_historique()

    assert series["marche-exemple-2026"] == [
        ("2026-08-26", "tradable"),
        ("2026-08-27", "tradable"),
        ("2026-08-28", "tradable"),
    ], "un jour manque : un des deux formats est lu de travers"


def test_la_persistance_traverse_le_changement_de_format(sante, tmp_path,
                                                         monkeypatch):
    """Le chiffre que la page publie. S'il retombait à 1 le jour du changement
    de format, « mort depuis six jours » redeviendrait « mort depuis hier » —
    et c'est cette phrase-là qui a de la valeur."""
    monkeypatch.setattr(sante, "HISTORIQUE", str(tmp_path))
    (tmp_path / "2026-08-26.json").write_text(
        json.dumps({"carnet-mort-2026": "mort"}), encoding="utf-8")
    (tmp_path / "2026-08-27.json").write_text(
        json.dumps({"carnet-mort-2026": {"verdict": "mort", "bid": None}}),
        encoding="utf-8")

    series = sante.charger_historique()

    assert sante.persistance(series["carnet-mort-2026"], "mort") == 3
