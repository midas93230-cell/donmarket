"""Vérifier ce que quelqu'un publie sur lui-même, sans le croire sur parole.

Ce paquet ne contient que des fonctions qui recalculent. Chaque fois qu'un
audité fournit à la fois une affirmation et l'outil qui la contrôle, c'est
l'outil qu'il faut remplacer, pas l'affirmation qu'il faut accepter.
"""

from .signatures import (
    Mutation,
    SignatureMismatch,
    SignatureVerdict,
    content_digest,
    detect_mutation,
    recover_signer,
    verify_signed_batch,
)

__all__ = [
    "Mutation",
    "SignatureMismatch",
    "SignatureVerdict",
    "content_digest",
    "detect_mutation",
    "recover_signer",
    "verify_signed_batch",
]
