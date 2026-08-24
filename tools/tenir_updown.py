# -*- coding: utf-8 -*-
"""Capture d'ecart sur les marches crypto « Up or Down », avec sortie forcee.

    .venv/Scripts/python tools/tenir_updown.py --bankroll 2
    .venv/Scripts/python tools/tenir_updown.py --bankroll 2 --arm

Sans `--arm`, rien ne part : l'outil decouvre, chiffre et affiche son plan.

## Pourquoi un outil separe de `tenir_marche`

`tenir_marche` ecarte tout marche a moins de six heures de sa resolution
(`making/core.MIN_HOURS_TO_RESOLUTION`). Les Up/Down se resolvent dans la
journee : ils etaient donc EXCLUS PAR CONSTRUCTION, et n'ont jamais ete
regardes. C'est l'utilisateur qui a demande qu'on aille voir.

## Ce que la mesure du 2026-08-23 a montre

Sur « Ethereum Up or Down on August 23 », a deux heures de la cloture :
bid 0,108 / ask 0,130 -- **2,2 cents d'ecart sur un prix de 0,108, soit ~20 %
brut par aller-retour**, avec 27 et 28 parts de profondeur DES DEUX COTES.
`orderMinSize` y vaut **5 parts**, contre 20 sur les marches a recompenses :
le ticket tombe a ~0,54 $.

Ce n'est pas le piege du carnet beant du 28/07 : la profondeur est reelle des
deux cotes et le prix est dans [0,10 ; 0,90]. Volume 24 h : 81 000 $ pour
Ethereum, 284 000 $ pour Bitcoin. Le probleme numero un de notre tenue de
marche -- quatorze heures au carnet sans un seul remplissage -- n'existe pas
ici.

## LE GARDE-FOU CENTRAL : la sortie forcee

Un marche qui se resout transforme toute position non soldee en pari : elle ne
vaut plus un prix, elle vaut 0 ou 1. A 0,108, c'est environ neuf chances sur
dix de tout perdre. C'est exactement le mecanisme qui a fait tomber QUATRE
positions a zero le 21/08 et coute 10 $ sur 16.

Cet outil refuse donc d'entrer trop pres de la cloture (`--marge-entree`), et
il LIQUIDE ce qu'il detient a l'approche de l'echeance (`--marge-sortie`), en
traversant l'ecart s'il le faut. Perdre l'ecart est un cout ; garder la
position est un pari. Les deux ne se comparent pas.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("updown")

GAMMA = "https://gamma-api.polymarket.com/markets"

# Ecart minimal, en fraction du prix, pour qu'un aller-retour vaille la peine.
# Sous ce seuil le moindre decalage du carnet transforme le gain en perte --
# meme raison que `MIN_SPREAD_TICKS` dans `making/core`.
ECART_MIN_RELATIF = 0.08

# Parts presentes de chaque cote pour qu'on parle de contrepartie.
# Volontairement bas : `orderMinSize` vaut 5 sur ces marches. On ne cherche pas
# un carnet epais, seulement a eliminer ceux ou personne ne repondra.
PROFONDEUR_MIN = 15.0

# Jours a sonder en avant. Les cycles ne sont pas quotidiens : le 23/08, le
# suivant portait sur le 25, pas le 24. Sonder une semaine couvre les trous.
JOURS_A_SONDER = 8

# Multiple du minimum d'ordre a engager. NE JAMAIS DESCENDRE A 1 : un ordre
# pose au minimum exact devient invendable des qu'il est rempli partiellement,
# ou meme totalement si un arrondi le laisse un cheveu en dessous. Mesure du
# 24/08 : 5 parts commandees, 4,9943 recues, vente refusee, 2,15 $ perdus.
MULTIPLE_MINIMUM = 2.0


def marches_updown(session) -> list[dict]:
    """Les Up/Down encore ouverts, decouverts par PLUSIEURS tris.

    LE BUG DU 23/08, et il etait ironique. Cette fonction n'interrogeait que le
    top 500 par `volume24hr`. Or un marche qui VIENT D'OUVRIR a un volume de
    zero : il n'y figure pas. On cherchait donc les marches neufs -- ceux qui
    ont le plus d'ecart, precisement parce que personne ne les a encore
    resserres -- en triant par activite PASSEE.

    Mesure : a 16h07, les trois Up/Down du 25 aout existaient avec 48 h devant
    eux et 17,4 % d'ecart, et l'outil affichait « 0 exploitable ». Il ne voyait
    que les deux marches du 23, clos depuis six minutes.

    Cumuler deux TRIS ne suffisait pas non plus -- deuxieme tentative, meme
    jour. Gamma plafonne chaque reponse a 500 marches et il y en a des
    milliers : les Up/Down du 25 ne remontaient ni par volume (nul, marche
    neuf) ni par `startDate` (trop de marches plus recents devant). L'outil
    affichait alors « 0 up/down », soit PIRE que le bug initial.

    On ne cherche donc plus : on DEMANDE. Le nommage est parfaitement regulier
    (`bitcoin-up-or-down-on-august-25-2026`), donc on genere les slugs des
    prochains jours et on les interroge nommement. Deterministe, et insensible
    au nombre de marches ouverts sur la place.
    """
    mois = ["january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december"]
    aujourd_hui = datetime.now(timezone.utc).date()
    slugs = [
        f"{actif}-up-or-down-on-{mois[jour.month - 1]}-{jour.day}-{jour.year}"
        for decalage in range(0, JOURS_A_SONDER)
        for jour in (aujourd_hui + timedelta(days=decalage),)
        for actif in ("bitcoin", "ethereum", "solana")
    ]
    trouves: dict[str, dict] = {}
    for depart in range(0, len(slugs), 20):
        tranche = slugs[depart:depart + 20]
        try:
            reponse = session.get(
                GAMMA,
                params=[("slug", s) for s in tranche] + [("limit", "100")],
                timeout=30,
            )
            reponse.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # Une tranche perdue n'est pas un univers vide : on le dit.
            logger.warning("%d slugs illisibles : %s", len(tranche), exc)
            continue
        for marche in reponse.json():
            if marche.get("closed"):
                continue
            slug = marche.get("slug") or ""
            if slug:
                trouves[slug] = marche
    return list(trouves.values())


def fin_de(marche: dict) -> datetime | None:
    brut = marche.get("endDate")
    if not brut:
        return None
    try:
        return datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None


def meilleur(niveaux):
    """Carnets Polymarket : PIRE PRIX EN PREMIER, le meilleur est en dernier."""
    return niveaux[-1] if niveaux else None


def examiner(client, marche: dict, maintenant: datetime, marge_entree: float):
    """Rend (candidat, motif). Un seul des deux est non nul."""
    fin = fin_de(marche)
    if fin is None:
        return None, "echeance illisible"
    restantes = (fin - maintenant).total_seconds() / 3600.0
    if restantes <= 0:
        # « cloture dans -0.1 h » ne veut rien dire pour qui lit la sortie.
        # Un marche passe se dit passe.
        return None, f"DEJA CLOS depuis {abs(restantes) * 60:.0f} min"
    if restantes < marge_entree:
        return None, f"cloture dans {restantes:.1f} h -- trop pres pour entrer"

    try:
        jetons = json.loads(marche.get("clobTokenIds") or "[]")
    except ValueError:
        return None, "jetons illisibles"
    if not jetons:
        return None, "jetons absents"

    # JAMAIS LE MINIMUM EXACT. Le 24/08, un ordre de 5 parts -- le minimum --
    # a ete rempli a 4,9943 par arrondi. Toute vente est alors REFUSEE
    # (« Size (4.99) lower than the minimum: 5 »), et la position part a la
    # resolution : elle ne vaut plus un prix, elle vaut 0 ou 1. 2,15 $ perdus
    # pour six MILLIEMES de part manquants.
    #
    # `making/core.exits` refusait deja de coter un « reliquat sous le minimum
    # -- invendable tel quel » : le garde-fou existait a la SORTIE, il manquait
    # a l'ENTREE. En engageant le double, une execution a 50 % laisse encore de
    # quoi ressortir.
    minimum = float(marche.get("orderMinSize") or 5)
    taille_min = minimum * MULTIPLE_MINIMUM
    cotes = []
    for jeton in jetons:
        carnet = client.get_order_book(token_id=jeton)
        bid, ask = meilleur(carnet.bids), meilleur(carnet.asks)
        if bid is None or ask is None:
            continue
        pb, pa = float(bid.price), float(ask.price)
        if pb <= 0 or pa >= 1:
            continue
        if float(bid.size) < PROFONDEUR_MIN or float(ask.size) < PROFONDEUR_MIN:
            continue
        cotes.append({
            "token_id": jeton,
            "bid": pb, "ask": pa,
            "ecart": pa - pb,
            "ecart_relatif": (pa - pb) / pb,
            "profondeur_bid": float(bid.size),
            "profondeur_ask": float(ask.size),
        })
    if not cotes:
        return None, "aucun cote avec contrepartie"

    # Le meilleur ecart RELATIF : c'est lui qui dit ce que rapporte un dollar
    # engage, pas l'ecart absolu.
    choix = max(cotes, key=lambda x: x["ecart_relatif"])
    if choix["ecart_relatif"] < ECART_MIN_RELATIF:
        return None, f"ecart {choix['ecart_relatif'] * 100:.1f} % trop mince"

    choix.update({
        "slug": marche.get("slug"),
        "question": marche.get("question"),
        "fin": fin,
        "heures": restantes,
        "parts": taille_min,
        "cout": taille_min * choix["bid"],
        "volume24h": float(marche.get("volume24hr") or 0),
    })
    return choix, None


def main() -> int:
    import httpx
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.making.runner import flatten
    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, required=True)
    parser.add_argument(
        "--marge-entree", type=float, default=4.0,
        help=(
            "heures minimales avant cloture pour OUVRIR. Sous cette marge un "
            "aller-retour n'a pas le temps de se boucler et l'achat devient un "
            "pari sur la resolution (defaut 4 h)."
        ),
    )
    parser.add_argument(
        "--marge-sortie", type=float, default=1.0,
        help=(
            "heures avant cloture ou l'on LIQUIDE, en traversant l'ecart s'il "
            "le faut. Perdre l'ecart est un cout, garder la position est un "
            "pari a 0 ou 1 (defaut 1 h)."
        ),
    )
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=45.0)
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )

    print("=" * 74)
    print("UP / DOWN CRYPTO -- CAPTURE D'ECART AVEC SORTIE FORCEE")
    print("=" * 74)
    if not args.arm:
        print("\nDESARME -- l'outil affiche son plan et n'envoie rien.")

    session = httpx.Client()
    debut = time.monotonic()

    # AVERTISSEMENT D'HORIZON. Le 23/08 l'outil a ete lance avec 150 min alors
    # que la sortie forcee tombait 47 h plus tard : il se serait arrete en
    # laissant la position seule pendant deux jours, ce que la sortie forcee
    # existe precisement pour empecher. On ne peut pas l'interdire -- relancer
    # l'outil est legitime -- mais on refuse de le taire.
    print(f"\nHorizon de cette session : {args.minutes:.0f} min.")
    print("  Si la position n'est pas soldee d'ici la, RELANCER l'outil.")
    print("  Une position up/down laissee sans surveillance jusqu'a sa")
    print("  resolution ne vaut plus un prix : elle vaut 0 ou 1.")
    ouvert: dict | None = None

    try:
        while time.monotonic() - debut < args.minutes * 60:
            maintenant = datetime.now(timezone.utc)
            try:
                marches = marches_updown(session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("univers illisible : %s", exc)
                time.sleep(args.interval)
                continue

            # LA SORTIE D'ABORD, TOUJOURS. Meme regle que `making/core.exits` :
            # l'eligibilite gouverne l'achat, jamais la sortie.
            if ouvert is not None:
                reste = (ouvert["fin"] - maintenant).total_seconds() / 3600.0
                if reste <= args.marge_sortie:
                    logger.error(
                        "LIQUIDATION : %s ferme dans %.2f h -- on sort au marche",
                        ouvert["slug"], reste,
                    )
                    if args.arm:
                        # ON LIQUIDE CE QU'ON DETIENT, pas ce qu'on a commande.
                        # Le 24/08 la liquidation visait `ouvert["parts"]` = la
                        # taille de l'ORDRE (5), alors que le remplissage avait
                        # donne 4,9943 parts. Vendre 5 quand on en a 4,99 est
                        # refuse, et le garde-fou ne se declenchait donc jamais.
                        detenu = 0.0
                        try:
                            for ligne in flatten(client.list_positions()):
                                if str(getattr(ligne, "token_id", "")) == ouvert["token_id"]:
                                    detenu = float(getattr(ligne, "size", 0) or 0)
                        except Exception as exc:  # noqa: BLE001
                            logger.error("inventaire illisible : %s", exc)
                            detenu = ouvert["parts"]

                        minimum = ouvert.get("min_ordre", 5.0)
                        if detenu <= 0:
                            print("Rien a liquider : aucune part detenue.")
                        elif detenu < minimum:
                            # LE PIEGE DU 24/08, dit franchement plutot que
                            # tente puis rate. 4,9943 parts pour un minimum de
                            # 5 : l'ordre est refuse quel que soit son type, et
                            # la position part a la resolution. Une liquidation
                            # qui ne PEUT PAS s'executer n'est pas un garde-fou,
                            # et le taire ferait croire que la sortie a eu lieu.
                            print(f"\n!! LIQUIDATION IMPOSSIBLE : {detenu:.4f} parts "
                                  f"detenues pour un minimum d'ordre de {minimum:.0f}.")
                            print(f"   La position ({detenu * ouvert['bid']:.2f} $) ira a "
                                  f"la RESOLUTION : elle vaudra 0 ou 1.")
                            print("   Cause : l'entree a engage le minimum exact, donc")
                            print("   tout remplissage partiel devient invendable.")
                        else:
                            try:
                                client.cancel_all()
                                client.place_market_order(
                                    token_id=ouvert["token_id"],
                                    shares=detenu, side="SELL",
                                )
                                print(f"Position liquidee avant resolution : "
                                      f"{detenu:.4f} parts.")
                            except Exception as exc:  # noqa: BLE001
                                logger.error("LIQUIDATION ECHOUEE : %s", exc)
                                print("!! POSITION NON SOLDEE -- verifier a la main.")
                    return 0

            candidats, motifs = [], []
            for marche in marches:
                try:
                    trouve, motif = examiner(
                        client, marche, maintenant, args.marge_entree
                    )
                except Exception as exc:  # noqa: BLE001
                    trouve, motif = None, f"lecture: {exc}"
                if trouve:
                    candidats.append(trouve)
                elif motif:
                    motifs.append((str(marche.get("slug", "?"))[:36], motif))

            candidats = [c for c in candidats if c["cout"] <= args.bankroll]
            candidats.sort(key=lambda c: -c["ecart_relatif"])

            print(f"\n--- {maintenant:%H:%M:%S} UTC | {len(marches)} up/down | "
                  f"{len(candidats)} exploitable(s)")
            for slug, motif in motifs[:4]:
                print(f"    ecarte : {slug} -- {motif}")
            for c in candidats[:4]:
                print(f"    {c['ecart_relatif'] * 100:>5.1f} % brut | achat "
                      f"{c['bid']:.3f} vente {c['ask']:.3f} | {c['parts']:.0f} parts "
                      f"= {c['cout']:.2f} $ | {c['heures']:.1f} h | {c['slug'][:30]}")

            # ADOPTION DE SON PROPRE ORDRE AU REDEMARRAGE.
            # Troisieme fois que ce piege se referme le meme jour, et la
            # deuxieme sur cet outil : une boucle qui repart avec une memoire
            # vide ne reconnait pas ce qu'elle a laisse au carnet. Ici elle a
            # tente de reposer un achat de 2,30 $ alors que le premier y etait
            # encore -- 4,60 $ demandes pour 4,27 $ disponibles, refuse par
            # Polymarket (`not enough balance`). Sans ce refus, on aurait
            # double la position sans le vouloir.
            if ouvert is None and args.arm:
                par_jeton = {}
                for marche in marches:
                    try:
                        for jeton in json.loads(marche.get("clobTokenIds") or "[]"):
                            par_jeton[str(jeton)] = marche
                    except ValueError:
                        continue
                try:
                    for vivant in flatten(client.list_open_orders()):
                        if str(vivant.side).upper() != "BUY":
                            continue
                        marche = par_jeton.get(str(vivant.token_id))
                        if marche is None:
                            continue
                        fin = fin_de(marche)
                        if fin is None:
                            continue
                        # On reprend le prix de vente vise depuis le carnet
                        # courant : l'ask d'il y a deux heures n'existe plus.
                        carnet = client.get_order_book(token_id=str(vivant.token_id))
                        cible = meilleur(carnet.asks)
                        ouvert = {
                            "token_id": str(vivant.token_id),
                            "slug": marche.get("slug"),
                            "fin": fin,
                            "bid": float(vivant.price),
                            "ask": float(cible.price) if cible else float(vivant.price),
                            "parts": float(vivant.original_size),
                        }
                        print(f"\nREPRISE : achat deja au carnet, "
                              f"{ouvert['parts']:.0f} @ {ouvert['bid']:.3f} sur "
                              f"{str(ouvert['slug'])[:34]} -- adopte, aucun doublon.")
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("carnet illisible a l'adoption : %s", exc)

            # LA VENTE, DES QUE L'ACHAT EST REMPLI. Sans elle l'outil achete
            # puis liquide au marche : il paie l'ecart DEUX FOIS et la capture
            # de 17 % devient une perte garantie. C'etait le defaut du premier
            # jet, trouve le 23/08 juste apres le premier achat reel.
            if ouvert is not None and args.arm and not ouvert.get("vente_posee"):
                try:
                    detenu = 0.0
                    for ligne in flatten(client.list_positions()):
                        if str(getattr(ligne, "token_id", "")) == ouvert["token_id"]:
                            detenu = float(getattr(ligne, "size", 0) or 0)
                    if detenu >= ouvert["parts"]:
                        recu = client.place_limit_order(
                            token_id=ouvert["token_id"], price=ouvert["ask"],
                            size=detenu, side="SELL", post_only=True,
                        )
                        if bool(getattr(recu, "success",
                                        getattr(recu, "ok", False))):
                            ouvert["vente_posee"] = True
                            gain = (ouvert["ask"] - ouvert["bid"]) * detenu
                            print(f"\nACHAT REMPLI -> VENTE POSEE : {detenu:.0f} "
                                  f"@ {ouvert['ask']:.3f} (gain vise "
                                  f"{gain:+.2f} $)")
                        else:
                            logger.error("vente refusee : %s", recu)
                except Exception as exc:  # noqa: BLE001
                    logger.error("pose de la vente impossible : %s", exc)

            if ouvert is None and candidats and args.arm:
                choix = candidats[0]
                recu = client.place_limit_order(
                    token_id=choix["token_id"], price=choix["bid"],
                    size=choix["parts"], side="BUY", post_only=True,
                )
                if bool(getattr(recu, "success", getattr(recu, "ok", False))):
                    ouvert = choix
                    sortie = choix["fin"] - timedelta(hours=args.marge_sortie)
                    print(f"\nACHAT POSE : {choix['parts']:.0f} @ {choix['bid']:.3f} "
                          f"= {choix['cout']:.2f} $ -- sortie forcee a "
                          f"{sortie:%H:%M} UTC")
                    chemin = os.path.join(
                        os.environ.get("TEMP", "."), "updown-session.json"
                    )
                    with open(chemin, "w", encoding="utf-8") as f:
                        json.dump({
                            "ouvert_le": maintenant.isoformat(),
                            "clot_prevue": choix["fin"].isoformat(),
                            "slug": choix["slug"],
                            "token_id": choix["token_id"],
                            "prix_achat": choix["bid"],
                            "parts": choix["parts"],
                            "ordre_id": str(getattr(recu, "order_id", "")),
                        }, f, indent=2, ensure_ascii=False)
                else:
                    print(f"achat refuse : {recu}")

            time.sleep(args.interval)
    finally:
        session.close()

    if ouvert is not None:
        print("\nATTENTION : une position reste ouverte et la boucle s'arrete.")
        print(f"  {ouvert['slug']} ferme a {ouvert['fin']:%H:%M} UTC.")
        print("  Relancer l'outil, ou solder a la main AVANT la resolution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
