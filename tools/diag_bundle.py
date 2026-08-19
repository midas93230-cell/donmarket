# -*- coding: utf-8 -*-
"""Diagnostic : quel champ de `place-order-bundle` est refuse ?

POURQUOI CET OUTIL. Chaque -3026 ne nomme qu'un parametre a la fois, et le
decouvrir par la boucle coute un tour complet. Ici chaque essai prend une
seconde, et la nature du refus suffit a conclure sans rien engager :

  - « Required request parameter X »  -> X etait necessaire, on le remet ;
  - « Your input param is invalid »   -> un champ present gene, on en retire ;
  - un ORDRE PASSE                    -> la combinaison est la bonne.

SECURITE. Le prix est volontairement pose LOIN sous le marche, de sorte qu'un
ordre qui partirait ne trouve pas preneur. Le montant est le minimum utile.
Et des qu'un ordre part, le script l'ANNULE immediatement puis s'arrete : on
cherche la forme de la requete, pas une position.

Lancement :
    .venv\\Scripts\\python tools\\diag_bundle.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, ".")

from donmarket.binance.api import BinancePredictionClient  # noqa: E402
from donmarket.binance.trade import to_base_units  # noqa: E402

logging.basicConfig(level=logging.ERROR)

NOTIONNEL = 2.0
# Trois quarts sous le meilleur bid : un achat pose la ne peut pas etre servi.
FACTEUR_PRIX = 0.25


async def main() -> int:
    async with BinancePredictionClient() as client:
        adresse = await client.wallet_address()
        identifiant = await client.wallet_id()
        compte = await client.funding_account_type()
        print(f"portefeuille {adresse[:8]}...{adresse[-4:]}  compte {compte}")

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
            print("aucun marche exploitable pour le diagnostic")
            return 1

        topic, marche, branche, _liq = choix
        carnet = await client.fetch_book(
            marche["marketId"], token_id=branche["tokenId"], vendor=topic["vendor"]
        )
        prix_pose = round(carnet.best_bid.price * FACTEUR_PRIX, 2)
        print(
            f"marche {marche['marketId']} branche {branche['name']} "
            f"bid {carnet.best_bid.price} -> on posera a {prix_pose} "
            "(injouable, donc non rempli)"
        )

        base = {
            "walletAddress": adresse,
            "walletId": identifiant,
            "marketId": marche["marketId"],
            "tokenId": branche["tokenId"],
            "side": "BUY",
            "orderType": "LIMIT",
            "priceLimit": f"{prix_pose:.2f}",
            "amountIn": to_base_units(NOTIONNEL),
            "slippageBps": topic.get("slippageBps") or 1000,
            "vendor": topic["vendor"],
        }

        devis = await client.post("/trade/get-quote", base)
        quote_id = (devis.get("data") or devis).get("quoteId")
        print(f"devis obtenu : {quote_id}\n")

        commun = {
            "walletAddress": adresse,
            "walletId": identifiant,
            "quoteId": quote_id,
            "timeInForce": "GTC",
            "accountType": compte,
            "orderType": "LIMIT",
            "slippageBps": base["slippageBps"],
        }

        essais = [
            ("les 7 champs reclames", commun),
            ("sans slippageBps", {k: v for k, v in commun.items() if k != "slippageBps"}),
            ("sans orderType", {k: v for k, v in commun.items() if k != "orderType"}),
            ("+ priceLimit", {**commun, "priceLimit": base["priceLimit"]}),
            ("+ vendor", {**commun, "vendor": topic["vendor"]}),
            ("+ protocol", {**commun, "protocol": "predictdotfun"}),
            ("+ fundingSource", {**commun, "fundingSource": "MPC"}),
            ("+ protocol + fundingSource",
             {**commun, "protocol": "predictdotfun", "fundingSource": "MPC"}),
            ("quoteId seul + portefeuille",
             {"walletAddress": adresse, "walletId": identifiant, "quoteId": quote_id}),
        ]

        for nom, params in essais:
            try:
                recu = await client.post("/trade/place-order-bundle", params)
            except Exception as exc:  # noqa: BLE001 — on classe le refus
                message = str(exc).split(" : ")[-1].strip()
                print(f"  {nom:<30} -> {message[:70]}")
                continue

            interne = recu.get("data") if isinstance(recu, dict) else None
            order_id = (interne or recu or {}).get("orderId")
            print(f"\n>>> ORDRE PASSE avec « {nom} » — orderId {order_id}")
            print(f"    champs : {sorted(params)}")
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
                    print(f"    ANNULATION REFUSEE : {exc} — a annuler dans l app")
            return 0

        print("\naucune combinaison acceptee — voir les messages ci-dessus")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
