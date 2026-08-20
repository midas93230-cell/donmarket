# -*- coding: utf-8 -*-
"""Combien de marches Polymarket sont cotables en teneur a petit capital ?

POURQUOI CETTE MESURE. La strategie construite pour Binance -- acheter au
meilleur bid, revendre au meilleur ask, sans frais -- n'a rien de propre a
Binance. Polymarket accepte les ordres limites sans discussion, et son moteur
d'execution existe deja dans ce depot.

Le 2026-08-18, Polymarket avait ete ecarte sur le mauvais critere : les
RECOMPENSES de liquidite exigent `rewardsMinSize` (~100 parts, soit ~100 $).
Capturer l'ECART n'a pas besoin de ca -- il faut franchir `orderMinSize`, qui
vaut souvent 5 parts. C'est un tout autre ticket d'entree.

CE QUE LA MESURE REND, et rien de plus : le nombre de branches dont l'ecart
depasse deux pas ET dont le ticket tient dans le capital. Un ecart AFFICHE
n'est pas un ecart OBTENU -- lecon du 2026-07-28 -- donc aucun rendement n'est
annonce ici.

Lecture seule : aucune cle, aucun ordre.
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, ".")

from donmarket.api import clob, gamma  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")

CAPITAL = 8.73
MIN_SPREAD_TICKS = 2
# Au-dela, ce n est plus un ecart mais un carnet beant : personne ne cote.
MAX_SPREAD_TICKS = 10
# Parts minimales presentes de chaque cote pour parler de contrepartie.
MIN_TAILLE = 20.0


async def main() -> int:
    print(f"capital de reference : {CAPITAL:.2f} $\n")
    marches = await gamma.fetch_all_markets()
    print(f"{len(marches)} marches lus")

    negociables = [m for m in marches if m.is_tradable]
    print(f"{len(negociables)} negociables (deux branches, carnet attendu)")

    token_ids = [tid for m in negociables for tid in m.token_ids]
    carnets = await clob.fetch_books(token_ids)
    print(f"{len(carnets)} carnets obtenus\n")

    cotables = []
    motifs = {"ecart serre": 0, "carnet beant": 0, "prix extreme": 0,
              "trop peu de profondeur": 0, "ticket trop gros": 0,
              "carnet incomplet": 0}

    for marche in negociables:
        pas = marche.min_tick_size or 0.01
        for token_id in marche.token_ids:
            carnet = carnets.get(token_id)
            if carnet is None or carnet.best_bid is None or carnet.best_ask is None:
                motifs["carnet incomplet"] += 1
                continue
            # Sur Polymarket, `best_bid`/`best_ask` sont deja des prix
            # (flottants), pas des paliers -- contrairement au modele Binance.
            bid = float(carnet.best_bid)
            ask = float(carnet.best_ask)
            taille_bid = (
                float(getattr(carnet.bids[-1], "size", 0) or 0) if carnet.bids else 0.0
            )
            taille_ask = (
                float(getattr(carnet.asks[-1], "size", 0) or 0) if carnet.asks else 0.0
            )
            ecart_pas = round((ask - bid) / pas) if pas else 0
            if ecart_pas < MIN_SPREAD_TICKS:
                motifs["ecart serre"] += 1
                continue
            # PIEGE DU 2026-07-28, et il faut le nommer : un ecart ENORME n'est
            # pas une aubaine, c'est un carnet VIDE. Bid 0,002 contre ask 0,565
            # affiche 28 000 % de gain brut et ne sera jamais servi. Sans ce
            # filtre la mesure en trouvait 1608 ; la quasi-totalite etait ce
            # mirage.
            if ecart_pas > MAX_SPREAD_TICKS:
                motifs["carnet beant"] += 1
                continue
            if not (0.10 <= bid <= 0.90):
                motifs["prix extreme"] += 1
                continue
            # De la taille DES DEUX COTES : sans contrepartie en face, on ne
            # peut ni etre rempli a l'achat ni ressortir a la vente.
            if min(taille_bid, taille_ask) < MIN_TAILLE:
                motifs["trop peu de profondeur"] += 1
                continue
            # Ticket : le minimum d'ordre, paye au prix ou l'on achete.
            ticket = max(marche.min_order_size, 1.0) * bid
            if ticket > CAPITAL:
                motifs["ticket trop gros"] += 1
                continue
            cotables.append((marche, token_id, bid, ask, ecart_pas, ticket))

    print("Ecartes :")
    for motif, n in motifs.items():
        print(f"  {motif:<20} {n}")

    print(f"\n>>> {len(cotables)} branche(s) cotables a {CAPITAL:.2f} $")
    if not cotables:
        return 0

    # Le meilleur gain brut d'abord : sans frais, il vaut l'ecart rapporte au
    # prix d'achat. BRUT -- il suppose les deux cotes remplis.
    cotables.sort(key=lambda c: (c[3] - c[2]) / c[2], reverse=True)
    print(f"\n{'bid':>6} {'ask':>6} {'pas':>4} {'ticket':>7} {'brut/AR':>8}  titre")
    for marche, _tid, bid, ask, pas_n, ticket in cotables[:15]:
        brut = (ask - bid) / bid
        titre = (marche.question or "")[:44]
        print(
            f"{bid:>6.3f} {ask:>6.3f} {pas_n:>4} {ticket:>7.2f} "
            f"{brut:>7.1%}  {titre}"
        )

    medians = sorted((c[3] - c[2]) / c[2] for c in cotables)
    print(
        f"\ngain brut median d'un aller-retour : "
        f"{medians[len(medians) // 2]:.1%}"
    )
    print(
        "\nBRUT veut dire : les DEUX cotes remplis. Le taux de remplissage\n"
        "n'est pas mesure ici, et c'est lui qui decide."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
