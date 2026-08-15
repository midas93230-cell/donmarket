"""Sceller les secrets du `.env` avec DPAPI — utile seulement contre la COPIE.

Une clé privée dans un `.env` en clair est lisible par tout processus de la
session Windows. DPAPI (`ProtectedData`, portée `CurrentUser`) chiffre la valeur
avec un secret dérivé du compte utilisateur : le fichier copié sur une autre
machine, ou lu sous un autre compte, ne rend plus rien.

## Ce que ça protège, et ce que ça ne protège PAS

Protégé : l'exfiltration du FICHIER. Un `.env` envoyé par erreur, poussé sur
git, récupéré dans une sauvegarde ou sur un disque revendu devient inerte.

**Pas protégé** : un programme malveillant qui tourne sous TON compte, sur TA
machine. Il appellera `Unprotect` exactement comme nous. DPAPI déplace la
barrière, il ne la supprime pas — et le prétendre serait pire que de ne rien
faire, parce que ça inviterait à relâcher la vigilance ailleurs.

## Format

Une valeur scellée est reconnaissable et se colle telle quelle dans le `.env` :

    POLYMARKET_PRIVATE_KEY=dpapi:v1:AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA…

Le fichier garde donc exactement la même forme. Une valeur en clair continue de
fonctionner : le scellement est une option, pas une migration forcée — un
utilisateur bloqué par sa propre sécurité la désactive, et se retrouve moins
protégé qu'avant.

## Coût

Chaque descellement lance un processus PowerShell (~1 à 2 s sur cette machine).
D'où le cache en mémoire : un secret donné n'est descellé qu'une fois par
processus. Le cache ne survit pas à l'arrêt, et n'est jamais écrit sur disque.
"""

from __future__ import annotations

import base64
import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)

SEALED_PREFIX = "dpapi:v1:"

# `Protect` et `Unprotect` reçoivent leur charge par l'ENTRÉE STANDARD, jamais
# par la ligne de commande : les arguments d'un processus sont lisibles par tout
# le système, ce qui annulerait l'intérêt de l'opération.
_PROTECT_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$b64 = [Console]::In.ReadToEnd().Trim()
$bytes = [Convert]::FromBase64String($b64)
$sealed = [System.Security.Cryptography.ProtectedData]::Protect(
    $bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
[Console]::Out.Write([Convert]::ToBase64String($sealed))
"""

_UNPROTECT_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$b64 = [Console]::In.ReadToEnd().Trim()
$bytes = [Convert]::FromBase64String($b64)
$clear = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
[Console]::Out.Write([Convert]::ToBase64String($clear))
"""

_cache: dict[str, str] = {}


class VaultError(RuntimeError):
    """Le scellement ou le descellement a échoué."""


class VaultUnavailable(VaultError):
    """DPAPI n'existe pas sur cette plateforme."""


def is_available() -> bool:
    """DPAPI est une API Windows. Ailleurs, il n'y a rien à proposer."""
    return platform.system() == "Windows"


def is_sealed(value: str) -> bool:
    return value.startswith(SEALED_PREFIX)


def _run_powershell(script: str, payload_b64: str) -> str:
    if not is_available():
        raise VaultUnavailable(
            "le scellement DPAPI n'existe que sous Windows ; "
            "sur cette plateforme, garder le .env en clair et le protéger "
            "par les permissions du système de fichiers"
        )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            input=payload_b64,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VaultError(f"PowerShell injoignable : {type(exc).__name__}") from exc

    if completed.returncode != 0:
        # La sortie d'erreur peut contenir un fragment de la charge : on ne
        # retient QUE le code de retour et la première ligne du message.
        lignes = (completed.stderr or "").strip().splitlines()
        detail = lignes[0][:120] if lignes else "aucun détail"
        raise VaultError(f"DPAPI a échoué (code {completed.returncode}) : {detail}")

    return completed.stdout.strip()


def seal(plaintext: str) -> str:
    """Scelle une valeur. Rend la chaîne à coller dans le `.env`."""
    if not plaintext:
        raise VaultError("rien à sceller : valeur vide")
    payload = base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    return SEALED_PREFIX + _run_powershell(_PROTECT_SCRIPT, payload)


def unseal(value: str) -> str:
    """Descelle `dpapi:v1:…`. Une valeur en clair est rendue telle quelle."""
    if not is_sealed(value):
        return value
    if value in _cache:
        return _cache[value]

    clear_b64 = _run_powershell(_UNPROTECT_SCRIPT, value[len(SEALED_PREFIX) :])
    try:
        clear = base64.b64decode(clear_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError("la valeur descellée n'est pas du texte valide") from exc

    _cache[value] = clear
    return clear


def read_secret(name: str) -> str | None:
    """Lit une variable d'environnement, en la descellant si besoin.

    C'est le point d'entrée que le reste du code doit utiliser à la place de
    `os.getenv` pour toute valeur secrète. Un appelant qui oublie ne casse rien
    de visible : il reçoit la chaîne `dpapi:v1:…` telle quelle et l'API la
    rejette pour identifiants invalides — un échec obscur, très loin de sa
    cause. D'où l'existence de cette fonction plutôt qu'un descellement
    implicite au chargement.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return unseal(raw.strip())


def clear_cache() -> None:
    """Vide le cache en mémoire. Utile aux tests, et après rotation d'un secret."""
    _cache.clear()
