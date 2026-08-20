# -*- coding: utf-8 -*-
"""Diagnostic n2 : quelle VALEUR est refusee, et le devis expire-t-il ?

Le diagnostic n1 a montre que les sept champs sont tous requis -- en retirer un
rend « Required parameter » -- mais qu'ensemble ils rendent « invalid ». Ce
n'est donc pas un champ absent, c'est une valeur.

DEUX HYPOTHESES, testees ensemble :

1. `timeInForce` ou `accountType` n'ont pas la forme attendue. On balaie les
   valeurs plausibles au lieu d'en supposer une.

2. LE DEVIS EXPIRE. Un devis releve le 2026-08-19 portait `timestamp`
   1787094431895 et `expireAt` 1787094435768 : moins de QUATRE SECONDES de
   validite. Le diagnostic n1 reutilisait un seul devis pour ses neuf essais --
   les huit derniers ne pouvaient donc qu'echouer, quelle que soit leur forme.
   Ici chaque essai demande un devis NEUF juste avant.

SECURITE. Prix au quart du meilleur bid : injouable, donc non rempli. Des
qu'une combinaison passe, l'ordre est annule et le script s'arrete.

Lancement :
    .venv\\Scripts\\python tools\\diag_valeurs.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

sys.path.insert(0, ".")

from donmarket.binance.api import BinancePredictionClient  # noqa: E402
from donmarket.binance.trade import to_base_units  # noqa: E402

logging.basicConfig(level=logging.ERROR)

NOTIONNEL = 2.0
FACTEUR_PRIX = 0.25

TIME_IN_FORCE = ["GTC", "IOC", "FOK", "GTD", "GOOD_TILL_CANCEL", "GTT"]


async def main() -> int:
    async with BinancePredictionClient() as client:
        adresse = await client.wallet_address()
        identifiant = await client.wallet_id()

        soldes = await client.payment_option_balances()
        comptes = [
            str(s.get("accountType"))
            for s in soldes
            if s.get("enabled") and s.get("accountType")
        ]
        print(f"comptes de paiement actifs : {comptes}")

        rep = await client._request("GET", "/market/list", {"limit": 40, "offset": 0})
        choix = None
        for topic in rep["marketTopics"]:
            for marche in topic["markets"]:
                for branche in marche["outcomes"]:
                    prix = float(branche.get("price") or 0)
                    if 0.3 < prix < 0.7:
                        liq = float(marche.get("liquidity") or 0)
                        if choix is None or liq > choix[3]:
                            choix = (topic, marche, branche, liq)
        if choix is None:
            print("aucun marche exploitable")
            return 1

        topic, marche, branche, _ = choix
        carnet = await client.fetch_book(
            marche["marketId"], token_id=branche["tokenId"], vendor=topic["vendor"]
        )
        prix_pose = round(carnet.best_bid.price * FACTEUR_PRIX, 2)
        slippage = topic.get("slippageBps") or 1000
        print(
            f"marche {marche['marketId']} branche {branche['name']} "
            f"bid {carnet.best_bid.price} -> pose a {prix_pose}"
        )

        params_devis = {
            "walletAddress": adresse,
            "walletId": identifiant,
            "marketId": marche["marketId"],
            "tokenId": branche["tokenId"],
            "side": "BUY",
            "orderType": "LIMIT",
            "priceLimit": f"{prix_pose:.2f}",
            "amountIn": to_base_units(NOTIONNEL),
            "slippageBps": slippage,
            "vendor": topic["vendor"],
        }

        # Duree de vie reelle d'un devis, mesuree une fois.
        sonde = await client.post("/trade/get-quote", params_devis)
        interne = sonde.get("data") if isinstance(sonde.get("data"), dict) else sonde
        debut, fin = interne.get("timestamp"), interne.get("expireAt")
        if debut and fin:
            print(f"validite du devis : {(int(fin) - int(debut)) / 1000:.1f} s\n")

        essais = [(tif, compte) for compte in comptes for tif in TIME_IN_FORCE]

        for time_in_force, compte in essais:
            devis = await client.post("/trade/get-quote", params_devis)
            interne = devis.get("data") if isinstance(devis.get("data"), dict) else devis
            quote_id = interne.get("quoteId")

            params = {
                "walletAddress": adresse,
                "walletId": identifiant,
                "quoteId": quote_id,
                "timeInForce": time_in_force,
                "accountType": compte,
                "orderType": "LIMIT",
                "slippageBps": slippage,
            }
            etiquette = f"{time_in_force} / {compte}"
            depart = time.monotonic()
            try:
                recu = await client.post("/trade/place-order-bundle", params)
            except Exception as exc:  # noqa: BLE001
                message = str(exc).split(" : ")[-1].strip()
                delai = time.monotonic() - depart
                print(f"  {etiquette:<28} ({delai:.1f}s) -> {message[:60]}")
                continue

            interne = recu.get("data") if isinstance(recu, dict) else None
            order_id = (interne or recu or {}).get("orderId")
            print(f"\n>>> ORDRE PASSE — {etiquette} — orderId {order_id}")
            if order_id:
                try:
                    await client.post(
                        "/trade/batch-cancel",
                        {
                            "walletAddress": adresse,
                            "walletId": identifiant,
                            "cancelInfoList[0].orderId": str(order_id),
                        },
                    )
                    print("    annule.")
                except Exception as exc:  # noqa: BLE001
                    print(f"    ANNULATION REFUSEE : {exc}")
            return 0

        print("\naucune valeur acceptee")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
