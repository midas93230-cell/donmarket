# -*- coding: utf-8 -*-
r"""Pose UN ordre sur un marche NOMME, avec les garde-fous de l'application.

    .venv\Scripts\python tools\poser_ordre.py --slug X --cote BUY --prix 0.13 --parts 46
    .venv\Scripts\python tools\poser_ordre.py --slug X --cote BUY --prix 0.13 --parts 46 --arm
    .venv\Scripts\python tools\poser_ordre.py --annuler 0xabc...

Sans `--arm`, RIEN n'est envoye : l'outil relit le carnet, applique les
controles et annonce ce qu'il poserait. C'est le mode par defaut, a dessein.

## Pourquoi ce fichier existe

`premier_ordre.py` choisit son marche lui-meme via `making.core.eligible`, qui
refuse tout prix sous 0,10 et travaille sur un notionnel fixe de 2 $. Il ne sait
donc pas poser un ordre sur un marche qu'on a choisi -- or nos deux seuls cycles
gagnants sont partis de marches choisis a la main apres mesure des impressions.

## Les controles, chacun paye une fois

1. CARNET RELU JUSTE AVANT L'ENVOI. Entre la selection et le clic, le carnet
   bouge : le 2026-08-26 un ecart de 0,43/0,58 s'est referme a un tick en dix
   minutes. On ne fait jamais confiance a un prix lu il y a une heure.

2. DEUX FOIS `orderMinSize`, DES DEUX COTES. Un remplissage partiel a 50 %
   laisse un reliquat SOUS le minimum, donc invendable. Paye deux fois : le
   2026-08-24 sur un achat, le 2026-08-26 sur une vente dont il est reste
   1,74 part bloquee sous un minimum de 5.

3. PRIX SUR LE TICK. Un prix hors tick est rejete par le CLOB, mais apres
   signature -- autant le voir avant.

4. TRAVERSEE DE L'ECART ANNONCEE, PAS INTERDITE. Notre bareme builder est
   maker 0 / taker 10 bps : un ordre preneur est le seul qui rapporte quelque
   chose. On le chiffre et on demande `--traverser` explicitement, plutot que
   de le refuser -- lecon du 2026-08-27, ou l'interdiction en dur rendait le
   revenu nul par construction.

5. VENTE : on ne vend pas ce qu'on ne detient pas, et on ne laisse jamais un
   reliquat sous le minimum derriere soi.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, ".")

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
MULTIPLE_MINIMUM = 2
TAUX_PRENEUR = 0.001


def pages(paginator):
    """Un `Paginator` itere des PAGES ; les lignes sont dans `.items`."""
    out = []
    for page in paginator:
        items = getattr(page, "items", None)
        out.extend(items if items is not None else [page])
    return out


def carnet(session, token_id):
    """Les carnets Polymarket arrivent PIRE PRIX EN PREMIER : le meilleur est
    en derniere position (mesure du 2026-07-26)."""
    r = session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
    r.raise_for_status()
    b = r.json()
    bids, asks = b.get("bids") or [], b.get("asks") or []
    return (
        (float(bids[-1]["price"]), float(bids[-1]["size"])) if bids else (None, 0.0),
        (float(asks[-1]["price"]), float(asks[-1]["size"])) if asks else (None, 0.0),
    )


def controler(cote, prix, parts, bid, ask, tick, mini, detenu, traverser):
    """Rend (refus, avertissements). Un refus bloque, un avertissement informe."""
    refus, avis = [], []

    if not (0 < prix < 1):
        refus.append(f"Le prix doit tenir strictement entre 0 et 1 (recu {prix}).")
    elif abs(prix / tick - round(prix / tick)) > 1e-6:
        refus.append(f"Le prix {prix} n'est pas un multiple du tick {tick}.")

    if parts < MULTIPLE_MINIMUM * mini:
        refus.append(
            f"Engager au moins {MULTIPLE_MINIMUM * mini:.0f} parts "
            f"(2 x le minimum de {mini:.0f}) : sinon un remplissage a 50 % "
            f"laisse un reliquat sous le minimum, donc invendable."
        )

    if cote == "SELL":
        if detenu <= 0:
            refus.append("Aucune part detenue sur ce marche.")
        elif parts > detenu:
            refus.append(f"Vente de {parts} parts pour {detenu:.2f} detenues.")
        else:
            reste = detenu - parts
            if 0 < reste < mini:
                refus.append(
                    f"Cette vente laisserait {reste:.2f} parts derriere elle, "
                    f"sous le minimum de {mini:.0f} : elles seraient invendables. "
                    f"Vendre tout ({detenu:.2f}) ou au plus {detenu - mini:.2f}."
                )
        if ask is not None and prix > ask:
            refus.append(
                f"Vente a {prix} au-dessus du meilleur ask ({ask}) : hors marche, "
                f"elle ne peut pas se remplir."
            )

    preneur = (cote == "BUY" and ask is not None and prix >= ask) or (
        cote == "SELL" and bid is not None and prix <= bid
    )
    if preneur:
        cout = parts * prix * TAUX_PRENEUR
        message = (
            f"Cet ordre TRAVERSE L'ECART : il se remplit tout de suite et paie "
            f"les frais de preneur, {cout:.4f} $ sur {parts * prix:.2f} $ engages."
        )
        if traverser:
            avis.append(message)
        else:
            refus.append(message + " Ajouter --traverser si c'est voulu.")

    return refus, avis


def main() -> int:
    from dotenv import load_dotenv

    p = argparse.ArgumentParser()
    p.add_argument("--slug")
    p.add_argument("--cote", choices=("BUY", "SELL"))
    p.add_argument("--prix", type=float)
    p.add_argument("--parts", type=float)
    p.add_argument("--issue", type=int, default=0, help="0 = premiere (Yes), 1 = seconde")
    p.add_argument("--traverser", action="store_true",
                   help="autorise un ordre preneur, dont le cout est affiche")
    p.add_argument("--annuler", metavar="ORDER_ID", help="annule un ordre existant")
    p.add_argument("--arm", action="store_true", help="envoie reellement")
    args = p.parse_args()

    load_dotenv(".env", override=True)
    import httpx
    from polymarket import SecureClient

    from donmarket.builder.attribution import order_attribution
    from donmarket.store import vault

    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )
    session = httpx.Client()

    # ------------------------------------------------------------ annulation
    if args.annuler:
        ouverts = {o.id: o for o in pages(client.list_open_orders())}
        o = ouverts.get(args.annuler)
        if o is None:
            print(f"Ordre {args.annuler} introuvable parmi les ordres ouverts.")
            print("Ordres ouverts :")
            for i, v in ouverts.items():
                print(f"  {i}  {v.side} {v.original_size} @ {v.price} "
                      f"(rempli {v.size_matched})")
            return 1
        rempli = float(o.size_matched)
        total = float(o.original_size)
        reste = total - rempli
        print(f"Annulation : {o.side} {total} @ {o.price}, rempli {rempli}")
        if o.side == "SELL" and 0 < reste < 5:
            print(f"  ATTENTION : {reste:.2f} parts resteraient invendues et "
                  f"probablement sous le minimum d'ordre.")
        if not args.arm:
            print("\nLECTURE SEULE -- ajouter --arm pour annuler reellement.")
            return 0
        print("  ->", client.cancel_order(order_id=args.annuler))
        return 0

    if not (args.slug and args.cote and args.prix and args.parts):
        p.error("--slug, --cote, --prix et --parts sont requis (ou --annuler)")

    # -------------------------------------------------------------- le marche
    marches = session.get(GAMMA, params={"slug": args.slug}, timeout=20).json()
    if not marches:
        print(f"Slug introuvable : {args.slug}")
        return 1
    m = marches[0]
    token = json.loads(m["clobTokenIds"])[args.issue]
    tick = float(m.get("orderPriceMinTickSize") or 0.01)
    mini = float(m.get("orderMinSize") or 5)

    (bid, taille_bid), (ask, taille_ask) = carnet(session, token)
    detenu = sum(float(pos.size) for pos in pages(client.list_positions())
                 if str(pos.token_id) == str(token))

    print(f"{m.get('question', '')[:70]}")
    print(f"  carnet {bid} ({taille_bid:,.0f}) / {ask} ({taille_ask:,.0f})"
          f" · tick {tick} · minimum {mini:.0f} · detenu {detenu:.2f}")
    print(f"  demande : {args.cote} {args.parts:.0f} @ {args.prix} "
          f"= {args.parts * args.prix:.2f} $")

    refus, avis = controler(args.cote, args.prix, args.parts, bid, ask,
                            tick, mini, detenu, args.traverser)
    for a in avis:
        print(f"  ATTENTION — {a}")
    if refus:
        for r in refus:
            print(f"  REFUSE — {r}")
        return 1

    preneur = (args.cote == "BUY" and ask is not None and args.prix >= ask) or (
        args.cote == "SELL" and bid is not None and args.prix <= bid)

    # L'ATTRIBUTION S'ANNONCE AVANT LA SORTIE EN LECTURE SEULE. Placee apres,
    # elle n'etait visible qu'en engageant de l'argent -- donc invérifiable
    # sans risque, ce qui est exactement le defaut qu'on vient de corriger.
    # `order_attribution()` et non `os.getenv` : la lecture brute laissait
    # partir un code MALFORME, que le CLOB accepte sans broncher et qui
    # rend une page /builder/trades vide -- indistinguable d'un compte
    # sans volume. Un seul endroit lit le code, desormais.
    attribution = order_attribution()
    print(f"\nattribution : {attribution.phrase}")

    if not args.arm:
        print("LECTURE SEULE -- aucun ordre envoye. Ajouter --arm pour poser.")
        return 0

    # L'ATTRIBUTION SE JOUE ICI, PAS APRES. Le SDK expose `builder_code` sur
    # `place_limit_order` ; ne pas le passer, c'est renoncer definitivement aux
    # frais de cet ordre -- ils ne se reclament pas retroactivement.
    #
    # POURQUOI CE N'ETAIT PAS BRANCHE, parce que la raison compte plus que le
    # correctif : `donmarket/builder/attribution.py` affirmait que le code
    # etait « un IDENTIFIANT DE LECTURE » et que « le poser dans une requete
    # n'attribue rien du tout ». Cette croyance a fait qu'on ne l'a jamais
    # passe -- et le compteur de revenus builder a lu ZERO pendant deux
    # semaines, qu'on interpretait comme « pas de volume » au lieu de
    # « pas d'attribution ». Edoardo (Polymarket) a pose la question le
    # 2026-09-01 ; c'est en cherchant a repondre qu'on l'a vu. La phrase
    # fautive est corrigee, et le code ne se lit plus qu'a un seul endroit.
    try:
        r = client.place_limit_order(
            token_id=token, price=args.prix, size=args.parts,
            side=args.cote, post_only=not preneur,
            builder_code=attribution.code,
        )
        print(f"  POSE : {r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  REFUSE PAR LE CLOB : {str(exc)[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
