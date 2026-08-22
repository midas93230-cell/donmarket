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
import json
import logging
import time
import os
import sys

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def lire_univers(capital: float, improve_ticks: int = 0):
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
        rungs, rejets = eligible(
            negociables, carnets, capital_usd=capital,
            improve_ticks=improve_ticks,
        )
        # Les carnets remontent avec : la boucle en a besoin pour coter la
        # SORTIE de positions devenues non eligibles, absentes de `rungs`.
        return rungs, rejets, carnets

    return asyncio.run(_lire())


def main() -> int:
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.making.runner import flatten, run_making
    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, required=True)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-markets", type=int, default=2)
    parser.add_argument(
        "--improve", type=int, default=0,
        help=(
            "ameliorer le meilleur prix de N pas pour passer devant la "
            "file. Coute N pas d ecart et achete la priorite -- arbitrage "
            "que seule la mesure du remplissage peut trancher."
        ),
    )
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

    # REPRISE APRES RELANCE (2026-08-22). Les ordres d'une session precedente
    # survivent au carnet -- ils n'ont AUCUNE expiration, verifie ce jour-la
    # sur les trois ordres en cours. Sans ce fichier ils reviennent etrangers
    # a la boucle : elle ne peut ni les recoter ni les annuler, et la cle reste
    # bloquee. On ne reprend QUE des identifiants encore ouverts, pour ne pas
    # trainer indefiniment une liste de fantomes.
    memoire = os.path.join(os.environ.get("TEMP", "."), "donmarket-ordres.json")
    connus: set[str] = set()
    if os.path.exists(memoire):
        try:
            with open(memoire, encoding="utf-8") as f:
                connus = set(json.load(f))
        except (OSError, ValueError) as exc:
            # Illisible => on n'adopte RIEN. Se tromper ici ferait annuler des
            # ordres qui ne sont pas les notres.
            print(f"memoire d'ordres illisible ({exc}) -- aucune adoption")
            connus = set()
    if connus:
        try:
            ouverts = {
                str(getattr(o, "id", getattr(o, "order_id", "")))
                for o in flatten(client.list_open_orders())
            }
            adoptes = connus & ouverts
            perimes = connus - ouverts
            print(f"\nREPRISE : {len(adoptes)} ordre(s) de la session precedente "
                  f"adopte(s){f', {len(perimes)} perime(s) oublie(s)' if perimes else ''}.")
            connus = adoptes
        except Exception as exc:  # noqa: BLE001
            print(f"carnet illisible ({exc}) -- aucune adoption")
            connus = set()

    rapport = run_making(
        client,
        lambda: lire_univers(
            args.bankroll / max(args.max_markets, 1), args.improve
        ),
        bankroll=args.bankroll,
        minutes=args.minutes,
        interval_s=args.interval,
        max_markets=args.max_markets,
        armed=args.arm,
        adopted=connus,
        # Aucun ordre ne survit a la course qui l a pose, meme si la
        # machine s eteint avant le nettoyage.
        expiration=int(time.time() + args.minutes * 60) + 60,
    )

    print(f"\n{rapport.ticks} tour(s)")
    print(f"  ordres poses          : {rapport.placed}")
    print(f"  refuses (post_only)   : {rapport.refused}")
    print(f"  annules               : {rapport.cancelled}")
    print(f"  conserves en file     : {rapport.kept}")
    print(f"  ordres etrangers vus  : {rapport.foreign_seen}  (laisses intacts)")

    if rapport.stranded:
        # Dit fort : une position qu'on ne sait pas solder est une position
        # abandonnee, et c'est exactement ce qui a coute 10 $ le 2026-08-21.
        print(f"\n{len(rapport.stranded)} POSITION(S) NON SOLDABLE(S) :")
        for jeton in rapport.stranded:
            print(f"  {jeton[:24]}...")
        print("  Carnet illisible ou reliquat sous le minimum d'ordre.")

    # ECRIT AVANT TOUT AFFICHAGE : ce qui reste au carnet doit etre retrouvable
    # meme si la suite plante. `left_open` = le nettoyage final n'a pas abouti.
    try:
        with open(memoire, "w", encoding="utf-8") as f:
            json.dump(sorted(rapport.left_open), f)
    except OSError as exc:
        print(f"\nmemoire d'ordres NON ecrite ({exc}) -- la prochaine relance "
              f"verra ces ordres comme etrangers.")

    if rapport.blocked:
        # Une boucle qui voulait coter et n'a pas pu n'est pas une boucle qui
        # tourne bien. Dit fort, avant le bilan.
        print(f"\n{len(rapport.blocked)} ORDRE(S) BLOQUE(S) PAR UN ETRANGER :")
        for jeton, sens, prix, occupant in rapport.blocked:
            print(f"  {sens} {jeton[:16]}... voulu @ {prix:.3f} "
                  f"-- occupe par {occupant[:16]}")
        print("  Ni recote ni double : on ne pose pas sur une place qu'on ne")
        print("  peut pas liberer. Annuler l'ordre a la main pour debloquer.")

    if rapport.held_above:
        # Dit AVANT le bilan : ces ventes ne partiront pas tant que le carnet
        # n est pas remonte. C est voulu -- vendre sous son prix d achat est
        # ce qui a rendu le premier aller-retour perdant -- mais une position
        # tenue en silence redevient une position abandonnee.
        print(f"\n{len(rapport.held_above)} VENTE(S) TENUE(S) AU-DESSUS DU CARNET :")
        for jeton, prix, ask in rapport.held_above:
            print(
                f"  {jeton[:16]}...  cotee {prix:.3f}  (carnet {ask:.3f}) "
                f"-- sous le revient, perte non realisee"
            )

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
