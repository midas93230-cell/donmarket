"""Tests du vérificateur de signatures — vérifier l'audité, pas le croire.

`misterRegime` publie un flux de signaux signés et propose son propre
`verify_signal.py` pour le contrôler. **Un vérificateur écrit par la partie
auditée n'est pas une preuve, c'est une affirmation de plus.** D'où ce module :
il recalcule l'adresse du signataire depuis la signature elle-même, ce qui
transforme sa parole en calcul.

## Le piège central, et c'est le même que celui du code builder

Le recouvrement ECDSA **réussit toujours**. Sur une signature bien formée, il
rend une adresse quoi qu'il arrive — simplement une autre adresse si la
signature ne correspond pas au contenu. Il n'y a ni erreur, ni exception, ni
indice.

C'est exactement la famille de défaut du code builder malformé : le CLOB
acceptait, `/builder/trades` rendait `count: 0`, et on lisait « pas de volume »
au lieu de « pas d'attribution ». Ici, un code naïf lirait « recouvrement
réussi » au lieu de « signature invalide ».

**Donc « le recouvrement a fonctionné » ne veut rien dire. Seul
« recouvré == revendiqué » veut dire quelque chose.**
"""

from __future__ import annotations

import pytest
from eth_account import Account

from donmarket.audit.signatures import (
    SignatureMismatch,
    content_digest,
    detect_mutation,
    verify_signed_batch,
)

# Clé de test, jamais utilisée ailleurs, générée pour ces tests uniquement.
CLE = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
SIGNATAIRE = Account.from_key(CLE).address
AUTRE = Account.from_key(
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"
).address

LOT = '{"date":"2026-09-07","signals":[{"id":1,"side":"long","skipped":false}]}'


def _signer(texte: str, cle: str = CLE) -> str:
    from eth_account.messages import encode_defunct

    return Account.sign_message(encode_defunct(text=texte), cle).signature.hex()


# ------------------------------------------------------------- le cas nominal


def test_un_lot_signe_par_l_adresse_revendiquee_est_verifie():
    v = verify_signed_batch(LOT, _signer(LOT), SIGNATAIRE)
    assert v.verified is True
    assert v.recovered.lower() == SIGNATAIRE.lower()


def test_la_verification_dit_QUEL_schema_a_correspondu():
    """Essayer plusieurs schémas jusqu'à ce qu'un marche est légitime — on teste
    une hypothèse contre une cible fixe. Le taire ne l'est pas : le rapport
    doit dire lequel, sinon personne ne peut refaire le calcul."""
    v = verify_signed_batch(LOT, _signer(LOT), SIGNATAIRE)
    assert v.scheme in ("personal_sign", "raw_hash")


# --------------------------------------------------- le piège qui compte


def test_UNE_SIGNATURE_D_UN_AUTRE_NE_LEVE_RIEN_ELLE_RECOUVRE_AUTRE_CHOSE():
    """Le cœur du module. Le recouvrement RÉUSSIT et rend la mauvaise adresse.

    Un code qui traiterait « recouvrement sans exception » comme un succès
    validerait n'importe quelle signature bien formée.
    """
    autre_signature = _signer(
        LOT, "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba"
    )
    v = verify_signed_batch(LOT, autre_signature, SIGNATAIRE)
    assert v.verified is False
    assert v.recovered.lower() == AUTRE.lower()  # une adresse a bien ete rendue


def test_un_contenu_modifie_d_un_seul_caractere_casse_la_verification():
    signature = _signer(LOT)
    altere = LOT.replace('"skipped":false', '"skipped":true')
    v = verify_signed_batch(altere, signature, SIGNATAIRE)
    assert v.verified is False


def test_une_verification_ratee_peut_etre_exigee_bruyamment():
    """Pour les appels où continuer en silence serait la faute."""
    with pytest.raises(SignatureMismatch):
        verify_signed_batch(LOT, _signer(LOT), AUTRE, strict=True)


def test_une_signature_malformee_est_REFUSEE_et_non_recouvree():
    for muet in ("", "0x", "pas une signature", "0x1234"):
        v = verify_signed_batch(LOT, muet, SIGNATAIRE)
        assert v.verified is False
        assert v.recovered is None
        assert v.error


# ------------------------------------------------------------ immuabilité


def test_deux_lectures_identiques_ne_signalent_aucune_mutation():
    assert detect_mutation(LOT, LOT) is None


def test_UNE_REECRITURE_SILENCIEUSE_EST_DETECTEE():
    """Le second test que misterRegime a lui-même demandé : l'archive est-elle
    append-only EN PRATIQUE, pas seulement en promesse."""
    plus_tard = LOT.replace('"side":"long"', '"side":"short"')
    mutation = detect_mutation(LOT, plus_tard)
    assert mutation is not None
    assert mutation.before != mutation.after


def test_l_empreinte_est_stable_et_sensible():
    assert content_digest(LOT) == content_digest(LOT)
    assert content_digest(LOT) != content_digest(LOT + " ")


def test_l_empreinte_ne_normalise_RIEN():
    """Un espace en fin de ligne est une différence. Normaliser avant de hacher
    laisserait passer une réécriture invisible — et c'est précisément ce qu'on
    cherche à détecter."""
    assert content_digest(' {"a":1}') != content_digest('{"a":1}')
