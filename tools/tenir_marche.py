# -*- coding: utf-8 -*-
"""Lance la boucle de tenue de marche sur Polymarket.

Assemble les trois morceaux : la lecture de l'univers (`api/gamma` + `api/clob`),
la decision (`making/core`) et l'execution (`making/runner` sur le SDK
`polymarket`).

    .venv\\Scripts\\python tools\\tenir_marche.py --bankroll 4 --minutes 2
    .venv\\Scripts\\python tools\\tenir_marche.py --bankroll 4 --minutes 30 --arm

Sans `--arm`, rien ne part : la boucle lit, planifie et affiche ce qu'elle
poserait. C'est le mode par lequel il faut passer d'abord.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def lire_univers(capital: float):
    """Rend (branches cotables, motifs de rejet). Synchrone pour la boucle.

    La lecture est asynchrone et lente -- 2100 marches, 4200 carnets, une
    trentaine de secondes. On l'enveloppe ici pour que `run_making` reste un
    code synchrone simple, testable sans place de marche.
    """
    from donmarket.api import clob, gamma
    from donmarket.making.core import eligible

    async def _lire():
        marches = await gamma.fetch_all_markets()
        negociables = [m for m in marches if m.is_tradable]
        jetons = [t for m in negociables for t in m.token_ids]
        carnets = await clob.fetch_books(jetons)
        return eligible(negociables, carnets, capital_usd=capital)

    return asyncio.run(_lire())


def main() -> int:
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.making.runner import run_making
    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, required=True)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-markets", type=int, default=2)
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env", override=True)

    print("=" * 70)
    print("POLYMARKET -- TENUE DE MARCHE")
    print("=" * 70)
    if args.arm:
        print(
            f"\nARMEE -- jusqu'a {args.bankroll:.2f} $ reellement engages, "
            f"sur {args.max_markets} branche(s), pendant {args.minutes:.0f} min."
        )
    else:
        print("\nDESARMEE -- la boucle planifie et n'envoie rien.")
        print("  Ajouter --arm pour poser reellement les ordres.")

    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )

    rapport = run_making(
        client,
        lambda: lire_univers(args.bankroll / max(args.max_markets, 1)),
        bankroll=args.bankroll,
        minutes=args.minutes,
        interval_s=args.interval,
        max_markets=args.max_markets,
        armed=args.arm,
    )

    print(f"\n{rapport.ticks} tour(s)")
    print(f"  ordres poses          : {rapport.placed}")
    print(f"  refuses (post_only)   : {rapport.refused}")
    print(f"  annules               : {rapport.cancelled}")
    print(f"  conserves en file     : {rapport.kept}")
    print(f"  ordres etrangers vus  : {rapport.foreign_seen}  (laisses intacts)")

    if rapport.problem:
        # Dit AVANT tout chiffre : une boucle qui s'est abstenue n'a pas
        # mesure la strategie, elle a mesure son propre refus.
        print(f"\nINVENTAIRE ILLISIBLE : {rapport.problem}")
        print("  La boucle s'est abstenue plutot que d'acheter a l'aveugle.")

    if rapport.left_open:
        print(f"\n{len(rapport.left_open)} ordre(s) PEUT-ETRE encore au carnet :")
        for oid in rapport.left_open:
            print(f"  {oid}")
        print("  Le nettoyage final a ete refuse -- verifier sur polymarket.com.")

    print(
        "\nUn ordre pose n'est pas un ordre rempli. Le taux de remplissage est\n"
        "le seul chiffre qui decide, et il ne se lit que sur des ordres qui ont\n"
        "vecu au carnet -- pas sur les ecarts affiches."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
