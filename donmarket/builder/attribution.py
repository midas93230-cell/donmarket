"""Brancher l'attribution sur les ordres — deux choses distinctes, souvent confondues.

Pour encaisser des frais builder il faut DEUX éléments qui n'ont ni la même
forme, ni le même rôle, et les confondre coûte des mois de volume non attribué :

**Le code builder** (`0x` + 64 hexadécimaux) DÉSIGNE le bénéficiaire. Il sert à
interroger `/builder/trades`, et il se joint à chaque ordre : le SDK l'expose
en `builder_code=` sur `place_limit_order` et `place_market_order`. Un ordre
posé sans lui n'est rattaché à personne.

**CORRECTION DU 2026-09-02, à lire avant de refaire l'erreur.** Ce paragraphe
affirmait jusqu'ici que le code était « un IDENTIFIANT DE LECTURE » et que
« le poser dans une requête n'attribue rien du tout ». C'était FAUX, et cette
seule phrase — écrite une fois, jamais revérifiée — a coûté deux semaines de
volume non attribué : elle a fait lire « builder 0 $ » comme « pas de volume »
au lieu de « pas d'attribution ». Elle n'a été démasquée que parce
qu'Edoardo (Polymarket) a demandé le 2026-09-01 si l'attribution était branchée.
La leçon n'est pas « vérifier le SDK » : c'est qu'une doc affirmative sur un
sujet non mesuré se propage plus vite qu'un bug, et sans trace.

**Les identifiants d'API builder** (`key` / `secret` / `passphrase`) sont
l'autre moitié : `py_builder_signing_sdk` les utilise pour signer des en-têtes
builder, et c'est cette signature que le CLOB reconnaît au moment où l'ordre
est apparié. Les deux moitiés sont nécessaires, et aucune des deux ne se
rattrape après coup — l'attribution se joue à la signature, jamais après.

Un poseur d'ordre n'a pas à connaître ce partage : il appelle
`order_attribution()`, plus bas, et rien d'autre.

Les deux se récupèrent sur `polymarket.com → Settings → Builders`, sur un compte
connecté.

## Ce qui ne suffit PAS, même avec les deux

Le palier par défaut est **Unverified** : 100 transactions de relayer par jour
et **aucune monétisation**. Facturer le moindre point de base exige le palier
**Verified**, obtenu par approbation manuelle (courriel à builder@polymarket.com
avec la clé d'API, le cas d'usage et le volume attendu). Ce module dit donc
« prêt à attribuer », jamais « prêt à encaisser » : la seconde affirmation
dépend d'une décision humaine chez Polymarket, que le code ne peut pas lire.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .codes import BuilderCode, InvalidBuilderCode

CODE_VAR = "POLYMARKET_BUILDER_CODE"
API_VARS = (
    "POLYMARKET_BUILDER_API_KEY",
    "POLYMARKET_BUILDER_API_SECRET",
    "POLYMARKET_BUILDER_API_PASSPHRASE",
)


class AttributionNotConfigured(RuntimeError):
    """L'attribution a été demandée sans les éléments pour l'assurer."""


def _present(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


@dataclass(frozen=True)
class BuilderAttribution:
    """Ce qu'on sait de l'attribution — jamais un secret, seulement sa présence.

    Le code est porté en clair parce qu'il n'en est pas un : il figure dans
    chaque ligne publique de `/builder/trades`. Les identifiants d'API, eux,
    ne sortent jamais d'ici.
    """

    code: BuilderCode | None
    has_api_credentials: bool
    code_error: str | None = None

    @property
    def can_attribute(self) -> bool:
        """Vrai seulement si un ordre signé maintenant serait attribué.

        Le code seul ne suffit pas : c'est la signature des en-têtes qui
        attribue. Un « can_attribute » qui se contenterait du code laisserait
        croire que le volume rentre alors qu'il ne va chez personne.
        """
        return self.has_api_credentials

    @property
    def can_read_attribution(self) -> bool:
        """Vrai si on peut au moins LIRE ce qui a été attribué."""
        return self.code is not None

    @property
    def missing(self) -> tuple[str, ...]:
        absent: list[str] = []
        if self.code is None:
            absent.append(CODE_VAR)
        absent.extend(name for name in API_VARS if not _present(name))
        return tuple(absent)


def load_attribution() -> BuilderAttribution:
    """Lit l'environnement. `config.py` a déjà chargé le `.env` local.

    Un code MALFORMÉ n'est pas traité comme un code absent : il est retenu dans
    `code_error`. La distinction compte — un code absent est un réglage à
    faire, un code malformé est une faute de frappe qui produirait des pages
    vides silencieuses et un compte à zéro qu'on croirait vrai.
    """
    raw = os.getenv(CODE_VAR)
    code: BuilderCode | None = None
    error: str | None = None
    if raw and raw.strip():
        try:
            code = BuilderCode(raw)
        except InvalidBuilderCode as exc:
            error = str(exc)

    return BuilderAttribution(
        code=code,
        has_api_credentials=all(_present(name) for name in API_VARS),
        code_error=error,
    )


@dataclass(frozen=True)
class OrderAttribution:
    """Ce qu'un poseur d'ordre doit savoir, et RIEN de plus.

    Trois champs parce que les sept appelants avaient tous besoin des trois et
    les recalculaient chacun : la valeur à passer au SDK, un verdict booléen,
    et une phrase imprimable.
    """

    code: str | None
    """À passer tel quel à `builder_code=`. `None` = ne rien joindre.

    JAMAIS la valeur brute de l'environnement : soit un code validé et
    normalisé, soit rien. Un code malformé vaut `None` — voir ci-dessous.
    """

    is_attributed: bool
    """Vrai si l'ordre signé maintenant porterait une attribution lisible."""

    phrase: str
    """Le verdict en clair, à imprimer ou journaliser. Aucun secret dedans."""


def order_attribution() -> OrderAttribution:
    """LE SEUL endroit d'où un poseur d'ordre tire son code builder.

    ## Pourquoi une fonction, et pourquoi une seule

    Le code était lu dans sept fichiers, chacun par son propre
    `os.getenv("POLYMARKET_BUILDER_CODE")`. Cette dispersion n'a pas seulement
    dupliqué trois lignes : elle a fait qu'on a pu corriger DEUX endroits le
    2026-09-01 en croyant avoir corrigé le défaut, alors que cinq appels
    continuaient de partir sans attribution. Un défaut réparti sur sept sites
    n'a pas d'état « réparé » observable ; sur un seul, si.

    ## Pourquoi elle VALIDE au lieu de simplement lire

    Parce que `os.getenv(...) or None` — la forme employée par les deux
    premiers correctifs — rejouait exactement le bug qu'elle prétendait
    corriger. Mesuré le 2026-08-13 (détail en tête de `codes.py`) : un code
    malformé n'est refusé par personne. Le CLOB signe, l'ordre passe, et
    `/builder/trades` rend `{"data":[],"count":0}` — c'est-à-dire la réponse
    EXACTE d'un code valide sans volume. « 0X… » au lieu de « 0x… » suffit.

    Donc un code malformé est traité comme une ABSENCE de code, pas comme un
    code : on préfère un ordre visiblement non attribué à un ordre qu'on croit
    attribué. La raison précise du refus est portée par `phrase`, à imprimer.

    ## Ce que cette fonction ne dit pas

    Elle ne dit pas que les frais seront perçus. Au palier par défaut
    (*Unverified*) il n'y a aucune monétisation, et le palier ne se lit nulle
    part dans l'API. `is_attributed` signifie « lisible dans
    /builder/trades », jamais « payé ».
    """
    attribution = load_attribution()

    if attribution.code is not None:
        return OrderAttribution(
            code=attribution.code.value,
            is_attributed=True,
            phrase=f"code builder {attribution.code.short} joint",
        )

    if attribution.code_error:
        # Le cas cher. On refuse de joindre, et on dit pourquoi : sans ça
        # l'outil afficherait « AUCUNE » sur une faute de frappe à un
        # caractère, et l'utilisateur chercherait un réglage manquant qu'il a
        # pourtant fait.
        return OrderAttribution(
            code=None,
            is_attributed=False,
            phrase=(
                f"AUCUNE — {CODE_VAR} est MALFORME ({attribution.code_error}). "
                "Le code n'est PAS joint : envoye tel quel il serait accepte "
                "sans erreur et les frais seraient perdus en silence."
            ),
        )

    return OrderAttribution(
        code=None,
        is_attributed=False,
        phrase=f"AUCUNE — {CODE_VAR} n'est pas renseigne",
    )


def build_builder_config() -> Any:
    """Construit le `BuilderConfig` de `py-clob-client`, ou explique le refus.

    Import PARESSEUX, comme `build_clob_client` : le reste de DONmarket ne doit
    pas payer les secondes de chargement du SDK de signature pour afficher un
    classement en lecture seule.
    """
    attribution = load_attribution()
    if not attribution.has_api_credentials:
        # Pas d'identifiants locaux : c'est le cas de TOUT utilisateur tiers,
        # puisque le dépôt est public et que le secret n'y figure évidemment
        # pas. Le mode distant est le seul chemin par lequel son volume peut
        # être attribué — voir `remote.py`. On ne l'essaie que s'il est
        # configuré : sinon le refus ci-dessous reste le bon message.
        from .remote import build_remote_builder_config, load_remote_config

        if load_remote_config().is_configured:
            return build_remote_builder_config()

        manquants = ", ".join(n for n in API_VARS if not _present(n))
        raise AttributionNotConfigured(
            f"identifiants d'API builder absents : {manquants}. "
            "Ils se récupèrent sur polymarket.com → Settings → Builders, sur un "
            "compte connecté. Sans eux un ordre part SANS attribution et les "
            "frais sont perdus définitivement."
        )

    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

    from ..store.vault import read_secret

    # `read_secret` et non `os.environ` : une valeur scellée par DPAPI
    # (`dpapi:v1:…`) doit être descellée ici. La passer telle quelle donnerait
    # un rejet d'authentification très loin de sa cause.
    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=read_secret(API_VARS[0]),
            secret=read_secret(API_VARS[1]),
            passphrase=read_secret(API_VARS[2]),
        )
    )


def attribution_status() -> dict[str, object]:
    """L'état d'attribution tel qu'un rapport l'affiche. Aucun secret dedans."""
    from .remote import load_remote_config, remote_status

    attribution = load_attribution()
    remote = load_remote_config()
    return {
        "code": attribution.code.short if attribution.code else None,
        "code_error": attribution.code_error,
        "can_read_attribution": attribution.can_read_attribution,
        # Un tiers attribue par le signeur distant, sans jamais détenir le
        # secret : dire « can_attribute: false » parce qu'il n'a pas les
        # identifiants locaux lui ferait croire que son volume est perdu.
        "can_attribute": attribution.can_attribute or remote.is_configured,
        "attribution_mode": (
            "local"
            if attribution.can_attribute
            else "remote" if remote.is_configured else None
        ),
        **remote_status(),
        "missing": list(attribution.missing),
        # Le palier Verified ne se lit nulle part dans l'API : il dépend d'une
        # approbation humaine. On ne prétend donc JAMAIS savoir si les frais
        # seront réellement perçus.
        "tier_is_unknown": True,
    }
