"""Le moteur d'exécution — vérifié sur ce qu'il REFUSE de faire.

Un test qui vérifierait qu'un ordre part demanderait un compte approvisionné et
dépenserait de l'argent à chaque exécution de la suite. Ce qui se teste ici est
l'inverse, et c'est ce qui protège : que rien ne parte tant que les trois
verrous ne sont pas levés, et qu'aucun secret ne ressorte.
"""

from __future__ import annotations

import pytest as _pytest

from donmarket.execute.engine import _redact


@_pytest.fixture
def secrets_configures(monkeypatch):
    """Configure de faux secrets, comme le ferait un `.env` rempli."""
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("POLYMARKET_API_SECRET", "secret-api-tres-long-1234")
    monkeypatch.setenv("POLYMARKET_BUILDER_API_SECRET", "secret-builder-abcdef-99")
    return monkeypatch


def test_le_secret_est_retire_meme_en_tete_du_message(secrets_configures):
    """Le défaut corrigé : tronquer d'abord gardait justement le secret.

    Une exception de requête signée commence par l'URL et les en-têtes, donc
    les 200 premiers caractères conservés étaient les pires.
    """
    nettoye = _redact("POST /order secret-api-tres-long-1234 " + "x" * 400)
    assert "secret-api-tres-long-1234" not in nettoye
    assert "***" in nettoye


def test_la_cle_privee_est_retiree_avec_ou_sans_prefixe(secrets_configures):
    nue = "ab" * 32
    assert nue not in _redact(f"signing failed with key {nue}")
    assert nue not in _redact(f"signing failed with key 0x{nue}")


def test_le_secret_builder_est_retire_lui_aussi(secrets_configures):
    assert "secret-builder-abcdef-99" not in _redact(
        "builder header rejected: secret-builder-abcdef-99"
    )


def test_un_en_tete_reconstruit_est_filtre_meme_sans_variable(monkeypatch):
    """Filet à motifs : la valeur peut fuir sans venir de l'environnement."""
    for name in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    nettoye = _redact("headers={'POLY_API_KEY': 'AbCdEf0123456789xyz'}")
    assert "AbCdEf0123456789xyz" not in nettoye


def test_les_VRAIS_noms_d_en_tetes_builder_sont_filtres(monkeypatch):
    """Relevés en direct le 2026-08-16 sur des en-têtes réellement signés.

    La première version du motif attendait `POLY_API_KEY` et ne couvrait donc
    aucun des en-têtes builder, tous préfixés `POLY_BUILDER_`. Le filet ne
    filtrait rien, et rien ne l'aurait signalé.
    """
    for name in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_SECRET"):
        monkeypatch.delenv(name, raising=False)

    brut = (
        "401 headers={'POLY_BUILDER_API_KEY': 'AbCdEf0123456789xyz', "
        "'POLY_BUILDER_PASSPHRASE': 'Zy9876543210wvuTsR', "
        "'POLY_BUILDER_SIGNATURE': 'c1D2e3F4g5H6i7J8k9L0=', "
        "'POLY_BUILDER_TIMESTAMP': '1786000000000'}"
    )
    nettoye = _redact(brut, limit=500)
    for secret in (
        "AbCdEf0123456789xyz",
        "Zy9876543210wvuTsR",
        "c1D2e3F4g5H6i7J8k9L0=",
    ):
        assert secret not in nettoye


def test_le_client_clob_recoit_bien_la_configuration_builder(monkeypatch):
    """Le défaut qui aurait coûté tout le revenu.

    `build_builder_config()` existait, était exporté et testé — mais personne
    ne l'appelait, et `ClobClient` était construit sans lui. Les ordres
    seraient partis sans attribution, définitivement : l'attribution se joue à
    la signature, jamais après coup. Rien ne l'aurait signalé, puisque l'ordre
    passe normalement — seul le revenu manque.
    """
    import donmarket.execute.engine as engine

    vus: dict[str, object] = {}

    class FauxClient:
        def __init__(self, host, **kwargs):
            vus.update(kwargs)

        def set_api_creds(self, creds):
            pass

        def create_or_derive_api_creds(self):
            return None

    sentinelle = object()
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    monkeypatch.setenv("POLYMARKET_FUNDER", "0x" + "cd" * 20)

    import donmarket.builder.attribution as attribution

    monkeypatch.setattr(attribution, "build_builder_config", lambda: sentinelle)

    import py_clob_client.client as clob_module

    monkeypatch.setattr(clob_module, "ClobClient", FauxClient)

    engine.build_clob_client()

    assert "builder_config" in vus, "ClobClient construit SANS builder_config"
    assert vus["builder_config"] is sentinelle


def test_sceller_un_secret_n_aveugle_PAS_la_redaction(monkeypatch):
    """Le piège inverse : `os.getenv` rend la forme SCELLÉE.

    C'est la valeur DÉSCELLÉE qui voyage dans les en-têtes et apparaît dans une
    erreur. Substituer la forme scellée ne retirerait donc rien — sceller ses
    secrets aurait affaibli la protection au lieu de la renforcer.
    """
    import donmarket.store.vault as vault

    monkeypatch.setenv("POLYMARKET_API_SECRET", "dpapi:v1:AAAAsomethingsealed")
    monkeypatch.setattr(vault, "unseal", lambda v: "le-vrai-secret-en-clair")
    vault.clear_cache()

    nettoye = _redact("rejet: le-vrai-secret-en-clair dans la requete")
    assert "le-vrai-secret-en-clair" not in nettoye


def test_le_message_reste_diagnostiquable(secrets_configures):
    """Un journal illisible pousse à désactiver la protection. Équilibre tenu."""
    nettoye = _redact("400 Bad Request: insufficient balance for market 0xdeadbeef")
    assert "insufficient balance" in nettoye
    assert "0xdeadbeef" in nettoye  # un id de marché n'est pas un secret

from dataclasses import dataclass

import pytest

from donmarket.execute import engine
from donmarket.execute.limits import ExecutionLimits


@dataclass(frozen=True)
class _Order:
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float


def _orders(count: int = 3):
    return [
        _Order(f"0x{i}", token_id=f"t{i}", side="BUY", price=0.50, size=100.0)
        for i in range(count)
    ]


def _limits(total=1000.0, per_market=200.0, orders=10) -> ExecutionLimits:
    return ExecutionLimits(
        max_total_usd=total, max_per_market_usd=per_market, max_orders=orders
    )


class TestUnarmedSendsNothing:
    def test_the_default_is_unarmed(self):
        """`armed` vaut faux par défaut.

        Le défaut d'un moteur d'ordres doit être l'inaction : un appelant qui
        oublie le paramètre ne doit pas découvrir qu'il vient de trader.
        """
        result = engine.execute_plan(_orders(), limits=_limits())

        assert result.armed is False
        assert result.is_dry_run is True

    def test_nothing_is_accepted_when_unarmed(self):
        result = engine.execute_plan(_orders(), limits=_limits(), armed=False)

        assert result.accepted_count == 0
        assert result.engaged_usd == 0.0
        assert all(order.order_id is None for order in result.sent)

    def test_the_gate_runs_even_unarmed(self):
        """Le mode non armé parcourt le MÊME chemin, plafonds compris.

        C'est ce qui lui donne sa valeur : vérifier le réglage des plafonds sans
        qu'un dollar puisse partir. Un mode d'essai qui court-circuiterait le
        portier ne vérifierait rien de ce qui compte.
        """
        result = engine.execute_plan(
            _orders(5), limits=_limits(total=120.0, per_market=120.0), armed=False
        )

        # 50 $ par ordre, plafond 120 $ : deux passent, trois sont refusés.
        assert len(result.sent) == 2
        assert len(result.refused) == 3

    def test_no_client_is_built_when_unarmed(self, monkeypatch):
        """Non armé, aucune clé n'est lue et aucun client n'est construit.

        Le vérifier explicitement : construire le client déclenche la dérivation
        des identifiants API, donc un appel réseau signé, avant même la question
        de savoir si on voulait trader.
        """

        def _explode(*args, **kwargs):
            raise AssertionError("le client ne doit pas être construit hors armement")

        monkeypatch.setattr(engine, "build_clob_client", _explode)

        result = engine.execute_plan(_orders(), limits=_limits(), armed=False)

        assert result.is_dry_run


class TestArmedWithoutCredentials:
    def test_arming_without_a_key_refuses_loudly(self, monkeypatch):
        """Armé mais sans clé : refus explicite, pas un silence.

        Un moteur qui renverrait un résultat vide laisserait croire qu'il n'y
        avait rien à faire, alors que c'est la configuration qui manque.
        """
        for name in (engine.PRIVATE_KEY_VAR, *engine.API_VARS):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(engine.ExecutionRefused) as caught:
            engine.execute_plan(_orders(), limits=_limits(), armed=True)

        assert engine.PRIVATE_KEY_VAR in str(caught.value)

    def test_preflight_names_what_is_missing_without_revealing_anything(
        self, monkeypatch
    ):
        for name in (engine.PRIVATE_KEY_VAR, *engine.API_VARS):
            monkeypatch.delenv(name, raising=False)

        ready, missing = engine.preflight()

        assert ready is False
        assert engine.PRIVATE_KEY_VAR in missing

    def test_preflight_reports_ready_when_everything_is_present(self, monkeypatch):
        monkeypatch.setenv(engine.PRIVATE_KEY_VAR, "0xdeadbeef")
        for name in engine.API_VARS:
            monkeypatch.setenv(name, "valeur")

        ready, missing = engine.preflight()

        assert ready is True
        assert missing == ()


class TestSecretsNeverLeak:
    def test_error_messages_are_truncated(self):
        """Une exception de requête signée peut porter les en-têtes d'auth."""
        long_error = "POLY_API_KEY=secret " * 100

        redacted = engine._redact(long_error)

        assert len(redacted) <= 201
        assert redacted.endswith("…")

    def test_redaction_flattens_newlines(self):
        """Une trace multiligne dans un journal casse la lecture des lignes."""
        assert "\n" not in engine._redact("ligne1\nligne2")

    def test_the_result_carries_no_secret(self, monkeypatch):
        monkeypatch.setenv(engine.PRIVATE_KEY_VAR, "0xtressecret")

        result = engine.execute_plan(_orders(), limits=_limits(), armed=False)

        assert "0xtressecret" not in repr(result)


class TestChainConstants:
    def test_the_chain_is_polygon(self):
        """Signer pour une autre chaîne produit un ordre valide et inutilisable."""
        assert engine.POLYGON_CHAIN_ID == 137

    def test_an_unset_signature_type_refuses_rather_than_guessing(self, monkeypatch):
        """Pas de défaut : recharger par carte ou PayPal donne un PROXY.

        Un défaut à 0 conviendrait à la minorité des cas et échouerait de la
        façon la plus déroutante — ordre accepté, puis rejeté pour solde
        insuffisant sur une adresse vide.
        """
        monkeypatch.delenv(engine.SIGNATURE_TYPE_VAR, raising=False)

        with pytest.raises(engine.ExecutionRefused) as caught:
            engine.configured_signature_type()

        assert engine.SIGNATURE_TYPE_VAR in str(caught.value)

    def test_an_out_of_range_signature_type_is_refused(self, monkeypatch):
        monkeypatch.setenv(engine.SIGNATURE_TYPE_VAR, "7")

        with pytest.raises(engine.ExecutionRefused):
            engine.configured_signature_type()

    def test_a_valid_signature_type_is_read(self, monkeypatch):
        monkeypatch.setenv(engine.SIGNATURE_TYPE_VAR, "1")

        assert engine.configured_signature_type() == engine.SIGNATURE_TYPE_EMAIL_PROXY

    def test_the_funder_wins_over_the_key_address(self, monkeypatch):
        """En type 1 ou 2, l'adresse qui détient les fonds n'est PAS celle de la clé.

        Les confondre est exactement l'erreur que le type de signature cherche
        à éviter : `POLYMARKET_FUNDER` doit donc primer.
        """
        monkeypatch.setenv("POLYMARKET_ADDRESS", "0xcledesignature")
        monkeypatch.setenv(engine.FUNDER_VAR, "0xproxyquidetient")

        assert engine.configured_funder() == "0xproxyquidetient"

    def test_the_key_address_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_ADDRESS", "0xcle")
        monkeypatch.delenv(engine.FUNDER_VAR, raising=False)

        assert engine.configured_funder() == "0xcle"

    def test_the_three_signature_types_are_distinct(self):
        """Le type de signature décide de QUELLE adresse détient les fonds.

        Se tromper fait rejeter l'ordre pour solde insuffisant en pointant une
        adresse où l'argent n'est pas — le piège le plus déroutant de l'API.
        """
        types = {
            engine.SIGNATURE_TYPE_EOA,
            engine.SIGNATURE_TYPE_EMAIL_PROXY,
            engine.SIGNATURE_TYPE_BROWSER_PROXY,
        }

        assert types == {0, 1, 2}
