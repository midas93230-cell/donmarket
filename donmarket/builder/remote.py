"""Attribution à distance — le seul chemin par lequel un TIERS peut nous rapporter.

`attribution.py` couvre le mode LOCAL : les identifiants d'API builder sont dans
l'environnement qui signe. C'est le mode de l'opérateur du programme, et il a une
conséquence qui décide de toute l'économie du dépôt :

**un utilisateur tiers qui clone ce dépôt public n'a pas nos identifiants, donc
son volume n'est attribué à personne.** Vérifié sur le format de fil et pas
seulement sur la documentation : `order_to_json` produit exactement
`{order, owner, orderType, postOnly}` — aucun champ builder. L'attribution passe
à 100 % par quatre en-têtes SIGNÉS avec le secret. Router du volume sans le
secret rapporte zéro, et publier le secret pour y remédier le ferait révoquer.

Le SDK offre la sortie : `BuilderType.REMOTE`. Le client POSTe
`{method, path, body, timestamp}` vers une URL de signature et reçoit les quatre
en-têtes. **Le tiers ne voit jamais notre secret ; nous ne voyons jamais sa clé
privée.** Ce module câble ce mode et corrige les deux pièges qu'il porte.

## Piège 1 — le mode REMOTE est cassé dans le SDK installé

`py_builder_signing_sdk.config.BuilderConfig.generate_builder_headers` renvoie
directement le retour de `http_helpers.post()`, qui est `resp.json()`, donc un
`dict`. Or `py_clob_client._get_builder_headers` appelle `.to_dict()` dessus :

    AttributeError: 'dict' object has no attribute 'to_dict'

Reproduit sur deux URL. Le correctif tient en une conversion, faite ici plutôt
que par un correctif de singe sur le paquet installé : une dépendance corrigée
en place redevient cassée au premier `pip install --upgrade`, et silencieusement.

## Piège 2 — un échec d'attribution N'ARRÊTE PAS l'ordre

`py_clob_client/client.py` : si les en-têtes builder valent `None`, le client
repart sur un `post()` SANS attribution. L'ordre est envoyé, il s'exécute, et les
frais sont perdus définitivement. Le signeur distant du SDK y mène tout droit —
il avale toute exception avec un `print` puis `return None`.

**Ce module ne bloque PAS l'ordre pour autant.** Un ordre non attribué ne coûte
rien à celui qui le passe : il ne paie simplement pas de frais builder. Le
bloquer pour protéger notre revenu retournerait l'outil contre son utilisateur.
On laisse donc passer, mais on rend le raté BRUYANT et COMPTÉ, au lieu du `print`
avalé du SDK.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

REMOTE_URL_VAR = "POLYMARKET_BUILDER_REMOTE_URL"
REMOTE_TOKEN_VAR = "POLYMARKET_BUILDER_REMOTE_TOKEN"

HEADER_FIELDS = (
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_TIMESTAMP",
    "POLY_BUILDER_PASSPHRASE",
    "POLY_BUILDER_SIGNATURE",
)


class RemoteAttributionUnavailable(RuntimeError):
    """Le mode distant a été demandé sans URL exploitable."""


@dataclass(frozen=True)
class RemoteAttribution:
    """Ce qu'on sait du signeur distant. Le jeton n'est jamais porté ici."""

    url: str | None
    has_token: bool

    @property
    def is_configured(self) -> bool:
        return bool(self.url)

    @property
    def is_encrypted(self) -> bool:
        """Faux sur `http://`, et ça compte : le jeton porteur voyage en clair.

        Toléré sur une boucle locale, où rien ne sort de la machine.
        """
        if not self.url:
            return False
        return self.url.startswith("https://")

    @property
    def is_loopback(self) -> bool:
        if not self.url:
            return False
        return self.url.startswith(("http://127.0.0.1", "http://localhost"))


def load_remote_config() -> RemoteAttribution:
    """Lit l'environnement. `config.py` a déjà chargé le `.env` local."""
    raw = os.getenv(REMOTE_URL_VAR)
    url = raw.strip() if raw and raw.strip() else None
    token = os.getenv(REMOTE_TOKEN_VAR)
    return RemoteAttribution(url=url, has_token=bool(token and token.strip()))


def coerce_header_payload(raw: Any) -> Any:
    """Transforme la réponse du signeur en `BuilderHeaderPayload`.

    Rend `None` si la réponse ne porte pas les quatre en-têtes : mieux vaut un
    raté compté qu'un objet à moitié rempli qui produirait une signature fausse
    et un rejet côté CLOB, très loin de sa cause.
    """
    if raw is None:
        return None
    if hasattr(raw, "to_dict"):  # déjà un payload (mode LOCAL, ou SDK corrigé)
        return raw
    if not isinstance(raw, dict):
        return None

    manquants = [name for name in HEADER_FIELDS if not raw.get(name)]
    if manquants:
        logger.warning(
            "Signeur distant : réponse incomplète, en-têtes absents %s — "
            "l'ordre partira SANS attribution et les frais seront perdus",
            ", ".join(manquants),
        )
        return None

    from py_builder_signing_sdk.sdk_types import BuilderHeaderPayload

    return BuilderHeaderPayload(**{name: str(raw[name]) for name in HEADER_FIELDS})


def build_remote_builder_config() -> Any:
    """Construit un `BuilderConfig` distant qui survit aux deux pièges ci-dessus.

    Import PARESSEUX du SDK, comme partout ailleurs dans ce paquet : afficher un
    classement en lecture seule ne doit pas payer le chargement du signataire.
    """
    remote = load_remote_config()
    if not remote.is_configured:
        raise RemoteAttributionUnavailable(
            f"{REMOTE_URL_VAR} absente : aucun signeur distant à interroger. "
            "Sans elle un ordre part SANS attribution et les frais sont perdus "
            "définitivement."
        )
    if not remote.is_encrypted and not remote.is_loopback:
        logger.warning(
            "Signeur distant en clair (%s) : le jeton porteur et le corps de "
            "l'ordre transitent sans chiffrement",
            REMOTE_URL_VAR,
        )

    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import RemoteBuilderConfig

    class _CoercedRemoteConfig(BuilderConfig):
        """`BuilderConfig` distant dont la réponse est reconvertie et comptée.

        `misses` n'existe pas pour faire joli : sans compteur, un signeur en
        panne se traduit par du volume qui part gratuitement pendant des heures
        sans que rien ne le dise.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.misses = 0

        def generate_builder_headers(self, *args: Any, **kwargs: Any) -> Any:
            payload = coerce_header_payload(
                super().generate_builder_headers(*args, **kwargs)
            )
            if payload is None:
                self.misses += 1
                logger.warning(
                    "Attribution MANQUÉE (%d depuis le démarrage) : le signeur "
                    "distant n'a pas répondu d'en-têtes exploitables. L'ordre "
                    "part quand même — il ne sera simplement attribué à "
                    "personne, et ces frais-là ne se rattrapent pas",
                    self.misses,
                )
            return payload

    token = os.getenv(REMOTE_TOKEN_VAR)
    return _CoercedRemoteConfig(
        remote_builder_config=RemoteBuilderConfig(
            url=remote.url,
            token=token.strip() if token and token.strip() else None,
        )
    )


def remote_status() -> dict[str, object]:
    """L'état du mode distant tel qu'un rapport l'affiche. Aucun secret dedans."""
    remote = load_remote_config()
    return {
        "remote_configured": remote.is_configured,
        "remote_url": remote.url,  # une URL publique de signature n'est pas un secret
        "remote_has_token": remote.has_token,
        "remote_is_encrypted": remote.is_encrypted,
    }
