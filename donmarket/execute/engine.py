"""Le moteur d'exécution — la seule partie du dépôt qui dépense de l'argent.

## Ce qui est délégué, et pourquoi

La signature des ordres passe par `py-clob-client`, le client officiel de
Polymarket. C'est la seule dépendance externe du projet, et elle est assumée :
un ordre Polymarket est une structure EIP-712 signée puis authentifiée par des
en-têtes HMAC dérivés d'une clé API elle-même dérivée de la clé privée. Écrire
cela à la main donnerait un code qui passe les tests et se trompe en
production — et l'erreur se paierait en dollars, pas en trace d'exception.

## Trois verrous, dans cet ordre

1. **`armed`** — faux par défaut. Sans lui, le moteur calcule, journalise et ne
   signe rien. C'est le mode par lequel tout doit passer d'abord.
2. **Les plafonds** (`execute/limits`) — appliqués AVANT signature, donc avant
   qu'un dollar puisse bouger. Purs, testables sans compte.
3. **La clé** — absente, rien ne part, quel que soit l'état des deux autres.

Aucun de ces verrous ne se lève tout seul. En particulier, `armed=True` n'est
jamais déduit de la présence d'une clé : avoir une clé configurée veut dire
qu'on POURRAIT trader, pas qu'on a décidé de le faire maintenant.

## Ce que ce module ne fait jamais

Il ne lit pas de secret hors de `execute/credentials`, n'en journalise aucun,
et n'en met aucun dans un objet renvoyé. Les messages d'erreur du client sont
tronqués avant journalisation : une requête signée rejetée peut contenir des
en-têtes d'authentification dans sa trace.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Sequence

from .credentials import API_VARS, PRIVATE_KEY_VAR, load_credentials
from .limits import ExecutionLimits, GateDecision, gate, order_cost_usd

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"

# Polygon. Polymarket ne règle sur aucune autre chaîne : une erreur ici ne
# produit pas un ordre invalide mais un ordre signé pour un autre réseau.
POLYGON_CHAIN_ID = 137

# Type de signature attendu par le CLOB.
#
# 0 = clé privée qui détient elle-même l'USDC (portefeuille externe).
# 1 = compte créé par e-mail sur polymarket.com : les fonds sont sur un proxy,
#     la clé ne fait que signer pour lui.
# 2 = portefeuille de navigateur connecté à polymarket.com, proxy également.
#
# Se tromper ne casse pas la signature : le CLOB accepte l'ordre puis le rejette
# pour solde insuffisant, en pointant une adresse qui n'est pas celle où l'argent
# se trouve. C'est le piège le plus déroutant de cette API, d'où le réglage
# explicite plutôt qu'un défaut silencieux.
SIGNATURE_TYPE_EOA = 0
SIGNATURE_TYPE_EMAIL_PROXY = 1
SIGNATURE_TYPE_BROWSER_PROXY = 2

SIGNATURE_TYPE_VAR = "POLYMARKET_SIGNATURE_TYPE"
FUNDER_VAR = "POLYMARKET_FUNDER"

VALID_SIGNATURE_TYPES = (
    SIGNATURE_TYPE_EOA,
    SIGNATURE_TYPE_EMAIL_PROXY,
    SIGNATURE_TYPE_BROWSER_PROXY,
)


def configured_signature_type() -> int:
    """Type de signature lu dans l'environnement.

    Pas de défaut implicite : recharger depuis polymarket.com (carte, PayPal,
    virement) place les fonds sur un PROXY, donc le type 1 ou 2 ; seule une clé
    qui détient elle-même l'USDC relève du type 0. Un défaut à 0 conviendrait
    donc à la minorité des cas et échouerait de la façon la plus déroutante qui
    soit — ordre accepté, puis rejeté pour solde insuffisant sur une adresse
    vide. On exige que ce soit dit.
    """
    raw = os.getenv(SIGNATURE_TYPE_VAR)
    if raw is None or not raw.strip():
        raise ExecutionRefused(
            f"{SIGNATURE_TYPE_VAR} non défini. 0 = la clé détient l'USDC ; "
            "1 = compte polymarket.com par e-mail (fonds sur un proxy) ; "
            "2 = portefeuille de navigateur connecté à polymarket.com."
        )
    try:
        value = int(raw.strip())
    except ValueError:
        raise ExecutionRefused(f"{SIGNATURE_TYPE_VAR} doit être 0, 1 ou 2") from None
    if value not in VALID_SIGNATURE_TYPES:
        raise ExecutionRefused(f"{SIGNATURE_TYPE_VAR} doit être 0, 1 ou 2, pas {value}")
    return value


def configured_funder() -> str | None:
    """Adresse qui DÉTIENT les fonds — le proxy, pas la clé, en type 1 ou 2.

    `POLYMARKET_FUNDER` prime sur `POLYMARKET_ADDRESS` : la seconde est
    l'adresse de la clé, et les confondre est exactement l'erreur que le
    réglage du type de signature cherche à éviter.
    """
    return os.getenv(FUNDER_VAR) or os.getenv("POLYMARKET_ADDRESS") or None


class ExecutionRefused(RuntimeError):
    """Le moteur refuse d'agir. Jamais levée pour une erreur réseau."""


@dataclass(frozen=True)
class SentOrder:
    """Un ordre effectivement transmis, et ce que le CLOB en a dit."""

    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    cost_usd: float
    order_id: str | None
    accepted: bool
    detail: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    """Le compte rendu complet : envoyé, refusé par le portier, échoué.

    Les trois catégories sont distinctes parce qu'elles appellent trois
    réactions différentes. Un refus de portier est un réglage à revoir ; un
    échec CLOB est un problème de compte ou de marché ; un envoi accepté est de
    l'argent qui a bougé.
    """

    armed: bool
    sent: tuple[SentOrder, ...] = ()
    refused: tuple[tuple[object, str], ...] = ()
    failed: tuple[tuple[object, str], ...] = ()

    @property
    def accepted_count(self) -> int:
        return sum(1 for order in self.sent if order.accepted)

    @property
    def engaged_usd(self) -> float:
        return sum(order.cost_usd for order in self.sent if order.accepted)

    @property
    def is_dry_run(self) -> bool:
        return not self.armed


# En-têtes d'authentification du CLOB : leur VALEUR ne doit jamais atteindre un
# journal, quel que soit le chemin par lequel elle y arrive.
# Noms RELEVÉS en direct le 2026-08-16 sur des en-têtes builder réellement
# signés : POLY_BUILDER_API_KEY, POLY_BUILDER_PASSPHRASE,
# POLY_BUILDER_SIGNATURE, POLY_BUILDER_TIMESTAMP. Le segment « BUILDER_ » était
# absent de la première version de ce motif, qui ne filtrait donc AUCUN des
# en-têtes réellement produits — une protection qui ne protégeait rien, et dont
# rien ne l'aurait signalé.
_AUTH_HEADER_PATTERN = re.compile(
    r"(POLY[_-](?:BUILDER[_-])?"
    r"(?:API[_-]KEY|PASSPHRASE|SIGNATURE|NONCE|TIMESTAMP)['\"?:=\s]+)"
    r"([A-Za-z0-9+/=_-]{8,})",
    re.IGNORECASE,
)

# Les variables dont la valeur est un secret. Le code builder n'en est PAS un
# (il figure dans chaque ligne publique de /builder/trades) : le retirer ne
# ferait que rendre les erreurs d'attribution indéchiffrables.
_SECRET_VARS = (
    PRIVATE_KEY_VAR,
    *API_VARS,
    "POLYMARKET_BUILDER_API_KEY",
    "POLYMARKET_BUILDER_API_SECRET",
    "POLYMARKET_BUILDER_API_PASSPHRASE",
)


def _redact(message: object, limit: int = 200) -> str:
    """Retire les secrets d'un message d'erreur, PUIS le tronque.

    Tronquer ne suffisait pas, et c'était le défaut de la version précédente :
    la représentation d'une exception de requête signée commence souvent par
    l'URL et les en-têtes, donc les 200 premiers caractères conservés étaient
    précisément ceux qu'il fallait retirer. On substitue d'abord, on coupe
    ensuite — l'ordre inverse ne protège rien.

    Deux passes, comme `binance.signing.redact` : d'abord les valeurs réellement
    configurées (substitution exacte, la seule sûre), puis un filet à motifs
    pour ce qui aurait fuité par un chemin inattendu — un en-tête reconstruit,
    une réponse d'API qui renvoie la clé.
    """
    from ..store.vault import read_secret

    text = str(message).replace("\n", " ")

    for name in _SECRET_VARS:
        # `read_secret` et non `os.getenv` : une valeur scellée par DPAPI se lit
        # `dpapi:v1:…` dans l'environnement, alors que c'est la valeur DÉSCELLÉE
        # qui voyage dans les en-têtes et qui apparaîtra dans une erreur.
        # Substituer la forme scellée ne retirerait donc rien du tout — sceller
        # ses secrets aurait AVEUGLÉ la redaction, exactement l'inverse de
        # l'effet recherché.
        try:
            value = (read_secret(name) or "").strip()
        except Exception:
            # Un descellement impossible ne doit jamais empêcher de journaliser
            # une erreur : on retombe sur la forme brute, qui protège au moins
            # les valeurs non scellées.
            value = (os.getenv(name) or "").strip()
        # Sous 8 caractères, une substitution ferait plus de dégâts qu'elle
        # n'en éviterait : elle mutilerait des fragments de message anodins.
        if len(value) >= 8:
            text = text.replace(value, "***")
            if value.startswith("0x"):
                text = text.replace(value[2:], "***")

    text = _AUTH_HEADER_PATTERN.sub(r"\1***", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def preflight() -> tuple[bool, tuple[str, ...]]:
    """Ce qui manque pour pouvoir signer. Ne renvoie jamais de valeur secrète."""
    credentials = load_credentials()
    missing: list[str] = []
    if not credentials.has_private_key:
        missing.append(PRIVATE_KEY_VAR)
    if not credentials.has_api_credentials:
        missing.extend(name for name in API_VARS if not os.getenv(name))
    return (not missing), tuple(missing)


def build_clob_client(*, signature_type: int | None = None, funder: str | None = None):
    """Construit le client officiel, identifiants API compris.

    Importé ici et non en tête de module : `py-clob-client` tire `web3` et ses
    dépendances de cryptographie, soit plusieurs secondes d'import. Le reste de
    DONMARKET — balayage, mesures, page locale — n'en a aucun besoin et ne doit
    pas les payer.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    # `read_secret` et non `os.getenv` : une valeur scellée par DPAPI
    # (`dpapi:v1:…`) doit être descellée AVANT d'atteindre le signataire. La
    # passer telle quelle produirait une clé privée invalide, donc une adresse
    # dérivée fausse, donc un rejet pour « solde insuffisant » sur un compte
    # vide — un symptôme situé très loin de sa cause.
    from ..store.vault import read_secret

    private_key = read_secret(PRIVATE_KEY_VAR)
    if not private_key:
        raise ExecutionRefused(f"{PRIVATE_KEY_VAR} absente : aucune signature possible")

    resolved_type = (
        configured_signature_type() if signature_type is None else signature_type
    )
    resolved_funder = funder or configured_funder()

    # ATTRIBUTION. Sans `builder_config`, le client ne signe aucun en-tête
    # builder et le volume routé n'est attribué à personne — définitivement,
    # puisque l'attribution se joue à la signature et jamais après coup. C'est
    # une perte silencieuse : l'ordre passe normalement, seul le revenu manque.
    #
    # L'absence d'identifiants n'est PAS une erreur : on peut vouloir trader
    # sans être builder. On journalise, on continue.
    builder_config = None
    try:
        from ..builder.attribution import build_builder_config

        builder_config = build_builder_config()
        logger.info("Attribution builder active — le volume routé sera attribué")
    except Exception as exc:  # identifiants absents, SDK indisponible…
        logger.warning(
            "Attribution builder INACTIVE (%s) : les ordres partiront sans "
            "attribution et les frais correspondants seront perdus",
            type(exc).__name__,
        )

    client = ClobClient(
        CLOB_HOST,
        chain_id=POLYGON_CHAIN_ID,
        key=private_key,
        signature_type=resolved_type,
        funder=resolved_funder,
        builder_config=builder_config,
    )

    key = read_secret("POLYMARKET_API_KEY")
    secret = read_secret("POLYMARKET_API_SECRET")
    passphrase = read_secret("POLYMARKET_API_PASSPHRASE")
    if key and secret and passphrase:
        client.set_api_creds(ApiCreds(key, secret, passphrase))
    else:
        # Dérivation déterministe depuis la clé privée : la même clé redonne
        # toujours les mêmes identifiants, donc régénérer n'invalide rien.
        logger.info("Identifiants API absents du .env — dérivation depuis la clé privée")
        client.set_api_creds(client.create_or_derive_api_creds())

    return client


def execute_plan(
    orders: Sequence[object],
    *,
    limits: ExecutionLimits,
    armed: bool = False,
    already_engaged_usd: float = 0.0,
    signature_type: int | None = None,
    funder: str | None = None,
) -> ExecutionResult:
    """Applique les plafonds, puis signe et envoie — seulement si `armed`.

    `armed` est faux par défaut et ne se déduit de rien. Le mode non armé
    parcourt exactement le même chemin, plafonds compris, et s'arrête juste
    avant la signature : c'est ce qui permet de vérifier le comportement du
    portier sans qu'un dollar puisse partir.
    """
    decision: GateDecision = gate(
        orders, limits=limits, already_engaged_usd=already_engaged_usd
    )

    if not armed:
        logger.info(
            "MOTEUR NON ARMÉ — %d ordre(s) auraient été envoyés, %d refusés par "
            "les plafonds. Rien n'est parti.",
            decision.allowed_count,
            decision.refused_count,
        )
        planned = tuple(
            SentOrder(
                condition_id=getattr(order, "condition_id", ""),
                token_id=getattr(order, "token_id", ""),
                side=getattr(order, "side", ""),
                price=float(getattr(order, "price", 0.0)),
                size=float(getattr(order, "size", 0.0)),
                cost_usd=order_cost_usd(order),
                order_id=None,
                accepted=False,
                detail="non armé",
            )
            for order in decision.allowed
        )
        return ExecutionResult(armed=False, sent=planned, refused=decision.refused)

    ready, missing = preflight()
    if not ready:
        raise ExecutionRefused(
            "moteur armé mais identifiants incomplets : " + ", ".join(missing)
        )

    from py_clob_client.clob_types import OrderArgs, OrderType

    client = build_clob_client(signature_type=signature_type, funder=funder)

    sent: list[SentOrder] = []
    failed: list[tuple[object, str]] = []

    # POURQUOI AUCUN `builder_code` ICI, alors que les six autres poseurs
    # d'ordres en ont un depuis le 2026-09-02. Ce n'est pas un oubli, c'est un
    # SDK different, et le noter evite qu'on « repare » ce fichier au prochain
    # passage :
    #
    #   `py_clob_client.clob_types.OrderArgs` porte exactement huit champs --
    #   token_id, price, size, side, fee_rate_bps, nonce, expiration, taker --
    #   et `post_order(order, orderType, post_only)` n'en prend pas davantage.
    #   Il n'existe AUCUN parametre d'attribution par ordre sur ce chemin
    #   (verifie par introspection le 2026-09-02, py-clob-client 0.34.6).
    #   L'attribution s'y fait entierement au niveau du CLIENT, par le
    #   `builder_config=` passe dans `build_clob_client` ci-dessus -- ce qui
    #   etait deja branche, et reste la seule voie possible ici.
    #
    # DETTE SEPAREE, PLUS GRAVE QUE L'ATTRIBUTION : `py-clob-client` est
    # ARCHIVE (« no longer functional, should not be used ») et le CLOB rejette
    # ses ordres par « invalid order version, please use the latest
    # clob-client ». Ce moteur est donc probablement mort a l'envoi, pas
    # seulement non attribue. Le reste du depot est passe a `polymarket-client`
    # le 2026-08-20 ; ce fichier ne l'a pas suivi. A traiter comme une
    # migration a part entiere, avec une mesure a l'appui -- pas au detour d'un
    # correctif d'attribution.
    for order in decision.allowed:
        try:
            signed = client.create_order(
                OrderArgs(
                    token_id=order.token_id,
                    price=float(order.price),
                    size=float(order.size),
                    side=order.side,
                )
            )
            # GTC : l'ordre reste au carnet. C'est le seul type qui marque des
            # points de récompense — un FOK est exécuté ou annulé sur-le-champ
            # et n'est jamais présent au moment de l'échantillonnage du score.
            response = client.post_order(signed, OrderType.GTC)
        except Exception as exc:  # le client lève des types variés selon l'étage
            logger.warning("Ordre refusé par le CLOB : %s", _redact(exc))
            failed.append((order, _redact(exc)))
            continue

        accepted = bool(response.get("success", False)) if isinstance(response, dict) else False
        order_id = response.get("orderID") if isinstance(response, dict) else None
        sent.append(
            SentOrder(
                condition_id=getattr(order, "condition_id", ""),
                token_id=order.token_id,
                side=order.side,
                price=float(order.price),
                size=float(order.size),
                cost_usd=order_cost_usd(order),
                order_id=order_id,
                accepted=accepted,
                detail="" if accepted else _redact(response),
            )
        )

    logger.info(
        "MOTEUR ARMÉ — %d accepté(s), %d échec(s), %d refusé(s) par les plafonds",
        sum(1 for o in sent if o.accepted),
        len(failed),
        decision.refused_count,
    )
    return ExecutionResult(
        armed=True,
        sent=tuple(sent),
        refused=decision.refused,
        failed=tuple(failed),
    )
