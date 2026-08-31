# -*- coding: utf-8 -*-
r"""Cherche les evenements dont les prix se CONTREDISENT -- LECTURE SEULE.

    .venv\Scripts\python tools\incoherences.py
    .venv\Scripts\python tools\incoherences.py --evenements 600 --marge 0.005

Aucune cle, aucun ordre. Le geste d'engager de l'argent reste a l'humain.

## Ce qu'il cherche, et pourquoi ce n'est pas une prediction

Un evenement `negRisk` a des issues MUTUELLEMENT EXCLUSIVES : exactement une
se realise. « Fed decision in September » a cinq issues, une seule sera vraie.
Donc detenir une part de CHAQUE issue rapporte exactement 1 $, quoi qu'il
arrive.

    somme des `bestAsk` < 1 $  ->  on achete tout, on encaisse 1 $
    somme des `bestBid` > 1 $  ->  on vend tout, on encaisse plus que 1 $

C'est de l'arithmetique, pas une opinion sur qui va gagner. C'est la seule
famille de strategies qui reste apres avoir mesure et enterre les autres :
arbitrage a deux jambes MORT (0 sur 1 937 marches, 2026-07-28), recompenses de
liquidite MORTES (0 $ gagne, 0 sur 14 up/down), sniping de latence hors de
portee (fenetre a 2,7 s), tenue de marche sur ecart large mesuree perdante.

## LE PIEGE QUI TUE CETTE STRATEGIE, dit avant les resultats

L'arbitrage a deux jambes se rate proprement : les deux ordres partent, l'un
passe, l'autre non, on annule. A CINQ jambes, on peut etre rempli sur trois et
rester a decouvert sur deux -- avec une position DIRECTIONNELLE qu'on n'a
jamais voulue, sur un compte qui ne peut pas l'absorber. C'est exactement la
faute du 2026-08-21 : forcer des ordres sur des carnets qui ne presentaient pas
le montage, quatre positions mortes achetees le meme jour.

D'ou le fait que cet outil rende le NOMBRE DE JAMBES et le TICKET TOTAL, pas
seulement l'ecart. Un ecart de 2 % sur cinq jambes a 5 $ minimum chacune, c'est
25 $ engages pour 0,50 $ -- sur un compte de 11 $, ce n'est pas une occasion,
c'est une impossibilite.

## CE QU'IL TROUVE N'EST PAS EXECUTABLE -- mesure du 2026-08-31

A LIRE AVANT DE CROIRE UN RESULTAT DE CET OUTIL. Le 31/08 il a trouve trois
evenements dont la somme des `bestBid` depasse 1, dont « Fed Decision in
October » a +1,10 %, verifie sur le carnet EN DIRECT avec de la profondeur
jusqu'a 500 $. Puis verification du SDK : `split_position` scinde du
collateral en YES + NO d'UN SEUL marche binaire, et il n'existe AUCUNE
operation de conversion negRisk. Assembler un YES de chaque issue impose donc
de les acheter un par un a l'ask -- somme 1,0340 -- pour les revendre a
1,0110. C'est une PERTE de 2,3 %, pas un gain de 1,1 %.

L'incoherence existe PRECISEMENT PARCE QU'ELLE N'EST PAS ARBITRABLE. C'est la
reponse a « pourquoi personne ne l'a prise » : le marche n'est pas distrait,
l'operation manque. La version executable -- deux jambes, `split` puis
`merge` -- a ete mesuree MORTE : 0 sur 1 937 marches.

Cet outil reste utile pour MESURER cette famille d'inefficiences, que personne
n'a documentee. Il ne doit pas servir a engager de l'argent tant que le
mecanisme de conversion n'a pas ete atteint par appel direct au contrat, ce
qui est un chantier separe et irreversible.

## Les prix de Gamma sont INDICATIFS

`bestAsk` et `bestBid` viennent de l'API Gamma, pas du carnet en direct. Ils
suffisent a ECARTER 99 % des evenements ; ils ne suffisent pas a engager de
l'argent. Tout candidat doit etre reverifie sur le CLOB avant d'agir -- et
c'est precisement l'ecart entre les deux qui a fait perdre de l'argent a la
moitie des gens qui racontent leur bot sur Reddit.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

GAMMA = "https://gamma-api.polymarket.com/events"
PAR_PAGE = 100

# Sous ce montant de volume sur 24 h, un ecart n'est pas une occasion : c'est
# un carnet que personne ne tient. Mesure du 2026-08-28 : les ecarts larges
# sont des matchs deja joues, pas des inefficiences.
VOLUME_MIN = 500.0

# Marge par defaut. Sous 0,5 %, l'ecart est mange par les frais preneurs et le
# moindre mouvement de carnet entre la lecture et l'ordre.
MARGE = 0.005

# Taille minimale d'un ordre, en parts (`orderMinSize`, mesure du 2026-08-24).
# C'est elle qui fixe la mise, pas le nombre de jambes.
MIN_PARTS = 5.0


def evenements(client, combien: int) -> list[dict]:
    """Les evenements ouverts les plus actifs, pagines."""
    out, decalage = [], 0
    while len(out) < combien:
        lot = client.get(GAMMA, params={
            "closed": "false", "limit": PAR_PAGE, "offset": decalage,
            "order": "volume24hr", "ascending": "false"}, timeout=30).json()
        if not isinstance(lot, list) or not lot:
            break
        out.extend(lot)
        if len(lot) < PAR_PAGE:
            break
        decalage += PAR_PAGE
    return out[:combien]


def f(valeur) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


def examiner(ev: dict, marge: float) -> dict | None:
    """Rend le defaut d'arithmetique d'un evenement, ou None."""
    if not ev.get("negRisk"):
        return None
    marches = [m for m in ev.get("markets", [])
               if m.get("acceptingOrders") and m.get("enableOrderBook")
               and not m.get("closed")]
    if len(marches) < 2:
        return None

    asks = [f(m.get("bestAsk")) for m in marches]
    bids = [f(m.get("bestBid")) for m in marches]
    # UN SEUL PRIX MANQUANT INVALIDE TOUT. Sans le prix d'une jambe on ne peut
    # pas acheter l'ensemble complet, donc la garantie de 1 $ n'existe plus.
    # Ignorer la jambe illisible donnerait une somme trop basse et un faux
    # signal -- le genre d'erreur qui envoie de l'argent reel sur du vent.
    if any(a is None for a in asks) or any(b is None for b in bids):
        return None

    somme_ask, somme_bid = sum(asks), sum(bids)
    volume = f(ev.get("volume24hr")) or 0.0

    if somme_ask < 1 - marge:
        sens, ecart = "ACHAT de toutes les issues", 1 - somme_ask
    elif somme_bid > 1 + marge:
        sens, ecart = "VENTE de toutes les issues", somme_bid - 1
    else:
        return None
    if volume < VOLUME_MIN:
        return None

    return {"titre": ev.get("title", "")[:52], "slug": ev.get("slug", ""),
            "jambes": len(marches), "somme_ask": somme_ask,
            "somme_bid": somme_bid, "ecart": ecart, "sens": sens,
            "volume": volume}


def marcher(niveaux: list[dict], parts: float) -> tuple[float, float]:
    """Prix moyen reellement obtenu en vendant `parts`, et parts servies.

    UN PRIX SANS PROFONDEUR N'EST PAS UN PRIX. `bestBid` dit a quel prix on
    vendrait UNE part ; il ne dit rien de la deuxieme. Cette fonction descend
    le carnet niveau par niveau, ce qui est exactement ce que fera l'ordre.
    L'ecart entre les deux est ce qui a ruine la moitie des bots racontes sur
    Reddit -- « le signal n'etait pas le probleme, c'est tout ce qui se passe
    entre le signal et le remplissage ».
    """
    reste, encaisse = parts, 0.0
    for n in sorted(niveaux, key=lambda x: -float(x["price"])):
        prix, taille = float(n["price"]), float(n["size"])
        pris = min(reste, taille)
        encaisse += pris * prix
        reste -= pris
        if reste <= 1e-9:
            break
    servies = parts - reste
    return (encaisse / servies if servies else 0.0), servies


def verifier(client, slug: str, parts: float) -> int:
    """Confronte un candidat au carnet en direct, jambe par jambe."""
    import json as _json

    ev = client.get(GAMMA, params={"slug": slug}, timeout=30).json()
    if not ev:
        print(f"evenement introuvable : {slug}")
        return 1
    ev = ev[0]
    marches = [m for m in ev.get("markets", [])
               if m.get("acceptingOrders") and not m.get("closed")]
    print(f"{ev.get('title','')}\n{len(marches)} jambes · "
          f"{parts:.0f} parts par jambe\n")

    total_affiche = total_reel = 0.0
    complet = True
    for m in marches:
        jetons = m.get("clobTokenIds")
        if isinstance(jetons, str):
            jetons = _json.loads(jetons)
        livre = client.get("https://clob.polymarket.com/book",
                           params={"token_id": jetons[0]}, timeout=30).json()
        bids = livre.get("bids") or []
        affiche = max((float(b["price"]) for b in bids), default=0.0)
        reel, servies = marcher(bids, parts)
        total_affiche += affiche
        total_reel += reel * (servies / parts) if parts else 0.0
        if servies < parts:
            complet = False
        etat = "OK" if servies >= parts else f"SEULEMENT {servies:.1f} parts"
        print(f"  {(m.get('groupItemTitle') or m.get('question',''))[:34]:<34} "
              f"affiche {affiche:.3f} · obtenu {reel:.3f} · {etat}")

    print(f"\n  somme des prix AFFICHES : {total_affiche:.4f}")
    print(f"  somme des prix OBTENUS  : {total_reel:.4f}")
    marge_reelle = total_reel - 1.0
    print(f"\n  Mise {parts:.0f} $ · gain reel {parts * marge_reelle:+.3f} $ "
          f"({100 * marge_reelle:+.2f} %)")
    if not complet:
        print("\n  PROFONDEUR INSUFFISANTE sur au moins une jambe. L'ensemble\n"
              "  complet ne peut pas etre vendu : ce qui reste est une position\n"
              "  DIRECTIONNELLE qu'on n'a pas voulue. Ne pas engager.")
    elif marge_reelle <= 0:
        print("\n  L'ECART NE SURVIT PAS AU CARNET. Il existait a l'affichage,\n"
              "  il disparait des qu'on demande la quantite. C'est le resultat,\n"
              "  et il se publie.")
    else:
        print("\n  L'ecart survit A CETTE SECONDE. Frais preneurs et gaz de la\n"
              "  scission ne sont PAS deduits ci-dessus -- les retrancher avant\n"
              "  de conclure quoi que ce soit.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--verifier", metavar="SLUG",
                   help="confronte un evenement au carnet en direct")
    p.add_argument("--parts", type=float, default=MIN_PARTS)
    p.add_argument("--evenements", type=int, default=400)
    p.add_argument("--marge", type=float, default=MARGE)
    p.add_argument("--capital", type=float, default=11.30,
                   help="pour dire si le ticket est seulement payable")
    args = p.parse_args()

    import httpx
    client = httpx.Client(headers={"accept": "application/json"})

    if args.verifier:
        return verifier(client, args.verifier, args.parts)

    tous = evenements(client, args.evenements)
    exclusifs = [e for e in tous if e.get("negRisk")]
    print(f"{len(tous)} evenements lus, {len(exclusifs)} a issues exclusives")

    trouves = [t for t in (examiner(e, args.marge) for e in exclusifs) if t]
    if not trouves:
        print(f"\nAUCUNE incoherence au-dela de {100 * args.marge:.1f} %.\n"
              "Ce n'est pas un echec de l'outil. L'arbitrage a deux jambes a\n"
              "ete mesure mort sur 1 937 marches ; si la version a N jambes\n"
              "l'est aussi, c'est un RESULTAT, et il se publie.\n"
              "Relancer plus tard : ces ecarts, quand ils existent, durent\n"
              "des secondes.")
        return 0

    trouves.sort(key=lambda t: -t["ecart"])
    print(f"\n{len(trouves)} evenement(s) dont les prix se contredisent :\n")
    for t in trouves:
        # LE TICKET AVANT L'ECART. Un ecart alléchant mais impayable donne
        # envie de forcer si on le lit apres le pourcentage. On le dit avant.
        #
        # ET LE NOMBRE DE JAMBES NE MULTIPLIE PAS L'ARGENT. Premiere version
        # de cet outil : `jambes x 5 $`, qui declarait « hors de portee » des
        # occasions parfaitement payables. Faux dans les deux sens :
        #   - VENTE : on scinde N dollars en une part de CHAQUE issue, puis on
        #     vend chaque jambe. Capital = N dollars, un point.
        #   - ACHAT : `MIN_PARTS` parts de chaque jambe coutent
        #     `MIN_PARTS x somme des ask`, soit ~N dollars puisque la somme
        #     vaut ~1 par construction.
        # Le nombre de jambes multiplie les ORDRES et donc le risque de
        # remplissage partiel -- pas la mise.
        ticket = MIN_PARTS * (t["somme_ask"] if "ACHAT" in t["sens"] else 1.0)
        payable = "PAYABLE" if ticket <= args.capital else "HORS DE PORTEE"
        print(f"  {t['titre']}")
        print(f"    {t['sens']} · {t['jambes']} jambes · "
              f"mise ~{ticket:.2f} $ -> {payable} a {args.capital:.2f} $ · "
              f"gain attendu ~{ticket * t['ecart']:.3f} $")
        print(f"    somme ask {t['somme_ask']:.4f} · somme bid "
              f"{t['somme_bid']:.4f} · ecart {100 * t['ecart']:.2f} % · "
              f"volume 24 h {t['volume']:,.0f} $")
        print(f"    https://polymarket.com/event/{t['slug']}\n")

    print("PRIX INDICATIFS (Gamma), pas le carnet en direct. Reverifier chaque\n"
          "jambe sur le CLOB avant d'engager quoi que ce soit : l'ecart entre\n"
          "le prix affiche et le prix obtenu est ce qui a ruine la moitie des\n"
          "bots racontes sur Reddit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
