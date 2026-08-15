"""Génère la page publique « Builders Radar » dans `docs/`.

    python tools/build_radar.py

Pourquoi un générateur plutôt qu'un HTML figé : les taux bougent. Un builder
reconfigure son barème, un nouveau entre dans le classement, et une page écrite
à la main devient fausse sans que personne ne s'en aperçoive. Ici la page se
reconstruit d'une commande, et les chiffres viennent tous de `donmarket.builder`
— jamais d'une transcription à la main.

Le gabarit vit dans `docs/_template.html` avec un marqueur `__DATA__`. Les
données sont injectées à la construction plutôt que chargées à l'exécution :
GitHub Pages servirait bien un `fetch()` relatif, mais la page doit aussi
pouvoir être ouverte depuis le disque, envoyée en pièce jointe ou collée dans un
dossier de candidature. Une page qui dépend d'un second fichier ne survit pas à
ces trajets.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from donmarket.builder.api import (  # noqa: E402
    build_clob_client,
    build_data_client,
    fetch_builder_trades,
    fetch_leaderboard,
)
from donmarket.builder.revenue import build_estimate, rank_by_revenue  # noqa: E402

DOCS = ROOT / "docs"
TEMPLATE = DOCS / "_template.html"
OUTPUT = DOCS / "index.html"
DATA_FILE = DOCS / "radar.json"

TOP = 25

# Deux pages de 300 exécutions suffisent à lire un barème : l'estimateur est le
# MAXIMUM du taux implicite, et il est atteint dès qu'une grosse ligne apparaît.
# En aspirer davantage coûterait des minutes sans rien changer au chiffre.
PAGES = 2

# Le classement sert 50 lignes au maximum (borne de l'API).
LEADERBOARD_LIMIT = 50

# Requêtes simultanées vers le CLOB. Volontairement bas : cette page est un
# geste public, elle n'a aucune raison de taper fort sur une API gratuite.
CONCURRENCY = 6


async def collect(period: str = "WEEK") -> dict:
    """Rassemble classement et barèmes mesurés. Lecture seule, sans clé."""
    async with build_data_client() as data:
        board = await fetch_leaderboard(data, period=period, limit=LEADERBOARD_LIMIT)
        historic = await fetch_leaderboard(data, period="ALL", limit=LEADERBOARD_LIMIT)

    usable = [e for e in board if e.has_usable_code][:TOP]
    if not usable:
        raise RuntimeError("aucun builder avec un code exploitable — API changée ?")

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with build_clob_client() as clob:

        async def sample_for(entry):
            async with semaphore:
                return await fetch_builder_trades(clob, entry.code, max_pages=PAGES)

        samples = await asyncio.gather(*(sample_for(e) for e in usable))

    estimates = [
        build_estimate(entry, sample, period=period)
        for entry, sample in zip(usable, samples)
    ]
    ranked = rank_by_revenue(estimates)
    all_volume = {e.builder: e.volume for e in historic}

    rows = []
    for est in ranked:
        taker, maker = est.schedule.taker, est.schedule.maker
        rows.append(
            {
                "builder": est.builder,
                "rank_volume": est.rank,
                "volume_week": est.volume,
                "volume_all": all_volume.get(est.builder),
                "active_users": est.active_users,
                "taker_bps": None if taker is None else round(taker.bps, 2),
                "maker_bps": None if maker is None else round(maker.bps, 2),
                "blended_bps": (
                    None if est.blended_bps is None else round(est.blended_bps, 2)
                ),
                "revenue_week": est.estimated_period_revenue_usd,
                "revenue_per_user": est.revenue_per_user_usd,
                "samples": len(est.sample),
                "exceeds_cap": est.schedule.exceeds_published_cap,
                "charges_nothing": est.schedule.charges_nothing,
            }
        )

    return {
        "period": period,
        "rows": rows,
        "totals": {
            "builders_measured": len(rows),
            "charging_nothing": sum(1 for r in rows if r["charges_nothing"]),
            "volume_week_total": sum(r["volume_week"] for r in rows),
            "revenue_week_total": sum(r["revenue_week"] or 0 for r in rows),
        },
    }


def render(payload: dict) -> None:
    """Injecte les données dans le gabarit et écrit la page."""
    template = TEMPLATE.read_text(encoding="utf-8")
    if "__DATA__" not in template:
        raise RuntimeError(f"marqueur __DATA__ absent de {TEMPLATE}")

    blob = json.dumps(payload, separators=(",", ":"))
    OUTPUT.write_text(template.replace("__DATA__", blob), encoding="utf-8")
    DATA_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    payload = asyncio.run(collect())
    render(payload)

    totals = payload["totals"]
    print(f"{OUTPUT.relative_to(ROOT)} — {totals['builders_measured']} builders")
    print(f"  dont {totals['charging_nothing']} à 0 bps")
    print(f"  revenu hebdomadaire estimé, cumulé : {totals['revenue_week_total']:,.0f} $")
    hors_plafond = [r["builder"] for r in payload["rows"] if r["exceeds_cap"]]
    if hors_plafond:
        print(f"  au-dessus du plafond publié : {', '.join(hors_plafond)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
