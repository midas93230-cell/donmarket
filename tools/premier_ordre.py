# -*- coding: utf-8 -*-
"""Pose UN ordre teneur sur Polymarket. Le test qui tranche.

POURQUOI IL FAUT UN ORDRE REEL. Le CLOB rend `balance: 0` alors que le proxy
detient bien 8,008 pUSD avec des autorisations illimitees, verifie on-chain.
La cause est connue : `py-clob-client` 0.34.6 interroge l'EOA au lieu du
funder. Mais c'est une LECTURE. A la signature, c'est le proxy qui est designe
comme donneur d'ordre, et le CLOB verifie son solde a lui. Savoir si cette
lecture fausse empeche vraiment de trader demande de poser un ordre : rien
d'autre ne repond.

CE QUE LE SCRIPT CHOISIT. La meme logique que `donmarket/making/core.py` :
ecart d'au moins deux pas mais pas beant, prix loin des extremes, de la
profondeur DES DEUX COTES. Un ecart enorme n'est pas une aubaine, c'est un
carnet vide -- lecon du 2026-07-28, reconfirmee le 2026-08-20 ou ce filtre
faisait passer 1608 branches « cotables » a 351.

SECURITE. Un seul ordre, au meilleur bid, taille minimale. Il REJOINT la file
au lieu de traverser l'ecart, donc il ne peut pas etre rempli instantanement.
Son identifiant est affiche pour pouvoir l'annuler.

    .venv\\Scripts\\python tools\\premier_ordre.py          (lecture seule)
    .venv\\Scripts\\python tools\\premier_ordre.py --arm    (pose l'ordre)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, ".")

NOTIONNEL = 2.0
MIN_PARTS = 5.0


async def choisir():
    from donmarket.api import clob, gamma
    from donmarket.making.core import eligible

    marches = await gamma.fetch_all_markets()
    negociables = [m for m in marches if m.is_tradable]
    jetons = [t for m in negociables for t in m.token_ids]
    carnets = await clob.fetch_books(jetons)
    rungs, _rejets = eligible(negociables, carnets, capital_usd=NOTIONNEL)
    return rungs


def main() -> int:
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env", override=True)

    rungs = asyncio.run(choisir())
    if not rungs:
        print("Aucune branche cotable. Rien a poser.")
        return 1

    choix = None
    for rung in rungs:
        parts = float(int(NOTIONNEL / rung.buy_price))
        if parts >= MIN_PARTS:
            choix = (rung, parts)
            break
    if choix is None:
        print("Aucune branche ou 5 parts tiennent dans le notionnel.")
        return 1
    rung, parts = choix

    print(f"marche  : {rung.question[:60]}")
    print(f"branche : {rung.token_id[:16]}...")
    print(
        f"ordre   : ACHAT {parts:.0f} parts @ {rung.buy_price:.3f} "
        f"= {parts * rung.buy_price:.2f} $"
    )
    print(
        f"revente visee a {rung.sell_price:.3f} -> "
        f"gain brut {rung.gross_edge:.1%} l'aller-retour"
    )

    if not args.arm:
        print("\nLECTURE SEULE -- rien n'a ete envoye.")
        return 0

    # NOUVEAU SDK, 2026-08-20. `py-clob-client` est ARCHIVE et son depot le dit
    # sans detour : « no longer functional, should not be used ». Le CLOB le
    # confirme en rendant « invalid order version, please use the latest
    # clob-client ». Le remplacant unifie est `polymarket-client`.
    #
    # `wallet` designe le PROXY qui detient le pUSD ; la cle ne fait que signer
    # pour lui. Les confondre fait accepter l'ordre puis le rejeter pour solde
    # insuffisant, en pointant une adresse vide.
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )

    print("\nenvoi...")
    try:
        reponse = client.place_limit_order(
            token_id=rung.token_id,
            price=rung.buy_price,
            size=parts,
            side="BUY",
            # post_only : l'ordre est REFUSE plutot que de traverser l'ecart.
            # Un teneur qui traverse devient preneur et paie les frais au lieu
            # de les eviter -- exactement ce qu'on cherche a ne pas faire.
            post_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSE : {str(exc)[:400]}")
        return 1

    print(f"REPONSE : {reponse}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
