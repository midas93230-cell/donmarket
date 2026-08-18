"""Tests du signeur builder DISTANT — le seul chemin qui monétise un tiers.

Chaque test rejoue un piège MESURÉ sur le SDK installé le 2026-08-17, pas une
mécanique inventée. Les deux premiers valent particulièrement d'être relus : ils
échouent tous les deux SILENCIEUSEMENT en production, l'un par une exception
tardive, l'autre en laissant partir du volume gratuitement.
"""

from __future__ import annotations

import pytest

from donmarket.builder.attribution import (
    API_VARS,
    CODE_VAR,
    AttributionNotConfigured,
    attribution_status,
    build_builder_config,
)
from donmarket.builder.remote import (
    HEADER_FIELDS,
    REMOTE_TOKEN_VAR,
    REMOTE_URL_VAR,
    RemoteAttributionUnavailable,
    build_remote_builder_config,
    coerce_header_payload,
    load_remote_config,
)

URL = "https://signer.example.com/sign"
EN_TETES = {
    "POLY_BUILDER_API_KEY": "cle",
    "POLY_BUILDER_TIMESTAMP": "1755000000",
    "POLY_BUILDER_PASSPHRASE": "phrase",
    "POLY_BUILDER_SIGNATURE": "sig",
}


@pytest.fixture
def env_vierge(monkeypatch):
    """Isole du `.env` de la machine : sinon le test dit la vérité d'ici."""
    for name in (CODE_VAR, *API_VARS, REMOTE_URL_VAR, REMOTE_TOKEN_VAR):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def signeur(monkeypatch):
    """Remplace l'appel réseau du SDK. Rend ce que le vrai serveur renverrait.

    Le vrai `http_helpers.post` fait `resp.json()`, donc un `dict` — c'est
    exactement là que le SDK se casse, et il faut le reproduire fidèlement.
    """
    import py_builder_signing_sdk.config as cfg

    appels: list[dict] = []

    def poser(reponse):
        def faux_post(url, data=None, headers=None):
            appels.append({"url": url, "data": data, "headers": headers})
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        monkeypatch.setattr(cfg, "post", faux_post)
        return appels

    return poser


# --------------------------------------------------------------------------
# Piège 1 — le SDK rend un dict là où py-clob-client attend un payload
# --------------------------------------------------------------------------


def test_le_sdk_nu_rend_un_dict_que_py_clob_client_ne_sait_pas_lire(env_vierge, signeur):
    """Le défaut d'origine, reproduit : sans correctif, l'ordre lève.

    `py_clob_client._get_builder_headers` appelle `.to_dict()` sur ce retour.
    Sur un `dict`, c'est un `AttributeError` — et il survient au moment d'envoyer
    un ordre, pas au démarrage.
    """
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import RemoteBuilderConfig

    signeur(dict(EN_TETES))
    nu = BuilderConfig(remote_builder_config=RemoteBuilderConfig(url=URL, token="t"))

    brut = nu.generate_builder_headers("POST", "/order", "{}")
    assert isinstance(brut, dict)  # et non un BuilderHeaderPayload
    with pytest.raises(AttributeError):
        brut.to_dict()


def test_notre_config_rend_un_payload_utilisable_par_py_clob_client(env_vierge, signeur):
    signeur(dict(EN_TETES))
    env_vierge.setenv(REMOTE_URL_VAR, URL)

    config = build_remote_builder_config()
    payload = config.generate_builder_headers("POST", "/order", "{}")

    assert payload is not None
    assert payload.to_dict() == EN_TETES  # ce que py-clob-client appelle vraiment
    assert config.misses == 0


def test_le_corps_et_le_jeton_partent_bien_au_signeur(env_vierge, signeur):
    appels = signeur(dict(EN_TETES))
    env_vierge.setenv(REMOTE_URL_VAR, URL)
    env_vierge.setenv(REMOTE_TOKEN_VAR, "jeton-porteur")

    build_remote_builder_config().generate_builder_headers("POST", "/order", '{"a":1}')

    assert appels[0]["url"] == URL
    assert appels[0]["headers"] == {"Authorization": "Bearer jeton-porteur"}
    # La signature couvre le corps : un signeur qui ne le reçoit pas ne peut
    # produire qu'une signature fausse, rejetée par le CLOB.
    assert appels[0]["data"]["body"] == '{"a":1}'
    assert appels[0]["data"]["method"] == "POST"
    assert appels[0]["data"]["path"] == "/order"


# --------------------------------------------------------------------------
# Piège 2 — un raté d'attribution ne doit ni bloquer l'ordre, ni passer inaperçu
# --------------------------------------------------------------------------


def test_un_signeur_en_panne_ne_bloque_PAS_l_ordre(env_vierge, signeur):
    """Un ordre non attribué ne coûte rien à celui qui le passe.

    Le bloquer pour protéger notre revenu retournerait l'outil contre son
    utilisateur. On rend `None` — py-clob-client enverra l'ordre sans en-têtes.
    """
    signeur(RuntimeError("signeur injoignable"))
    env_vierge.setenv(REMOTE_URL_VAR, URL)

    config = build_remote_builder_config()
    assert config.generate_builder_headers("POST", "/order", "{}") is None


def test_un_signeur_en_panne_est_COMPTE_et_journalise(env_vierge, signeur, caplog):
    """Sans compteur, le volume part gratuitement pendant des heures en silence."""
    signeur(RuntimeError("signeur injoignable"))
    env_vierge.setenv(REMOTE_URL_VAR, URL)
    config = build_remote_builder_config()

    with caplog.at_level("WARNING"):
        config.generate_builder_headers("POST", "/order", "{}")
        config.generate_builder_headers("POST", "/order", "{}")

    assert config.misses == 2
    assert "Attribution MANQUÉE" in caplog.text


def test_une_reponse_incomplete_est_refusee_plutot_que_signee_a_moitie(
    env_vierge, signeur, caplog
):
    """Un payload à moitié rempli produirait un rejet CLOB loin de sa cause."""
    tronquee = {k: v for k, v in EN_TETES.items() if k != "POLY_BUILDER_SIGNATURE"}
    signeur(tronquee)
    env_vierge.setenv(REMOTE_URL_VAR, URL)

    with caplog.at_level("WARNING"):
        payload = build_remote_builder_config().generate_builder_headers(
            "POST", "/order", "{}"
        )

    assert payload is None
    assert "POLY_BUILDER_SIGNATURE" in caplog.text


@pytest.mark.parametrize("valeur", [None, "pas un dict", 42, {}])
def test_toute_reponse_inexploitable_rend_None_sans_lever(valeur):
    assert coerce_header_payload(valeur) is None


def test_un_payload_deja_correct_traverse_sans_etre_reconstruit():
    """Si le SDK est corrigé en amont, on ne doit pas casser le mode LOCAL."""
    from py_builder_signing_sdk.sdk_types import BuilderHeaderPayload

    deja = BuilderHeaderPayload(**EN_TETES)
    assert coerce_header_payload(deja) is deja


# --------------------------------------------------------------------------
# Le choix du mode : local d'abord (opérateur), distant ensuite (tiers)
# --------------------------------------------------------------------------


def test_sans_rien_configure_le_refus_reste_celui_des_identifiants(env_vierge):
    """Le message d'origine ne doit pas être noyé par le mode distant."""
    env_vierge.setenv(CODE_VAR, "0x" + "0" * 63 + "1")
    with pytest.raises(AttributionNotConfigured) as exc:
        build_builder_config()
    assert "POLYMARKET_BUILDER_API_KEY" in str(exc.value)
    assert "perdus définitivement" in str(exc.value)


def test_un_tiers_sans_identifiants_attribue_par_le_signeur_distant(env_vierge):
    """Le cas de TOUT utilisateur du dépôt public : pas de secret, mais une URL."""
    env_vierge.setenv(REMOTE_URL_VAR, URL)

    config = build_builder_config()
    assert config.get_builder_type().value == "REMOTE"
    assert config.is_valid()


def test_les_identifiants_locaux_priment_sur_le_signeur_distant(env_vierge):
    """L'opérateur signe chez lui : un aller-retour réseau par ordre serait absurde."""
    for name in API_VARS:
        env_vierge.setenv(name, "AAAA")  # base64 url-safe décodable
    env_vierge.setenv(REMOTE_URL_VAR, URL)

    assert build_builder_config().get_builder_type().value == "LOCAL"


def test_le_mode_distant_refuse_de_se_construire_sans_URL(env_vierge):
    with pytest.raises(RemoteAttributionUnavailable) as exc:
        build_remote_builder_config()
    assert REMOTE_URL_VAR in str(exc.value)
    assert "perdus définitivement" in str(exc.value)


# --------------------------------------------------------------------------
# Le rapport : dire vrai au tiers, sans laisser fuir le jeton
# --------------------------------------------------------------------------


def test_le_rapport_annonce_can_attribute_a_un_tiers_en_mode_distant(env_vierge):
    """Dire `false` ici ferait croire à un tiers que son volume est perdu."""
    env_vierge.setenv(REMOTE_URL_VAR, URL)
    rapport = attribution_status()

    assert rapport["can_attribute"] is True
    assert rapport["attribution_mode"] == "remote"
    assert rapport["remote_is_encrypted"] is True


def test_le_rapport_ne_laisse_fuir_aucun_jeton(env_vierge):
    env_vierge.setenv(REMOTE_URL_VAR, URL)
    env_vierge.setenv(REMOTE_TOKEN_VAR, "jeton-tres-confidentiel")
    rapport = attribution_status()

    assert "jeton-tres-confidentiel" not in repr(rapport)
    assert rapport["remote_has_token"] is True


def test_une_URL_en_clair_hors_boucle_locale_est_signalee(env_vierge):
    env_vierge.setenv(REMOTE_URL_VAR, "http://signer.example.com/sign")
    remote = load_remote_config()

    assert remote.is_configured
    assert not remote.is_encrypted  # le jeton porteur voyagerait en clair
    assert not remote.is_loopback


def test_la_boucle_locale_en_clair_est_toleree(env_vierge):
    env_vierge.setenv(REMOTE_URL_VAR, "http://127.0.0.1:8080/sign")
    assert load_remote_config().is_loopback


def test_les_quatre_en_tetes_sont_ceux_releves_en_direct():
    """Relevés sur le signataire réel : préfixe `POLY_BUILDER_`, pas `POLY_`."""
    assert HEADER_FIELDS == (
        "POLY_BUILDER_API_KEY",
        "POLY_BUILDER_TIMESTAMP",
        "POLY_BUILDER_PASSPHRASE",
        "POLY_BUILDER_SIGNATURE",
    )
