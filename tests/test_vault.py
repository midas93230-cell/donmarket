"""Tests du scellement DPAPI.

Un aller-retour RÉEL est exécuté sous Windows — c'est le seul test qui prouve
quelque chose. Les autres vérifient les décisions prises autour, celles qui
tiennent sur toutes les plateformes.
"""

from __future__ import annotations

import platform

import pytest

from donmarket.store import vault

WINDOWS = platform.system() == "Windows"
SECRET = "0x" + "ab" * 32


@pytest.fixture(autouse=True)
def cache_vierge():
    vault.clear_cache()
    yield
    vault.clear_cache()


def test_une_valeur_en_clair_traverse_sans_etre_touchee():
    """Le scellement est une option, pas une migration forcée.

    Un utilisateur bloqué par sa propre sécurité la désactive, et se retrouve
    moins protégé qu'avant. Donc le clair reste valide.
    """
    assert vault.unseal("valeur-en-clair") == "valeur-en-clair"
    assert not vault.is_sealed("valeur-en-clair")


def test_une_valeur_scellee_est_reconnaissable():
    assert vault.is_sealed("dpapi:v1:AQAAAA==")


def test_read_secret_rend_none_sur_variable_absente(monkeypatch):
    monkeypatch.delenv("UNE_VARIABLE_QUI_NEXISTE_PAS", raising=False)
    assert vault.read_secret("UNE_VARIABLE_QUI_NEXISTE_PAS") is None


def test_read_secret_rend_none_sur_variable_vide(monkeypatch):
    """Une variable présente mais vide n'est pas un secret configuré."""
    monkeypatch.setenv("VARIABLE_VIDE", "   ")
    assert vault.read_secret("VARIABLE_VIDE") is None


def test_sceller_le_vide_est_refuse():
    with pytest.raises(vault.VaultError):
        vault.seal("")


def test_hors_windows_le_refus_est_explicite(monkeypatch):
    """Un message qui dit quoi faire à la place, pas seulement « indisponible »."""
    monkeypatch.setattr(vault, "is_available", lambda: False)
    with pytest.raises(vault.VaultUnavailable) as exc:
        vault.seal("peu importe")
    assert "Windows" in str(exc.value)
    assert "permissions" in str(exc.value)


def test_le_secret_ne_passe_JAMAIS_par_la_ligne_de_commande(monkeypatch):
    """Le point qui justifie la forme du module.

    Les arguments d'un processus sont lisibles par tout le système : passer la
    charge en argument annulerait l'intérêt de l'opération. Elle doit transiter
    par l'entrée standard.
    """
    vus: dict[str, object] = {}

    class FauxProcessus:
        returncode = 0
        stdout = "AQAAAA=="
        stderr = ""

    def faux_run(cmd, **kwargs):
        vus["cmd"] = cmd
        vus["input"] = kwargs.get("input")
        return FauxProcessus()

    monkeypatch.setattr(vault, "is_available", lambda: True)
    monkeypatch.setattr(vault.subprocess, "run", faux_run)

    vault.seal(SECRET)

    assert SECRET not in " ".join(vus["cmd"])
    assert vus["input"] is not None  # la charge est passée par stdin


def test_l_echec_dpapi_ne_recrache_pas_toute_la_sortie(monkeypatch):
    class FauxProcessus:
        returncode = 1
        stdout = ""
        stderr = "erreur inattendue\nligne suivante qui ne doit pas remonter"

    monkeypatch.setattr(vault, "is_available", lambda: True)
    monkeypatch.setattr(vault.subprocess, "run", lambda cmd, **kw: FauxProcessus())

    with pytest.raises(vault.VaultError) as exc:
        vault.seal("peu importe")
    message = str(exc.value)
    assert "code 1" in message
    assert "ligne suivante" not in message  # seule la 1re ligne est retenue


def test_le_descellement_est_mis_en_cache(monkeypatch):
    """Chaque descellement lance PowerShell : deux fois seraient deux secondes."""
    appels = {"n": 0}

    class FauxProcessus:
        returncode = 0
        stderr = ""
        stdout = "c2VjcmV0"  # base64 de « secret »

    def faux_run(cmd, **kwargs):
        appels["n"] += 1
        return FauxProcessus()

    monkeypatch.setattr(vault, "is_available", lambda: True)
    monkeypatch.setattr(vault.subprocess, "run", faux_run)

    assert vault.unseal("dpapi:v1:XXXX") == "secret"
    assert vault.unseal("dpapi:v1:XXXX") == "secret"
    assert appels["n"] == 1


@pytest.mark.slow
@pytest.mark.skipif(not WINDOWS, reason="DPAPI n'existe que sous Windows")
def test_aller_retour_reel_sous_windows():
    """Le seul test qui prouve que le scellement fonctionne vraiment."""
    scelle = vault.seal(SECRET)
    assert scelle.startswith(vault.SEALED_PREFIX)
    assert SECRET not in scelle  # la valeur n'est plus lisible dans le fichier
    assert vault.unseal(scelle) == SECRET


@pytest.mark.slow
@pytest.mark.skipif(not WINDOWS, reason="DPAPI n'existe que sous Windows")
def test_read_secret_descelle_depuis_l_environnement(monkeypatch):
    scelle = vault.seal(SECRET)
    monkeypatch.setenv("UN_SECRET_SCELLE", scelle)
    assert vault.read_secret("UN_SECRET_SCELLE") == SECRET
