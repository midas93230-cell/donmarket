# -*- coding: utf-8 -*-
r"""Cherche le montage qui a REELLEMENT produit nos deux cycles gagnants.

    .venv\Scripts\python tools\chercher_cycle.py
    .venv\Scripts\python tools\chercher_cycle.py --large    # criteres relaches

LECTURE SEULE. Cet outil ne pose aucun ordre : il rend une liste et les
parametres exacts a saisir. Le geste d'engager de l'argent reste a l'humain.

## Ce que la mesure a retenu des deux gagnants

Russie   achat 35 @ 0,07 -> vente 35 @ 0,08   +14,3 % en 19 h
BlueJays achat 20 @ 0,14 -> vente 20 @ 0,16   +14,3 % en 22 h

Les deux rendent EXACTEMENT `tick / prix`. C'est la seule formule qui compte :
un tick de 0,01 a 0,07 rend 14,3 %, le meme tick a 0,50 rend 2 %, et un tick de
0,001 a 0,07 ne rend que 1,4 %. D'ou `TICK_MIN = 0.01` -- un marche a tick fin
est structurellement sans interet pour un petit compte, quel que soit son ecart.

## Trois pieges qui ont chacun coute quelque chose

1. PRIX BAS N'EST PAS PRIX MINUSCULE. Un premier ecran optimisant `tick/prix`
   a remonte des marches a 0,001-0,01 : des queues de distribution expirant
   sous 48 h. Si l'achat se remplit et que la vente ne part pas, on tient un
   billet de loterie. D'ou `PRIX_MIN` et `JOURS_MIN`.

2. L'ECART LARGE N'EST PAS L'OCCASION. Nos deux gagnants avaient UN SEUL tick
   d'ecart : on achete au bid, on vend a l'ask. Exiger un ecart large ecarte
   precisement les bons carnets et retient les marches sans contrepartie
   (mesure du 28/08 : les ecarts larges sont des matchs deja joues).

3. LE VOLUME AFFICHE NE DIT PAS QU'ON SERA SERVI. Seul le TEST DES IMPRESSIONS
   le dit : ou tombent les transactions recentes ? Le 29/08, trois carnets a
   fort volume avaient 68 a 94 impressions sur 100 AU-DESSUS de l'ask, avec
   15 000 a 21 000 parts en file au bid -- un achat n'y serait jamais servi.
   Nos gagnants avaient ~30 impressions au bid et ~63 dans l'ecart.

## Et le piege qui reste ouvert

Un carnet ou 79 % des impressions frappent le bid REMPLIRA l'achat -- parce que
le prix s'effondre. C'est la selection adverse notee le 2026-08-21 : etre
rempli signifie que quelqu'un a voulu vendre a notre prix. D'ou `AU_BID_MAX`.
On cherche un flux a DEUX SENS, pas un vendeur unilateral.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
SANTE = "docs/health.json"

TICK_MIN = 0.01        # sous ce tick, la capture relative est negligeable
PRIX_MIN, PRIX_MAX = 0.05, 0.20
VOLUME_MIN = 20_000
JOURS_MIN = 14         # du temps devant si la sortie ne part pas
AU_BID_MIN = 20        # sinon l'achat n'est jamais servi
AU_BID_MAX = 60        # au-dela, on est servi parce que le prix s'effondre
DESSUS_MAX = 55        # flux acheteur unilateral : terrain de vendeur
FILE_MAX = 3_000       # parts deja en file au bid, devant nous


def lignes(paginator, limite):
    out = []
    for page in paginator:
        items = getattr(page, "items", None)
        out.extend(items if items is not None else [page])
        if len(out) >= limite:
            break
    return out[:limite]


def impressions(client, condition_id, bid, ask):
    """Ou tombent les 100 dernieres transactions, ramenees a la face YES."""
    sous = dans = dessus = 0
    for t in lignes(client.list_trades(market=condition_id, page_size=100), 100):
        p = float(getattr(t, "price", 0) or 0)
        if getattr(t, "outcome_index", 0) == 1:
            p = 1 - p
        if p <= bid:
            sous += 1
        elif p >= ask:
            dessus += 1
        else:
            dans += 1
    return sous, dans, dessus


def main() -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--large", action="store_true",
                        help="relache les seuils d'impressions (exploration)")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    import httpx
    from polymarket import PublicClient

    client, session = PublicClient(), httpx.Client(timeout=25)
    maintenant = datetime.now(timezone.utc)

    au_bid_min = 10 if args.large else AU_BID_MIN
    dessus_max = 75 if args.large else DESSUS_MAX
    file_max = 20_000 if args.large else FILE_MAX

    with open(SANTE, encoding="utf-8") as f:
        carnets = json.load(f)

    pre = [
        l for l in carnets
        if l["verdict"] in ("tradable", "efficient")
        and l.get("bid") and l.get("ask")
        and (l.get("tick") or 0.01) >= TICK_MIN
        and PRIX_MIN <= l["bid"] <= PRIX_MAX
        and l["volume24h"] >= VOLUME_MIN
    ]
    pre.sort(key=lambda l: -(l.get("tick") or 0.01) / l["bid"])
    print(f"{len(pre)} carnets passent le filtre de prix et de tick "
          f"(sur {len(carnets)} mesures)\n")

    retenus = []
    for l in pre[:25]:
        try:
            marches = session.get(GAMMA, params={"slug": l["slug"]}).json()
            if not marches:
                continue
            m = marches[0]
            fin = m.get("endDate") or ""
            jours = ((datetime.fromisoformat(fin.replace("Z", "+00:00")) - maintenant).days
                     if fin else 0)
            if jours < JOURS_MIN:
                continue

            token = json.loads(m["clobTokenIds"])[0]
            livre = session.get(f"{CLOB}/book", params={"token_id": token}).json()
            bids, asks = livre.get("bids") or [], livre.get("asks") or []
            if not bids or not asks:
                continue
            bid, ask = float(bids[-1]["price"]), float(asks[-1]["price"])
            file_bid = float(bids[-1]["size"])

            sous, dans, dessus = impressions(client, m["conditionId"], bid, ask)
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            mini = float(m.get("orderMinSize") or 5)

            if not (au_bid_min <= sous <= AU_BID_MAX):
                continue
            if dessus > dessus_max or file_bid > file_max:
                continue

            retenus.append({
                "nom": l["question"][:46], "slug": l["slug"],
                "bid": bid, "ask": ask, "tick": tick, "mini": mini,
                "gain": 100 * tick / bid, "jours": jours, "file": file_bid,
                "sous": sous, "dans": dans, "dessus": dessus,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"  {l['question'][:34]} : illisible ({str(exc)[:40]})")
        time.sleep(0.25)

    if not retenus:
        print("AUCUN carnet ne presente le montage des deux cycles gagnants.\n"
              "Ce n'est pas un echec de l'outil : le montage est rare. Forcer un\n"
              "ordre sur un carnet qui ne le presente pas, c'est exactement la\n"
              "faute du 2026-08-21. Relancer demain, ou --large pour explorer.")
        return 0

    retenus.sort(key=lambda r: -r["gain"])
    print(f"{len(retenus)} carnet(s) au profil des gagnants :\n")
    for r in retenus:
        parts = max(2 * r["mini"], round(6.0 / r["bid"]))
        print(f"  {r['nom']}")
        print(f"    carnet {r['bid']:.2f}/{r['ask']:.2f} · tick {r['tick']} · "
              f"gain d'un tick {r['gain']:.1f} % · echeance {r['jours']} j")
        print(f"    impressions : {r['sous']} au bid · {r['dans']} dans l'ecart · "
              f"{r['dessus']} au-dessus · file au bid {r['file']:,.0f} parts")
        print(f"    ACHAT propose : {parts:.0f} parts @ {r['bid']:.2f} "
              f"= {parts * r['bid']:.2f} $   (sortie visee {r['ask']:.2f})")
        print(f"    slug : {r['slug']}\n")
    print("Aucun ordre n'a ete pose. Pour armer la sortie apres remplissage,\n"
          "ajouter le slug a SURVEILLES dans tools/veiller_sorties.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
