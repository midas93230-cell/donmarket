# -*- coding: utf-8 -*-
r"""Verifie ce qu'un portefeuille Polymarket a REELLEMENT fait -- LECTURE SEULE.

    .venv\Scripts\python tools\verifier_portefeuille.py 0xADRESSE
    .venv\Scripts\python tools\verifier_portefeuille.py 0xADRESSE --annonce 438000

Aucune cle, aucune authentification, aucun ordre. Tout vient de l'API publique,
donc n'importe qui peut refaire le chiffre -- c'est le but.

## Pourquoi cet outil existe

Recherche Reddit « polymarket bot », 75 publications relevees le 2026-08-29.
Les annonces : 1,4 k$ devenus 965 388 $ (99,3 % sur 32 614 trades), 313 $
devenus 438 k$ en un mois, 25 k$/jour trente jours d'affilee, « 100 % win rate,
zero losses ». Les seuls chiffres dont le portefeuille est PUBLIC, sur la meme
page : ~5 000 $ en trois mois puis ~650 $ le mois suivant, et +256 $ sur 500 $
de mise. Deux ordres de grandeur d'ecart entre ce qui est annonce et ce qui est
montre.

Et le commentaire qui revient : « 14 des 20 premiers portefeuilles sont des
bots, et AUCUN ne peut prouver que son historique est reel. »

Tout est pourtant on-chain. Personne ne le lit. Cet outil le lit.

## Les trois chiffres que personne ne publie

1. LES DEPOTS. « 313 $ devenus 438 k$ » ne veut rien dire si le portefeuille a
   recu 500 k$ de depots entre-temps. Le depot est le denominateur de toute
   l'histoire, et il est public.

2. CE QUI EST SOLDE. Une position ouverte n'est pas un gain. Un portefeuille
   qui accumule des parts a 0,95 affiche une plus-value magnifique jusqu'au
   jour de la resolution, ou elle vaut 1 ou 0.

3. CE QU'ON NE SAIT PAS CALCULER. Cet outil REFUSE de rendre un PnL total tant
   qu'il reste des positions ouvertes ou des evenements qu'il ne sait pas
   comptabiliser, et il dit lesquels. Un chiffre faux mais precis est
   exactement ce qu'on denonce ici ; en produire un serait se ranger du cote
   des annonces.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from decimal import Decimal

sys.path.insert(0, ".")

# Un paquet de parts sous ce seuil est un residu d'arrondi, pas une position.
# Mesure du 2026-08-29 : une vente partielle laisse 1,74 part sur 25.
RESIDU = Decimal("0.01")

# Types d'evenements dont la comptabilite est comprise. Tout le reste force le
# refus de conclure -- SPLIT, MERGE et CONVERSION deplacent des parts sans
# passer par un prix, et les ignorer donnerait un PnL faux dans le bon sens.
CONNUS = {"TRADE", "DEPOSIT", "REDEEM", "WITHDRAW", "WITHDRAWAL"}
MOUVEMENTS = {"SPLIT", "MERGE", "CONVERSION"}

# CE QUE POLYMARKET REFUSE DE SERVIR. Au-dela de 5 000 actes, l'API repond
# « max historical activity offset of 5000 exceeded ». Les trois premiers du
# classement all-time sont tous au-dessus. Aucun outil, le notre compris, ne
# peut donc verifier un historique plus long -- et c'est la vraie raison pour
# laquelle personne ne prouve rien sur cette place de marche.
PLAFOND_API = 5000


def collecter(client, wallet: str, limite: int) -> tuple[list, bool]:
    """Rend (actes, plafond_atteint). Un `Paginator` itere des PAGES, pas des
    lignes -- piege mesure le 2026-08-20.

    LE PLAFOND EST CELUI DE POLYMARKET, PAS LE NOTRE. Mesure du 2026-08-29 sur
    les trois premiers du classement : au-dela de 5 000 actes, l'API repond
    « max historical activity offset of 5000 exceeded » et rien d'autre. C'est
    la raison MECANIQUE pour laquelle « 99,3 % sur 32 614 trades » ne peut pas
    etre verifie par qui que ce soit : le registre public est tronque a la
    source. Ce n'est pas une panne a rattraper, c'est un fait a publier.
    """
    out, plafond = [], False
    try:
        for page in client.list_activity(user=wallet, page_size=100):
            items = getattr(page, "items", None)
            out.extend(items if items is not None else [page])
            if len(out) >= min(limite, PLAFOND_API):
                break
    except Exception as exc:  # noqa: BLE001
        if "offset" not in str(exc).lower():
            raise
        plafond = True
    return out[:limite], plafond or len(out) >= PLAFOND_API


def d(valeur) -> Decimal:
    return Decimal(str(valeur)) if valeur is not None else Decimal(0)


def comptabiliser(actes: list) -> dict:
    """Reduit une liste d'actes en positions par jeton, sans rien extrapoler."""
    jetons: dict = defaultdict(lambda: {
        "titre": "", "achat_parts": Decimal(0), "achat_usdc": Decimal(0),
        "vente_parts": Decimal(0), "vente_usdc": Decimal(0),
        "redeem_parts": Decimal(0), "redeem_usdc": Decimal(0),
    })
    depots = retraits = Decimal(0)
    types = Counter()

    for a in actes:
        t = (getattr(a, "type", "") or "").upper()
        types[t] += 1
        montant, parts = d(getattr(a, "amount", None)), d(getattr(a, "shares", None))

        if t == "DEPOSIT":
            depots += montant
            continue
        if t in ("WITHDRAW", "WITHDRAWAL"):
            retraits += montant
            continue

        jeton = jetons[getattr(a, "token_id", None)]
        jeton["titre"] = jeton["titre"] or (getattr(a, "title", "") or "")
        if t == "REDEEM":
            jeton["redeem_parts"] += parts
            jeton["redeem_usdc"] += montant
        elif t == "TRADE":
            cote = (getattr(a, "side", "") or "").upper()
            if cote == "BUY":
                jeton["achat_parts"] += parts
                jeton["achat_usdc"] += montant
            elif cote == "SELL":
                jeton["vente_parts"] += parts
                jeton["vente_usdc"] += montant

    return {"jetons": dict(jetons), "depots": depots, "retraits": retraits,
            "types": types}


def trier(jetons: dict) -> tuple[list, list]:
    """Separe ce qui est SOLDE de ce qui est encore ouvert.

    C'est la separation qui fait tout le travail : le PnL d'une position soldee
    est un fait arithmetique, celui d'une position ouverte est une opinion sur
    un prix futur.
    """
    soldes, ouverts = [], []
    for jeton in jetons.values():
        # UN REDEEM NE PORTE PAS TOUJOURS SES PARTS. Mesure du 2026-08-29 :
        # le remboursement Solana rend 4,99 $ avec `shares` vide. Sans ce
        # rattrapage, une position remboursee -- donc fermee par definition --
        # resterait comptee « ouverte » pour toujours.
        if jeton["redeem_usdc"] > 0 and jeton["redeem_parts"] == 0:
            jeton["redeem_parts"] = jeton["achat_parts"] - jeton["vente_parts"]
        reste = (jeton["achat_parts"] - jeton["vente_parts"]
                 - jeton["redeem_parts"])
        gain = (jeton["vente_usdc"] + jeton["redeem_usdc"]
                - jeton["achat_usdc"])
        ligne = {**jeton, "reste": reste, "gain": gain}
        (soldes if abs(reste) <= RESIDU else ouverts).append(ligne)
    return soldes, ouverts


def bloquants(types: Counter, ouverts: list) -> list[str]:
    """Ce qui empeche d'annoncer un total. Vide = on peut conclure."""
    motifs = []
    for t, n in types.items():
        if t in MOUVEMENTS:
            motifs.append(f"{n} evenement(s) {t} : des parts changent de main "
                          "sans passer par un prix, la comptabilite par jeton "
                          "serait fausse")
        elif t not in CONNUS:
            motifs.append(f"{n} evenement(s) de type inconnu « {t} »")
    if ouverts:
        engage = sum(o["achat_usdc"] - o["vente_usdc"] - o["redeem_usdc"]
                     for o in ouverts)
        motifs.append(f"{len(ouverts)} position(s) encore ouverte(s), "
                      f"{engage:.2f} $ engages : leur valeur depend d'un prix "
                      "futur, pas d'un fait")
    return motifs


def rapport(wallet: str, actes: list, compta: dict, annonce: float | None,
            limite: int, plafond: bool = False) -> None:
    soldes, ouverts = trier(compta["jetons"])
    gagnants = [s for s in soldes if s["gain"] > 0]
    realise = sum(s["gain"] for s in soldes)
    motifs = bloquants(compta["types"], ouverts)

    print("=" * 70)
    print(f"PORTEFEUILLE {wallet}")
    print("=" * 70)
    if not actes:
        print("\nAucune activite lisible. Adresse inexistante, jamais utilisee,\n"
              "ou l'API ne la sert pas. Rien a conclure -- surtout pas que le\n"
              "portefeuille est vide.")
        return

    dates = sorted(a.timestamp for a in actes if getattr(a, "timestamp", None))
    if dates:
        jours = max((dates[-1] - dates[0]).days, 1)
        print(f"\n{len(actes)} actes lus, du {dates[0]:%Y-%m-%d} au "
              f"{dates[-1]:%Y-%m-%d} ({jours} jours)")
        if plafond:
            print("\n  *** NON VERIFIABLE — ET CE N'EST PAS NOTRE LIMITE. ***\n"
                  f"  Polymarket refuse de servir au-dela de {PLAFOND_API} "
                  "actes par portefeuille\n  (« max historical activity offset "
                  f"of {PLAFOND_API} exceeded »). L'historique de ce\n  "
                  "portefeuille est plus long. Tout ce qui suit ne porte donc "
                  "que sur la\n  fenetre lue, et AUCUN total, taux ou ratio "
                  "n'est valide pour ce compte.\n"
                  "  C'est la raison mecanique pour laquelle personne ne peut "
                  "prouver un\n  historique de 32 614 trades : le registre "
                  "public est tronque a la source.")
        elif len(actes) >= limite:
            print(f"  ATTENTION : plafond local de {limite} actes atteint. "
                  "Relancer avec --max plus haut.")
    print(f"  repartition : {dict(compta['types'])}")

    print(f"\nDEPOTS      : {compta['depots']:>12.2f} $   <-- le denominateur "
          "que les annonces omettent")
    print(f"RETRAITS    : {compta['retraits']:>12.2f} $")
    if compta["retraits"] > compta["depots"]:
        # UN RETRAIT EST LA MEILLEURE PREUVE QU'IL Y AIT. Un PnL affiche est
        # une ligne dans une interface ; de l'argent SORTI du compte a du
        # exister pour sortir. Sortir plus qu'on n'a depose ne se simule pas.
        print(f"  CORROBORATION : {compta['retraits'] - compta['depots']:.2f} $ "
              "sortis de plus qu'il n'en est entre.\n"
              "  Un retrait ne se falsifie pas -- c'est la preuve la plus "
              "solide disponible ici,\n  bien plus qu'un PnL affiche.")

    engage = sum(o["achat_usdc"] - o["vente_usdc"] - o["redeem_usdc"]
                 for o in ouverts)
    plancher = realise - engage

    print(f"\nPOSITIONS SOLDEES : {len(soldes)}")
    if soldes:
        print(f"  gain realise    : {realise:>12.2f} $")
        print(f"  taux de reussite: {100 * len(gagnants) / len(soldes):>11.1f} % "
              f"({len(gagnants)}/{len(soldes)})")
    print(f"POSITIONS OUVERTES : {len(ouverts):<3d}  cout engage : {engage:.2f} $")
    if ouverts:
        # LE BIAIS QUI REND TOUS CES CHIFFRES FLATTEURS. On solde ses gagnantes
        # -- il y a un acheteur -- et on garde ses perdantes, faute de
        # contrepartie. Les pertes s'accumulent donc dans « ouvertes » et ne
        # sont JAMAIS comptees, pendant que le gain realise et le taux de
        # reussite ne voient que les gagnantes. Un taux de reussite sur
        # positions soldees seules est structurellement surestime.
        print("  ATTENTION : les perdantes invendables restent ici et ne sont\n"
              "  jamais comptees. Le gain realise et le taux de reussite\n"
              "  ci-dessus sont donc des BORNES HAUTES, pas des resultats.")

    print("\n" + "-" * 70)
    if motifs:
        print("PNL TOTAL : REFUSE DE CONCLURE.")
        for m in motifs:
            print(f"  - {m}")
    # LE PLANCHER EST LE SEUL CHIFFRE HONNETE QUAND IL RESTE DES OUVERTES.
    # Il suppose que toutes les positions ouvertes valent ZERO -- l'hypothese
    # la plus defavorable possible. Ce qu'il rend n'est donc pas une estimation
    # mais une GARANTIE : le resultat reel ne peut pas etre en dessous.
    # UN PLANCHER N'EST GARANTI QUE SUR UN HISTORIQUE COMPLET. Sur une fenetre
    # tronquee il ne vaut que pour la fenetre, et l'appeler « garanti » serait
    # exactement l'abus de confiance qu'on reproche aux annonces.
    titre = "PLANCHER GARANTI" if not plafond else \
        f"PLANCHER SUR LA FENETRE LUE SEULEMENT ({len(actes)} derniers actes)"
    print(f"\n{titre} : {plancher:+.2f} $ "
          f"pour {compta['depots']:.2f} $ deposes", end="")
    if compta["depots"] > 0:
        print(f"  ({100 * plancher / compta['depots']:+.1f} %)")
    else:
        print()
    print("  (toutes les positions ouvertes supposees a zero ; le resultat\n"
          "   reel est au-dessus, mais le dire exigerait les prix courants)")

    if annonce is not None:
        print("\n" + "=" * 70)
        print(f"ANNONCE : {annonce:.2f} $")
        if plafond or len(actes) >= limite:
            # LA COMPARAISON EST INTERDITE SUR UN HISTORIQUE TRONQUE. Mesure du
            # 2026-08-29 sur le 1er du classement : 2 000 actes lus sur un
            # historique bien plus long, et l'outil annoncait « depasse le
            # prouvable d'un facteur 104,9 ». C'etait un chiffre confiant et
            # faux -- exactement ce que cet outil existe pour denoncer. On ne
            # confronte une annonce QUE si on a lu tout l'historique.
            print("CONFRONTATION IMPOSSIBLE : historique tronque.")
            print("Un ratio calcule sur une fenetre partielle n'aurait aucun "
                  "sens.")
            if plafond:
                # NE JAMAIS CONSEILLER UNE ACTION IMPOSSIBLE. Le plafond est
                # celui de Polymarket : relancer avec --max plus haut ne change
                # rien. Dire « reessaie » ici ferait perdre un tour a chaque
                # fois, comme le conseil `--large` de chercher_cycle.
                print(f"Et AUCUN reglage n'y changera rien : le plafond de "
                      f"{PLAFOND_API} actes est celui de\nPolymarket. Le seul "
                      "verdict honnete sur ce portefeuille est : NON "
                      "VERIFIABLE.")
            else:
                print(f"Relancer avec --max au-dessus de {limite}.")
            return
        print(f"PROUVE  : {realise:.2f} $ (positions soldees)")
        print(f"ECART   : {Decimal(str(annonce)) - realise:.2f} $")
        if realise > 0 and Decimal(str(annonce)) > realise * 2:
            print("\nL'annonce depasse le prouvable d'un facteur "
                  f"{Decimal(str(annonce)) / realise:.1f}. "
                  "Elle n'est pas refutee\npour autant : elle est NON PROUVEE, "
                  "ce qui n'est pas la meme chose.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("wallets", nargs="+", help="adresses 0x... a verifier")
    p.add_argument("--annonce", type=float, default=None,
                   help="gain annonce publiquement, en dollars, a confronter")
    p.add_argument("--max", type=int, default=5000,
                   help="plafond d'actes lus par portefeuille")
    args = p.parse_args()

    from polymarket import PublicClient
    client = PublicClient()

    for wallet in args.wallets:
        try:
            actes, plafond = collecter(client, wallet, args.max)
        except Exception as exc:  # noqa: BLE001
            print(f"{wallet} : illisible ({str(exc)[:80]})")
            continue
        rapport(wallet, actes, comptabiliser(actes), args.annonce, args.max,
                plafond)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
