"""Recouvrer un signataire au lieu de le croire — et le piège qui rend ça nécessaire.

## Pourquoi ce module existe

`misterRegime` publie un flux de signaux signés et propose son propre
`verify_signal.py` pour le contrôler. Un vérificateur écrit par la partie
auditée n'est pas une preuve : c'est une affirmation de plus, présentée dans
un format qui ressemble à un contrôle. Le refuser n'est pas de la méfiance
envers lui, c'est la seule chose qui distingue un audit d'un communiqué.

Ce module recalcule l'adresse du signataire à partir de la signature
elle-même. Si le recouvrement tombe sur l'adresse qu'il revendique, ce n'est
plus sa parole, c'est de l'arithmétique sur une courbe elliptique.

## LE PIÈGE, et c'est le même que celui du code builder

**Le recouvrement ECDSA réussit toujours.** Sur toute signature bien formée il
rend une adresse — simplement une autre adresse si la signature ne correspond
pas au contenu. Aucune erreur, aucune exception, aucun indice.

C'est mot pour mot la famille de défaut qui a coûté deux semaines de volume non
attribué : le CLOB acceptait un code builder malformé, `/builder/trades`
rendait `count: 0`, et on lisait « pas de volume » au lieu de « pas
d'attribution ». Un code naïf ferait ici la même faute en lisant
« recouvrement réussi » au lieu de « signature invalide ».

**« Le recouvrement a fonctionné » ne veut rien dire. Seul
« recouvré == revendiqué » veut dire quelque chose.** Toute l'API de ce module
est construite pour rendre la première lecture impossible : `verify_signed_batch`
ne rend jamais une adresse sans le verdict qui va avec.

## Ce qu'une signature vérifiée prouve, et surtout ce qu'elle ne prouve pas

Elle prouve que le détenteur de la clé a signé CE contenu exact, et que le
contenu n'a pas bougé depuis. C'est de la provenance et de l'immuabilité.

Elle ne prouve **rien** sur la véracité de ce contenu. Un horodatage à
l'intérieur d'un lot signé reste une affirmation de celui qui signe : il peut
signer aujourd'hui un lot daté d'hier. La signature scelle le texte, pas la
réalité qu'il décrit. Le rapport doit le dire aussi fort que le reste — c'est
le périmètre que misterRegime a lui-même fixé, et il a raison de l'avoir fixé.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# `personal_sign` (EIP-191) préfixe le message avant de le hacher ; `raw_hash`
# signe l'empreinte du contenu. Les deux existent dans la nature et rien dans
# une signature ne dit laquelle a servi — d'où l'essai des deux.
SCHEMES = ("personal_sign", "raw_hash")


class SignatureMismatch(RuntimeError):
    """Le signataire recouvré n'est pas celui qui était revendiqué."""


@dataclass(frozen=True)
class SignatureVerdict:
    """Un verdict, jamais une adresse seule.

    `recovered` n'est volontairement pas exposé sans `verified` à côté : lue
    isolément, une adresse recouvrée ressemble à un succès alors qu'elle est
    rendue même pour une signature qui ne correspond à rien.
    """

    verified: bool
    recovered: str | None
    claimed: str
    scheme: str | None
    error: str | None = None

    @property
    def summary(self) -> str:
        """La phrase à mettre dans un rapport. Jamais ambiguë."""
        if self.error:
            return f"SIGNATURE ILLISIBLE : {self.error}"
        if self.verified:
            return (
                f"VERIFIE — la signature recouvre {self.claimed} "
                f"(schema {self.scheme}). Le contenu n'a pas bouge depuis la "
                "signature. Cela ne dit rien de sa veracite."
            )
        return (
            f"NON VERIFIE — lue en {self.scheme}, la signature recouvre "
            f"{self.recovered}, pas {self.claimed}. Le recouvrement a REUSSI : "
            "c'est justement ce qui rend ce cas dangereux a lire trop vite."
        )


@dataclass(frozen=True)
class Mutation:
    """Une différence entre deux lectures du même document."""

    before: str
    after: str


def _recover_one(payload: str, signature: str, scheme: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_defunct

    if scheme == "personal_sign":
        message = encode_defunct(text=payload)
    else:
        message = encode_defunct(hexstr=hashlib.sha256(payload.encode()).hexdigest())
    return Account.recover_message(message, signature=signature)


def recover_signer(payload: str, signature: str) -> tuple[str | None, str | None]:
    """Rend (adresse, schéma) pour le premier schéma qui décode, sinon (None, None).

    À N'UTILISER QUE via `verify_signed_batch`. Prise seule, l'adresse rendue
    ici est un piège : elle existe pour toute signature bien formée.
    """
    for scheme in SCHEMES:
        try:
            return _recover_one(payload, signature, scheme), scheme
        except Exception:  # noqa: BLE001 — signature illisible pour ce schéma
            continue
    return None, None


def verify_signed_batch(
    payload: str, signature: str, claimed_signer: str, *, strict: bool = False
) -> SignatureVerdict:
    """Le contenu a-t-il été signé par l'adresse revendiquée ?

    Essaie chaque schéma et retient celui qui recouvre l'adresse revendiquée.
    Cette recherche est légitime — on teste une hypothèse contre une CIBLE
    FIXE, on ne cherche pas un résultat qui plaise — mais elle doit être
    déclarée : `scheme` dit lequel a correspondu, sans quoi personne ne peut
    refaire le calcul.

    `strict` lève au lieu de rendre un verdict négatif, pour les appels où
    continuer en silence serait la faute.
    """
    if not signature or not isinstance(signature, str):
        return SignatureVerdict(False, None, claimed_signer, None,
                                "signature absente")

    # LE PREMIER recouvrement, pas le dernier. Bug attrape par les tests le
    # 2026-09-03 : en gardant le dernier schema essaye, un echec renvoyait
    # l'adresse parasite issue de `raw_hash` au lieu de la lecture
    # `personal_sign`, qui est celle que l'auteur a presque surement voulue.
    # Le verdict etait juste et l'adresse affichee trompeuse -- soit exactement
    # le mode de defaillance que ce module existe pour empecher.
    premier: str | None = None
    premier_scheme: str | None = None
    derniere_erreur: str | None = None
    for scheme in SCHEMES:
        try:
            recouvre = _recover_one(payload, signature, scheme)
        except Exception as exc:  # noqa: BLE001
            derniere_erreur = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        if premier is None:
            premier, premier_scheme = recouvre, scheme
        if recouvre.lower() == claimed_signer.lower():
            return SignatureVerdict(True, recouvre, claimed_signer, scheme)

    if premier is None:
        return SignatureVerdict(False, None, claimed_signer, None,
                                derniere_erreur or "signature illisible")

    # `scheme` est renseigne meme en echec : sans lui, personne ne peut savoir
    # de quelle lecture vient l'adresse rapportee, donc personne ne peut la
    # refaire.
    verdict = SignatureVerdict(False, premier, claimed_signer, premier_scheme)
    if strict:
        raise SignatureMismatch(verdict.summary)
    return verdict


def content_digest(payload: str) -> str:
    """SHA-256 des octets EXACTS. Aucune normalisation, et c'est délibéré.

    Rogner les espaces ou réordonner un JSON avant de hacher laisserait passer
    une réécriture invisible — or c'est exactement ce qu'on cherche à
    détecter. Un espace en fin de ligne EST une différence.
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_mutation(earlier: str, later: str) -> Mutation | None:
    """Le même document a-t-il changé entre deux lectures ?

    Le second test que misterRegime a lui-même demandé : l'archive est-elle
    append-only EN PRATIQUE, et pas seulement en promesse. Une signature
    valide ne l'établit pas — le signataire peut resigner un contenu réécrit.
    Seule la comparaison de deux lectures espacées dans le temps le fait.
    """
    avant, apres = content_digest(earlier), content_digest(later)
    return None if avant == apres else Mutation(before=avant, after=apres)
