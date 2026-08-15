"""Serveur HTTP local — bibliothèque standard uniquement, boucle locale seule.

Deux décisions structurent ce fichier.

**Le routage est une fonction pure.** `handle_request` prend une méthode, un
chemin et un état, et rend une réponse. Elle ne touche ni socket ni fil, donc
elle se teste sans ouvrir de port — et les règles qui comptent (un GET ne
déclenche pas de balayage, un chemin inconnu fait un 404 propre) sont vérifiées
en mémoire.

**Le balayage tourne à part.** Il dure 48 à 190 secondes ; le tenir dans un
gestionnaire HTTP ferait expirer le navigateur et bloquerait le serveur entier.
Il part donc sur un fil, et la page interroge `/api/state` toutes les deux
secondes.

Le serveur écoute sur 127.0.0.1 et nulle part ailleurs. Ce n'est pas de la
prudence de principe : la page affiche des positions chiffrées sur de l'argent
réel, et une écoute sur 0.0.0.0 les exposerait à tout le réseau local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from ..analysis.opportunities import Mode
from ..execute.credentials import connection_status
from ..scan.rewards_scan import HISTORY_BUDGET, scan_rewards
from ..watch.monitor import LiquidityProbe, LiveMonitor
from .page import PAGE
from .payload import scan_payload
from .state import ScanState, Status

logger = logging.getLogger(__name__)

# Non négociable : voir le docstring du module.
HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Au-delà, ce n'est plus un capital saisi mais une faute de frappe.
MAX_BANKROLL_USD = 1_000_000.0

# L'avatar est servi depuis le disque plutôt qu'encodé dans la page : une image
# de 400 Ko en base64 alourdirait chaque rechargement du HTML de 550 Ko.
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
AVATAR_PATH = ASSETS_DIR / "avatar.png"


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes


def _json(payload: dict, *, status: int = 200) -> Response:
    return Response(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def state_payload(state: ScanState, monitor: LiveMonitor | None = None) -> dict:
    """L'état complet tel que la page le consomme.

    L'âge des carnets voyage avec eux. Un carnet de dix minutes présenté comme
    « temps réel » est pire qu'un carnet absent, parce qu'il est cru : c'est à
    la page de pouvoir dire depuis quand le flux ne dit plus rien.
    """
    snapshot = state.snapshot()
    live = monitor.snapshot() if monitor is not None else None
    return {
        "status": snapshot.status.value,
        "error": snapshot.error,
        "started_at": snapshot.started_at.isoformat() if snapshot.started_at else None,
        "finished_at": snapshot.finished_at.isoformat() if snapshot.finished_at else None,
        "connection": connection_status(),
        "live": {
            "tokens": len(live.tokens) if live else 0,
            "books": len(live.books) if live else 0,
            "updates": live.updates if live else 0,
            "age_seconds": live.age_seconds if live else None,
        },
        "scan": (
            scan_payload(snapshot.result, live=live) if snapshot.result else None
        ),
    }


# Écouter sur le loopback ne veut pas dire être à l'abri du web. N'importe quel
# site ouvert dans le MÊME navigateur peut poster vers 127.0.0.1 : la réponse lui
# est refusée, mais l'ACTION part quand même (requête « simple », sans préflight).
# Aujourd'hui le pire est un balayage déclenché à distance ; le jour où un POST
# posera un ordre, ce serait la même faille et un autre prix.
#
# Un navigateur moderne envoie `Sec-Fetch-Site` partout et `Origin` sur toute
# écriture. Un client hors navigateur (curl, script local) n'envoie ni l'un ni
# l'autre — et n'est pas le vecteur qu'on craint : personne n'a besoin de piéger
# un utilisateur pour lancer curl sur sa propre machine.
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_local_write_allowed(headers: Mapping[str, str], *, port: int) -> bool:
    """Vrai si cette écriture peut venir de la page locale, et pas d'un site tiers.

    Deux signaux, l'absence des deux valant acceptation. Refuser faute d'en-tête
    casserait tout client non navigateur sans rien protéger de plus : qui
    contrôle déjà un processus local n'a pas besoin de CSRF.
    """
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ALLOWED_FETCH_SITES:
        return False

    origin = (headers.get("Origin") or "").strip()
    if origin:
        parsed = urlsplit(origin)
        if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
            return False
        # `Origin: http://127.0.0.1:AUTRE_PORT` vient d'un autre service local,
        # pas de notre page : la boucle locale n'est pas une seule origine.
        if parsed.port is not None and parsed.port != port:
            return False

    return True


def handle_request(
    method: str,
    path: str,
    *,
    state: ScanState,
    launch: Callable[[], bool],
    monitor: LiveMonitor | None = None,
) -> Response:
    """Le routeur, sans effet de bord autre que celui demandé à `launch`."""
    if path == "/" and method == "GET":
        return Response(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))

    if path == "/avatar.png":
        if method != "GET":
            return _json({"error": "méthode non autorisée"}, status=405)
        # Chemin fixe, jamais construit à partir de la requête : il n'y a rien
        # à faire remonter avec des « .. » puisque rien de la requête n'entre ici.
        try:
            return Response(200, "image/png", AVATAR_PATH.read_bytes())
        except OSError:
            return _json({"error": "avatar absent"}, status=404)

    if path == "/api/state":
        if method != "GET":
            return _json({"error": "méthode non autorisée"}, status=405)
        return _json(state_payload(state, monitor))

    if path == "/api/scan":
        # Un GET part d'une simple balise <img> ou d'un préchargement du
        # navigateur : lancer 2 100 requêtes ne doit pas être aussi facile.
        if method != "POST":
            return _json({"error": "utiliser POST"}, status=405)
        return _json({"started": launch()})

    return _json({"error": "chemin inconnu"}, status=404)


def parse_bankroll(body: bytes, *, default: float) -> float:
    """Lit le capital envoyé par la page, sans jamais faire confiance au corps.

    Toute valeur absente, illisible, négative ou délirante retombe sur la
    valeur passée en ligne de commande : le serveur ne doit pas planter parce
    qu'un champ a été vidé.
    """
    try:
        value = float(json.loads(body or b"{}").get("bankroll"))
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return default
    if value <= 0 or value > MAX_BANKROLL_USD:
        return default
    return value


class ScanRunner:
    """Lance un balayage sur un fil, et n'en laisse jamais tourner deux."""

    def __init__(
        self,
        state: ScanState,
        *,
        mode: Mode,
        history_budget: int,
        bankroll: float,
        monitor: LiveMonitor | None = None,
    ) -> None:
        self._state = state
        self._mode = mode
        self._history_budget = history_budget
        self._monitor = monitor
        self.default_bankroll = bankroll

    def launch(self, *, bankroll: float | None = None) -> bool:
        """Vrai si le balayage a démarré, faux s'il y en avait déjà un."""
        if not self._state.begin():
            logger.info("Balayage déjà en cours — demande ignorée.")
            return False
        amount = bankroll if bankroll is not None else self.default_bankroll
        threading.Thread(
            target=self._run, args=(amount,), name="donmarket-scan", daemon=True
        ).start()
        return True

    def _run(self, bankroll: float) -> None:
        try:
            result = asyncio.run(
                scan_rewards(
                    mode=self._mode,
                    bankroll=bankroll,
                    history_budget=self._history_budget,
                )
            )
        except Exception as exc:  # le fil ne doit jamais mourir en silence
            logger.exception("Le balayage a échoué")
            self._state.fail(f"{type(exc).__name__}: {exc}")
            return
        self._state.succeed(result)
        logger.info(
            "Balayage terminé : %d candidat(s) en %.1f s",
            result.found,
            result.duration_seconds,
        )

        # Le balayage vient de dire QUOI surveiller ; le flux prend le relais
        # sur ces jetons-là seulement — quelques dizaines, pas les 4 200 de
        # l'univers. C'est ce qui rend la surveillance continue tenable.
        if self._monitor is not None:
            self._monitor.watch(
                [
                    LiquidityProbe(
                        key=c.condition_id, token_ids=c.token_ids, band=c.max_spread
                    )
                    for c in result.candidates
                ]
            )


def _make_handler(
    state: ScanState, runner: ScanRunner, monitor: LiveMonitor | None = None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "donmarket"
        protocol_version = "HTTP/1.1"

        def _respond(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802 (nom imposé par la stdlib)
            self._respond(
                handle_request(
                    "GET",
                    self.path,
                    state=state,
                    launch=runner.launch,
                    monitor=monitor,
                )
            )

        def do_POST(self) -> None:  # noqa: N802
            if not is_local_write_allowed(
                self.headers, port=self.server.server_address[1]
            ):
                logger.warning(
                    "Écriture refusée : origine %r, Sec-Fetch-Site %r",
                    self.headers.get("Origin"),
                    self.headers.get("Sec-Fetch-Site"),
                )
                self._respond(
                    _json({"error": "origine externe refusée"}, status=403)
                )
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            amount = parse_bankroll(body, default=runner.default_bankroll)
            self._respond(
                handle_request(
                    "POST",
                    self.path,
                    state=state,
                    launch=lambda: runner.launch(bankroll=amount),
                    monitor=monitor,
                )
            )

        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(
    *,
    bankroll: float,
    port: int = DEFAULT_PORT,
    mode: Mode = Mode.SERIEUX,
    history_budget: int = HISTORY_BUDGET,
    scan_on_start: bool = True,
) -> None:
    """Démarre le tableau de bord et rend la main au Ctrl+C."""
    state = ScanState()
    monitor = LiveMonitor()
    runner = ScanRunner(
        state,
        mode=mode,
        history_budget=history_budget,
        bankroll=bankroll,
        monitor=monitor,
    )
    httpd = ThreadingHTTPServer((HOST, port), _make_handler(state, runner, monitor))

    print(f"DONmarket — tableau de bord sur http://{HOST}:{port}")
    print(
        f"Capital {bankroll:.2f} $, mode {mode.value}. "
        "Lecture seule, Ctrl+C pour arrêter."
    )

    if scan_on_start:
        # Ouvrir la page sur un écran vide en attendant un clic serait une
        # minute perdue à chaque démarrage : le premier balayage part seul.
        runner.launch()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.")
    finally:
        monitor.stop()
        httpd.server_close()


__all__ = [
    "DEFAULT_PORT",
    "HOST",
    "Response",
    "ScanRunner",
    "Status",
    "handle_request",
    "parse_bankroll",
    "serve",
    "state_payload",
]
