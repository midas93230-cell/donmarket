"""Connexion du compte Polymarket — présence des identifiants, jamais leur valeur.

Trois règles tenues par ce module, et elles ne sont pas négociables.

**Rien ne vient du code.** Les identifiants sont lus dans l'environnement ou le
`.env` local, qui est ignoré par git. Aucune valeur par défaut, aucun exemple
réaliste : un secret écrit dans un fichier de code y reste pour toujours, y
compris dans l'historique.

**Rien ne ressort.** Ce module ne renvoie jamais une clé. Il dit ce qui est
présent et ce qui manque, et rien d'autre. L'adresse publique est le seul
élément affiché, et tronquée — c'est une adresse publique, pas un secret, mais
la coller entière dans une page n'apporte rien.

**Rien n'est armé ici.** Avoir une clé configurée ne pose aucun ordre. La
signature et l'envoi sont un autre module, qui n'existe pas encore, et la
décision de l'activer appartient au propriétaire du compte.

Variables attendues, à mettre dans `.env` à la racine du projet :

    POLYMARKET_ADDRESS=0x…            adresse publique du portefeuille
    POLYMARKET_PRIVATE_KEY=…          clé privée Polygon (signature des ordres)
    POLYMARKET_API_KEY=…              identifiants CLOB, dérivés de la clé
    POLYMARKET_API_SECRET=…
    POLYMARKET_API_PASSPHRASE=…
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PRIVATE_KEY_VAR = "POLYMARKET_PRIVATE_KEY"
ADDRESS_VAR = "POLYMARKET_ADDRESS"
API_VARS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


def _present(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


def short_address(address: str | None) -> str | None:
    """Adresse tronquée façon terminal : 0x1234…cdef."""
    if not address:
        return None
    cleaned = address.strip()
    if len(cleaned) <= 12:
        return cleaned
    return f"{cleaned[:6]}…{cleaned[-4:]}"


@dataclass(frozen=True)
class Credentials:
    """Ce qu'on sait de la connexion — sans jamais porter de secret.

    Volontairement dépourvue des valeurs elles-mêmes : un objet qui contient
    une clé finit par être journalisé, sérialisé ou affiché un jour.
    """

    address: str | None
    has_private_key: bool
    has_api_credentials: bool

    @property
    def is_connected(self) -> bool:
        """Assez d'éléments pour espérer signer un ordre.

        Les identifiants API se dérivent de la clé privée : c'est elle
        l'élément indispensable, le reste peut être régénéré.
        """
        return self.has_private_key

    @property
    def missing(self) -> tuple[str, ...]:
        absent: list[str] = []
        if not self.has_private_key:
            absent.append(PRIVATE_KEY_VAR)
        if not self.has_api_credentials:
            absent.extend(name for name in API_VARS if not _present(name))
        if not self.address:
            absent.append(ADDRESS_VAR)
        return tuple(absent)


def load_credentials() -> Credentials:
    """Lit l'environnement. `config.py` a déjà chargé le `.env` local."""
    address = os.getenv(ADDRESS_VAR) or None
    return Credentials(
        address=address.strip() if address else None,
        has_private_key=_present(PRIVATE_KEY_VAR),
        has_api_credentials=all(_present(name) for name in API_VARS),
    )


def connection_status() -> dict[str, object]:
    """L'état de connexion tel que la page l'affiche. Aucun secret dedans."""
    credentials = load_credentials()
    return {
        "connected": credentials.is_connected,
        "address": short_address(credentials.address),
        "has_api_credentials": credentials.has_api_credentials,
        "missing": list(credentials.missing),
        # Tant que ceci est faux, aucun ordre ne peut partir, quoi qu'affiche
        # le reste de la page.
        "can_execute": False,
    }
