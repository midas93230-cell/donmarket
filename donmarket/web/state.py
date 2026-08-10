"""L'état d'un balayage, partagé entre le serveur HTTP et le fil qui scanne.

Un scan de récompenses dure 48 à 190 secondes : il ne peut pas se dérouler
pendant qu'une requête HTTP attend. Il tourne donc sur un fil séparé, et la
page interroge cet état en boucle.

D'où le verrou. Sans lui, deux clics rapprochés lanceraient deux balayages
complets — 2 100 marchés et ~950 carnets chacun, sur des API publiques
gratuites. `begin()` est la porte : elle ne s'ouvre qu'une fois à la fois.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from ..scan.rewards_scan import RewardScanResult


class Status(str, Enum):
    """Où en est le balayage. Hérite de `str` pour aller tel quel dans le JSON."""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True)
class Snapshot:
    """Une photo cohérente de l'état, prise sous verrou.

    Immuable et détachée : le fil de scan peut continuer à travailler pendant
    que le serveur sérialise cette photo, sans qu'elle change sous ses pieds.
    """

    status: Status
    result: RewardScanResult | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ScanState:
    """Le seul objet mutable du serveur, et il l'est sous verrou."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = Status.IDLE
        self._result: RewardScanResult | None = None
        self._error: str | None = None
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                status=self._status,
                result=self._result,
                error=self._error,
                started_at=self._started_at,
                finished_at=self._finished_at,
            )

    def begin(self) -> bool:
        """Ouvre la porte si aucun balayage ne tourne. Faux sinon, sans lever.

        Le refus n'est pas une erreur : c'est la réponse normale à un deuxième
        clic, et l'appelant a besoin de la distinguer d'un démarrage.
        """
        with self._lock:
            if self._status is Status.RUNNING:
                return False
            self._status = Status.RUNNING
            self._error = None
            self._started_at = _now()
            self._finished_at = None
            return True

    def succeed(self, result: RewardScanResult) -> None:
        with self._lock:
            self._status = Status.DONE
            self._result = result
            self._error = None
            self._finished_at = _now()

    def fail(self, message: str) -> None:
        """Enregistre l'échec SANS effacer le dernier résultat.

        Une coupure réseau ne rend pas fausse la mesure d'il y a dix minutes :
        elle reste la meilleure information disponible, et l'utilisateur doit
        pouvoir la lire en sachant qu'elle a vieilli.
        """
        with self._lock:
            self._status = Status.ERROR
            self._error = message
            self._finished_at = _now()
