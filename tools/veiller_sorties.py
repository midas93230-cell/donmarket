# -*- coding: utf-8 -*-
r"""Pose la VENTE des qu'un achat se remplit, sur des marches nommes.

    .venv\Scripts\python tools\veiller_sorties.py --minutes 60
    .venv\Scripts\python tools\veiller_sorties.py --minutes 60 --arm

Sans `--arm`, rien n'est envoye : le veilleur lit et annonce ce qu'il poserait.

## Pourquoi cet outil existe

Le 2026-08-24, une position de 2,15 $ est devenue INVENDABLE : l'achat s'etait
rempli a 4,9943 parts sur un `orderMinSize` de 5, et toute vente etait refusee.
Trois defauts s'etaient empiles, dont celui-ci : *rien ne posait la vente quand
l'achat se remplissait*. Une position achetee sans sortie armee n'est pas une
position, c'est un pari sur la resolution.

La boucle generique `tenir_marche.py` choisit ses propres marches via
`making/core.eligible`, qui refuse tout prix sous `MIN_PRICE = 0.10`. Or la
capture d'un teneur vaut `capital x tick / prix` : plus le prix est BAS, plus
un tick rapporte en relatif. Ce filtre rend donc la boucle aveugle a ses
meilleurs terrains quand le capital est petit. Ce veilleur-ci ne choisit rien :
il surveille les positions qu'on lui nomme.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, ".")

# Importe en tete, malgre l'habitude d'imports paresseux dans ces outils :
# `attribution` ne tire que `os`, `dataclasses` et `re`. Le SDK de signature,
# lui, reste paresseux a l'interieur du module.
from donmarket.builder.attribution import order_attribution  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("veille")

CLOB = "https://clob.polymarket.com"

# Les marches surveilles, choisis a la main le 2026-08-26 apres mesure des
# impressions reelles (63 % du flux tombe DANS l'ecart pour le premier).
SURVEILLES = {
    "will-russia-invade-another-country-in-2026": "Russie envahit un pays en 2026",
    "will-the-toronto-blue-jays-clinch-a-spot-in-the-2026-mlb-postseason": "Blue Jays playoffs",
    # Ajoute le 2026-08-29 : seul carnet du relevé au profil des deux cycles
    # gagnants (tick 0,01 a 0,13 = 7,7 % par tick, 124 jours d'echeance,
    # 27 impressions sur 100 au bid). Reserve : 12 235 parts en file au bid.
    "clarity-act-signed-into-law-in-2026": "Clarity Act",
}


def pages(paginator):
    """Un `Paginator` itere des PAGES ; chaque page porte ses lignes dans
    `.items`. Iterer naivement rend des objets Page, pas des ordres."""
    out = []
    for page in paginator:
        out.extend(page if isinstance(page, (list, tuple)) else getattr(page, "items", [page]))
    return out


def carnet(session, token_id: str):
    r = session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
    r.raise_for_status()
    b = r.json()
    bids, asks = b.get("bids") or [], b.get("asks") or []
    # Les carnets Polymarket arrivent PIRE PRIX EN PREMIER : le meilleur est en
    # derniere position (mesure du 2026-07-26).
    return (float(bids[-1]["price"]) if bids else None,
            float(asks[-1]["price"]) if asks else None)


def une_passe(client, session, marches, armer: bool) -> None:
    # UNE SEULE LECTURE PAR PASSE, en tete : l'ancienne version appelait
    # `os.getenv` deux fois par ordre, une fois pour l'envoyer et une fois pour
    # dire s'il avait ete envoye. Deux lectures d'une meme valeur peuvent
    # diverger, et c'est precisement la ligne de journal qui aurait menti.
    attribution = order_attribution()
    if not attribution.is_attributed:
        # En WARNING et non en DEBUG : cette boucle vend des positions REELLES.
        # Chaque passe muette est du volume perdu sans reclamation possible.
        logger.warning("attribution %s", attribution.phrase)

    positions = {str(p.token_id): p for p in pages(client.list_positions())}
    ordres = pages(client.list_open_orders())
    vend_deja = {str(o.token_id) for o in ordres if o.side == "SELL"}

    for token_id, (nom, tick, mini) in marches.items():
        pos = positions.get(token_id)
        detenu = float(pos.size) if pos else 0.0
        if detenu <= 0:
            logger.info("%s : rien de detenu, achat pas encore rempli", nom)
            continue
        if token_id in vend_deja:
            logger.info("%s : %.2f parts detenues, vente deja au carnet", nom, detenu)
            continue
        if detenu < mini:
            # LE PIEGE DU 24/08, annonce en clair plutot qu'avale par un except.
            # Une sortie qu'on croit posee est pire qu'une sortie qu'on sait
            # impossible.
            logger.error("%s : %.4f parts DETENUES < minimum %.0f -- INVENDABLE. "
                         "Seule issue : completer l'achat ou attendre la resolution.",
                         nom, detenu, mini)
            continue
        bid, ask = carnet(session, token_id)
        if bid is None or ask is None:
            logger.warning("%s : carnet illisible", nom)
            continue
        prix = round(ask - tick, 4)
        if prix <= bid:
            # Vendre sous le bid, c'est traverser l'ecart et payer les frais de
            # preneur. On prefere rester au meilleur ask existant.
            prix = ask
        logger.info("%s : %.2f parts detenues, carnet %.3f/%.3f -> VENTE a %.3f (%.2f $)",
                    nom, detenu, bid, ask, prix, detenu * prix)
        if not armer:
            continue
        try:
            # SANS `builder_code`, LES FRAIS DE CET ORDRE SONT PERDUS POUR
            # TOUJOURS -- l'attribution se joue a la signature et ne se reclame
            # pas apres coup. Les deux sorties gagnantes des 28 et 29 aout sont
            # parties d'ici, sans attribution, parce que
            # `donmarket/builder/attribution.py` affirmait que joindre le code a
            # une requete « n'attribue rien du tout ». Le SDK l'expose pourtant
            # sur `place_limit_order`. Trouve le 2026-09-01 en repondant a la
            # question d'Edoardo (Polymarket). Le code vient maintenant de
            # `order_attribution()`, qui le VALIDE : `os.getenv` laissait passer
            # une faute de frappe que le CLOB accepte sans rien dire.
            r = client.place_limit_order(token_id=token_id, price=prix,
                                         size=detenu, side="SELL",
                                         post_only=True,
                                         builder_code=attribution.code)
            logger.info("  pose : %s (attribution : %s)", r, attribution.phrase)
        except Exception as exc:  # noqa: BLE001
            logger.error("  REFUSE : %s", str(exc)[:300])


def main() -> int:
    import httpx
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=45.0)
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    session = httpx.Client()

    # Resoudre les slugs une seule fois : token, tick et minimum ne bougent pas.
    marches = {}
    for slug, nom in SURVEILLES.items():
        m = session.get("https://gamma-api.polymarket.com/markets",
                        params={"slug": slug}, timeout=20).json()
        if not m:
            logger.warning("slug introuvable : %s", slug)
            continue
        m = m[0]
        token_id = json.loads(m["clobTokenIds"])[0]
        marches[token_id] = (nom,
                             float(m.get("orderPriceMinTickSize") or 0.01),
                             float(m.get("orderMinSize") or 5))
        logger.info("surveille %s (tick %s, minimum %s)", nom,
                    m.get("orderPriceMinTickSize"), m.get("orderMinSize"))

    if not marches:
        logger.error("aucun marche a surveiller")
        return 1
    if not args.arm:
        logger.warning("LECTURE SEULE -- aucune vente ne sera envoyee (ajouter --arm)")

    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )
    fin = time.time() + args.minutes * 60
    while time.time() < fin:
        try:
            une_passe(client, session, marches, args.arm)
        except Exception as exc:  # noqa: BLE001
            logger.error("passe echouee : %s", str(exc)[:200])
        time.sleep(args.interval)
    logger.info("fin de la veille")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
