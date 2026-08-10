"""Signature des requêtes Binance SAPI. Module PUR : aucun réseau, aucun disque.

Binance signe en HMAC-SHA256 la **chaîne de requête telle qu'elle est
transmise**. Tout l'enjeu de ce module tient dans ce « telle qu'elle est » :
la chaîne signée et la chaîne envoyée doivent coïncider **octet pour octet**.
Sitôt qu'une couche HTTP réencode quoi que ce soit après la signature, le
serveur signe autre chose que nous et rend `-1022`.

PIÈGE DOCUMENTÉ PAR BINANCE (change-log du 2026-06-16), et il n'est pas
théorique : `batch-cancel` emploie des clés à crochets indexés
(`cancelInfoList[0].orderId`). Les bibliothèques HTTP courantes — `requests`
en Python, `net/url` en Go, `URI` en Java, `url` en Node — encodent
automatiquement `[` en `%5B` et `]` en `%5D` **dans les clés**. Le serveur,
lui, signe les octets bruts. Résultat : `-1022 Signature for this request is
not valid`, sur une signature pourtant correcte. Binance conseille de
descendre à `http.client`.

CE CONSEIL EST INUTILE ICI, et c'est une mesure, pas une supposition :
`httpx.URL.copy_with(raw_path=...)` transmet les octets sans les normaliser
(vérifié le 2026-08-09 sur httpx 0.28.1 — les crochets ressortent intacts de
`Request.url.raw_path`). DONmarket garde donc httpx partout. Le test
`test_les_crochets_ne_sont_jamais_encodes` est le gardien de cette propriété :
s'il tombe, la signature est cassée en production avant de l'être ici.

Règle d'encodage retenue, et pourquoi elle est asymétrique :

  - les **clés** sont émises BRUTES (et validées : seuls lettres, chiffres,
    `_ . - [ ]` sont admis, sinon on lève). C'est là que vit le piège ;
  - les **valeurs** sont percent-encodées, comme le fait n'importe quel client
    Binance standard. Une valeur peut légitimement contenir `&` ou `=`, qui
    briseraient la chaîne si on les laissait passer.

Cette asymétrie n'est pas un compromis esthétique : elle vise exactement le
seul endroit où Binance s'écarte de l'usage.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any, Mapping
from urllib.parse import quote

# Une clé de paramètre légitime chez Binance. Les crochets sont ADMIS — c'est
# tout l'objet du module. Le reste est refusé plutôt que silencieusement
# encodé : une clé exotique signifie qu'on s'est trompé d'appel, pas qu'il faut
# l'échapper.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")

# Ce qui reste littéral dans une valeur. Volontairement restrictif : tout le
# reste est percent-encodé.
VALUE_SAFE = "-_.~"


class SigningError(ValueError):
    """La requête ne peut pas être signée telle qu'elle est décrite."""


def _encode_value(value: Any, *, where: str) -> str:
    """Rend la forme textuelle exacte que Binance attend, puis l'encode.

    Les booléens Python (`True`) ne s'écrivent pas comme les booléens JSON
    (`true`) : laisser `str(True)` passer enverrait « True », que le serveur
    ne reconnaît pas — et l'erreur reviendrait sous forme de paramètre ignoré,
    pas de refus. On traduit donc explicitement.
    """
    if value is None:
        raise SigningError(f"{where} : valeur absente — ne pas envoyer le paramètre")
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, float):
        # `str(1e-05)` donne « 1e-05 », que Binance lit mal. On passe par une
        # écriture décimale sans exposant, sans zéros de queue superflus.
        text = f"{value:.8f}".rstrip("0").rstrip(".") or "0"
    else:
        text = str(value)
    return quote(text, safe=VALUE_SAFE)


def canonical_query(params: Mapping[str, Any], *, sort: bool = False) -> str:
    """Assemble la chaîne exacte à signer ET à transmettre.

    `sort=True` trie les clés par ordre alphabétique : c'est ce qu'exige la
    poignée de main WebSocket (`/sapi/wss`). Le REST, lui, signe l'ordre
    d'émission — donc on ne trie PAS par défaut, sous peine de signer une
    chaîne que l'on n'enverra pas si l'appelant construit l'URL autrement.

    Les paramètres à valeur `None` sont OMIS. C'est délibéré : sur SAPI, un
    paramètre optionnel envoyé vide n'est pas équivalent à un paramètre absent.
    """
    items = list(params.items())
    if sort:
        items.sort(key=lambda kv: kv[0])

    parts: list[str] = []
    for key, value in items:
        if value is None:
            continue
        if not KEY_PATTERN.match(key):
            raise SigningError(
                f"nom de paramètre refusé {key!r} : seuls [A-Za-z0-9_.-[]] sont admis "
                "(un nom exotique trahit un appel mal construit, pas un besoin "
                "d'échappement)"
            )
        parts.append(f"{key}={_encode_value(value, where=key)}")
    return "&".join(parts)


def sign(query: str, secret: str) -> str:
    """HMAC-SHA256 hexadécimal de la chaîne, avec le secret comme clé."""
    if not secret:
        raise SigningError("secret d'API absent — impossible de signer")
    return hmac.new(
        secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def now_ms() -> int:
    """Horodatage en millisecondes, tel que Binance le veut.

    Isolé pour être remplaçable en test, et parce que c'est le point de panne
    le plus banal : une horloge décalée de plus de `recvWindow` fait rendre
    `-1021 Timestamp for this request is outside of the recvWindow`, message
    qui ne dit pas « ton horloge est fausse ».
    """
    return int(time.time() * 1000)


def signed_query(
    params: Mapping[str, Any],
    *,
    secret: str,
    recv_window_ms: int | None = None,
    timestamp_ms: int | None = None,
    sort: bool = False,
) -> str:
    """Chaîne complète, signature comprise, prête à être transmise telle quelle.

    `signature` est ajoutée EN DERNIER et n'entre évidemment pas dans son
    propre calcul. Le retour est destiné à être passé à `raw_path` sans
    retouche : toute modification ultérieure invalide la signature.
    """
    enriched: dict[str, Any] = dict(params)
    if recv_window_ms is not None:
        enriched.setdefault("recvWindow", recv_window_ms)
    enriched.setdefault(
        "timestamp", timestamp_ms if timestamp_ms is not None else now_ms()
    )

    payload = canonical_query(enriched, sort=sort)
    return f"{payload}&signature={sign(payload, secret)}"


def redact(text: str, *secrets: str) -> str:
    """Retire clés et secrets d'un texte avant journalisation.

    Une requête signée rejetée revient avec ses en-têtes et sa chaîne de
    requête ; les journaliser telles quelles publierait la clé d'API dans un
    fichier de log. Même logique que `_redact()` du moteur d'exécution
    Polymarket.
    """
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "***")
    # Ceinture et bretelles : une signature ou une clé qui aurait fuité par un
    # autre chemin que les secrets connus.
    cleaned = re.sub(r"(signature=)[0-9a-fA-F]{16,}", r"\1***", cleaned)
    cleaned = re.sub(r"(X-MBX-APIKEY['\":\s]+)[A-Za-z0-9]{16,}", r"\1***", cleaned)
    return cleaned
