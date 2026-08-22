# -*- coding: utf-8 -*-
"""Releve LECTURE SEULE du compte Polymarket : solde, positions, ordres, trades.

    .venv/Scripts/python tools/releve.py

Aucun ordre n'est pose ni annule. Ce fichier ne doit jamais rien ecrire sur la
place de marche -- c'est le seul outil qu'on puisse lancer sans reflechir.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")


def flat(paginator):
    """Un `Paginator` itere des PAGES, pas des lignes (piege mesure le 20/08)."""
    out = []
    for page in paginator:
        if isinstance(page, (list, tuple)):
            out.extend(page)
        else:
            out.append(page)
    return out


def champ(row, *noms, defaut=None):
    for n in noms:
        v = getattr(row, n, None)
        if v is not None:
            return v
    return defaut


def main() -> int:
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.store import vault

    load_dotenv(".env", override=True)
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )

    print("=" * 70)
    print(f"RELEVE POLYMARKET -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 70)

    try:
        bal = client.get_balance_allowance(asset_type="COLLATERAL")
        print(f"\nSOLDE : {bal}")
    except Exception as exc:
        print(f"\nSOLDE illisible : {exc}")

    for titre, appel in (
        ("ORDRES OUVERTS", client.list_open_orders),
        ("POSITIONS", client.list_positions),
        ("TRADES", client.list_trades),
    ):
        print(f"\n--- {titre} ---")
        try:
            lignes = flat(appel())
        except Exception as exc:
            print(f"  illisible : {exc}")
            continue
        if not lignes:
            print("  (aucune)")
            continue
        for r in lignes:
            print(f"  {r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
