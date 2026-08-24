# -*- coding: utf-8 -*-
"""Compte les DISLOCATIONS sur les marches crypto 15 minutes. LECTURE SEULE.

    .venv/Scripts/python tools/guetter_dislocations.py --minutes 20

Aucun ordre ne part d'ici, jamais. L'outil observe et compte.

## Ce qu'il mesure, et pourquoi

Le depot public `MrFadiAi/Polymarket-bot` (538 etoiles) decrit une strategie
« DipArb » : guetter une chute de plus de 15 % en 3 secondes sur les marches
crypto a 15 minutes, acheter le creux, couvrir avec le cote oppose.

C'est la seule strategie rencontree qui ne se heurte PAS au mur mesure le
23/08 -- « la ou il y a du volume, l'ecart vaut un tick ; la ou il y a de
l'ecart, il n'y a personne ». Elle n'attend pas l'ecart : elle attend qu'un
vendeur presse vide le carnet. On est PRENEUR REACTIF, pas teneur en file.

Reste a savoir si ces dislocations existent vraiment. Ce projet a deja perdu
des journees sur des opportunites qui n'existaient que dans un tableau : les
28 000 % d'arbitrage du 28/07 (carnets vides), le +238 %/jour du 31/07 (unite
mal comprise), les 17,4 % d'ecart du 23/08 (carnet pas encore forme). On MESURE
avant d'engager un dollar.

## Ce qu'un resultat veut dire

- Des dislocations frequentes et amples -> DipArb est testable, et on a de quoi.
- Aucune, ou trop petites -> la piste se ferme pour ZERO dollar risque.

Un « zero » ici vaut autant qu'un signal : c'est ce qui evite de coder une
strategie pour un phenomene qui n'arrive jamais.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("guet")

GAMMA = "https://gamma-api.polymarket.com/markets"

# Un creneau dure 900 s. Les slugs sont deterministes : `btc-updown-15m-<ts>`
# ou `<ts>` est un multiple de 900. Decouvert le 24/08 en lisant les slugs
# historiques -- Gamma ne sait pas lister ces marches par tri.
CRENEAU_S = 900

# Seuil de la strategie decrite dans le depot. On compte AUSSI les plus petites
# pour ne pas rendre un zero qui cacherait un phenomene reel mais plus discret.
SEUIL_DIPARB = 0.15
PALIERS = (0.03, 0.05, 0.10, 0.15)

# Fenetre sur laquelle on mesure la chute. La strategie parle de 3 secondes ;
# on garde un peu plus large pour ne pas dependre du rythme d'echantillonnage.
FENETRE_S = 6.0

# Delai apres lequel on regarde si le prix est REVENU. C'est le seul chiffre
# qui distingue une dislocation (le prix decroche puis se reprend, il y a de
# quoi gagner) d'une reevaluation (le prix change et reste, il n'y a rien a
# prendre). Sans cette distinction, compter les chutes ne mesure que la
# volatilite du sous-jacent.
DELAI_RETOUR_S = 30.0


def marches_15m(session, avance: int = 4) -> list[dict]:
    """Les creneaux 15 min ouverts, du plus proche au plus lointain."""
    base = (int(time.time()) // CRENEAU_S) * CRENEAU_S
    slugs = [
        f"{actif}-updown-15m-{base + k * CRENEAU_S}"
        for k in range(0, avance)
        for actif in ("btc", "eth")
    ]
    try:
        reponse = session.get(
            GAMMA,
            params=[("slug", s) for s in slugs] + [("limit", "100")],
            timeout=25,
        )
        reponse.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("univers illisible : %s", exc)
        return []
    ouverts = []
    for m in reponse.json():
        if m.get("closed"):
            continue
        try:
            jetons = json.loads(m.get("clobTokenIds") or "[]")
        except ValueError:
            continue
        if jetons:
            ouverts.append({"slug": m.get("slug"), "jeton": jetons[0],
                            "fin": m.get("endDate")})
    return ouverts


def main() -> int:
    from dotenv import load_dotenv
    import httpx
    from polymarket import SecureClient

    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=20.0)
    parser.add_argument("--intervalle", type=float, default=2.0,
                        help="secondes entre deux releves (defaut 2)")
    parser.add_argument("--marches", type=int, default=4,
                        help="nombre de marches suivis simultanement")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )
    session = httpx.Client()

    print("=" * 70)
    print("GUET DES DISLOCATIONS -- marches crypto 15 minutes")
    print("LECTURE SEULE : aucun ordre ne part d'ici.")
    print("=" * 70)

    chemin = os.path.join(os.environ.get("TEMP", "."), "dislocations.csv")
    journal = open(chemin, "w", newline="", encoding="utf-8")
    plume = csv.writer(journal)
    plume.writerow(["horodatage", "slug", "bid", "ask", "variation_pct"])

    # Historique par jeton : [(instant, milieu)]
    histoire: dict[str, list] = {}
    compte = {p: 0 for p in PALIERS}
    releves = 0
    creux: list[dict] = []
    temoins: list[dict] = []
    pire = 0.0
    pire_quoi = ""
    debut = time.monotonic()
    suivis: list[dict] = []
    dernier_scan = 0.0

    try:
        while time.monotonic() - debut < args.minutes * 60:
            maintenant = time.monotonic()
            # Les creneaux expirent toutes les 15 min : on rafraichit la liste.
            if maintenant - dernier_scan > 60:
                suivis = marches_15m(session)[: args.marches]
                dernier_scan = maintenant
                if suivis:
                    logger.info("suivi de %d marche(s) : %s", len(suivis),
                                ", ".join(str(m["slug"])[-14:] for m in suivis))
            if not suivis:
                time.sleep(args.intervalle)
                continue

            for m in suivis:
                try:
                    carnet = client.get_order_book(token_id=m["jeton"])
                except Exception:  # noqa: BLE001
                    continue
                if not carnet.bids or not carnet.asks:
                    continue
                bid = float(carnet.bids[-1].price)
                ask = float(carnet.asks[-1].price)
                milieu = (bid + ask) / 2.0
                if milieu <= 0:
                    continue
                releves += 1

                serie = histoire.setdefault(m["jeton"], [])
                serie.append((maintenant, milieu))
                # On ne garde que la fenetre utile.
                while serie and maintenant - serie[0][0] > FENETRE_S:
                    serie.pop(0)

                # LES CHUTES SEULEMENT, et ce n'est pas un detail. La premiere
                # version comptait `abs(variation)` : elle a rendu 71 « disloca-
                # tions » en 8 min dont les plus fortes etaient des HAUSSES
                # (+62 %, +135 %). DipArb achete un creux ; une hausse n'est pas
                # une occasion d'acheter, c'est le train qui part.
                if len(serie) >= 2:
                    reference = serie[0][1]
                    variation = (milieu - reference) / reference
                    if variation < pire:
                        pire, pire_quoi = variation, str(m["slug"])[-14:]
                    for palier in PALIERS:
                        if -variation >= palier:
                            compte[palier] += 1
                    if -variation >= PALIERS[0]:
                        # LE SEUL CHIFFRE QUI COMPTE : le prix REVIENT-IL ?
                        # Une chute qui ne se reprend pas n'est pas une
                        # dislocation, c'est une reevaluation -- le nouveau prix
                        # EST le bon prix, et acheter le creux revient a
                        # rattraper un couteau qui tombe. On enregistre le
                        # creux, et une passe posterieure mesurera le retour.
                        creux.append({
                            "instant": maintenant,
                            "jeton": m["jeton"],
                            "slug": m["slug"],
                            "avant": reference,
                            "bas": milieu,
                            "chute": variation,
                            "apres": None,
                            # LES PRIX D'EXECUTION, pas le milieu. On achete a
                            # l'ASK et on revend au BID : a 0,50 avec un tick de
                            # 0,01, l'aller-retour coute ~2 % avant tout gain.
                            # Mesurer sur le milieu ferait disparaitre ce cout
                            # et rendrait profitable une strategie qui ne l'est
                            # pas -- exactement l'erreur du 31/07 (+238 %/jour).
                            "achat": ask,
                            "vente": None,
                        })
                        plume.writerow([
                            datetime.now(timezone.utc).isoformat(),
                            m["slug"], f"{bid:.4f}", f"{ask:.4f}",
                            f"{100 * variation:.2f}",
                        ])
                        journal.flush()
                        logger.info("CHUTE %s  %+.1f %%  (%.3f -> %.3f)",
                                    str(m["slug"])[-14:], 100 * variation,
                                    reference, milieu)

                # Le retour se mesure APRES coup, sur les creux assez vieux.
                for c in creux:
                    if c["apres"] is None and c["jeton"] == m["jeton"] \
                            and maintenant - c["instant"] >= DELAI_RETOUR_S:
                        c["apres"] = milieu
                        # LE PRIX DE VENTE REEL. On revend au BID, jamais au
                        # milieu : a 0,50 avec un tick de 0,01, l'aller-retour
                        # coute deja ~2 % avant le moindre gain. Mesurer sur le
                        # milieu ferait disparaitre ce cout et rendrait
                        # profitable une strategie qui ne l'est pas.
                        c["vente"] = bid

                # LE TEMOIN, et sans lui la mesure ne vaut rien. Un prix qui
                # oscille remonte apres la moitie de ses baisses : « le creux
                # se reprend » serait vrai sur du bruit pur. On simule donc le
                # MEME aller-retour a des instants quelconques. Si les creux ne
                # battent pas le hasard, DipArb ne capture rien.
                if releves % 7 == 0:
                    temoins.append({"jeton": m["jeton"], "instant": maintenant,
                                    "achat": ask, "vente": None})
                for t in temoins:
                    if t["vente"] is None and t["jeton"] == m["jeton"] \
                            and maintenant - t["instant"] >= DELAI_RETOUR_S:
                        t["vente"] = bid
            time.sleep(args.intervalle)
    except KeyboardInterrupt:
        print("\ninterrompu.")
    finally:
        journal.close()
        session.close()

    duree = (time.monotonic() - debut) / 60
    print()
    print("=" * 70)
    print(f"{releves} releves en {duree:.1f} min sur {len(suivis)} marche(s)")
    print(f"fenetre de mesure : {FENETRE_S:.0f} s")
    print()
    for palier in PALIERS:
        marque = "  <-- seuil DipArb" if palier == SEUIL_DIPARB else ""
        print(f"  variations >= {100 * palier:>5.0f} % : {compte[palier]:>5}{marque}")
    print()
    print(f"plus forte CHUTE observee : {100 * pire:+.2f} %  ({pire_quoi})")
    mesures = [c for c in creux if c["apres"] is not None]
    if mesures:
        repris = 0
        somme = 0.0
        for c in mesures:
            # part du creux effacee : 0 = pas de retour, 1 = retour complet
            ecart = c["avant"] - c["bas"]
            part = ((c["apres"] - c["bas"]) / ecart) if ecart > 0 else 0.0
            somme += part
            if part >= 0.5:
                repris += 1
        print()
        print(f"RETOUR APRES CHUTE ({DELAI_RETOUR_S:.0f} s plus tard, "
              f"{len(mesures)} creux suivis) :")
        print(f"  reprise moyenne du creux : {100 * somme / len(mesures):.0f} %")
        print(f"  creux repris a moitie ou plus : {repris}/{len(mesures)}")
        print("  (0 % = le prix reste bas, la chute etait une REEVALUATION ;")
        print("   100 % = le prix revient, c'est une vraie DISLOCATION)")

        import statistics as st

        def bilan(lot, nom):
            nets = [100 * (c["vente"] - c["achat"]) / c["achat"]
                    for c in lot if c.get("vente") and c.get("achat")]
            if not nets:
                print(f"\n{nom} : aucun aller-retour complet.")
                return None
            gagnants = sum(1 for x in nets if x > 0)
            print(f"\n{nom} ({len(nets)} allers-retours simules)")
            print(f"   median  {st.median(nets):+6.2f} %   moyen {st.fmean(nets):+6.2f} %")
            print(f"   gagnants {gagnants}/{len(nets)} = {100*gagnants/len(nets):.0f} %")
            return st.median(nets)

        print()
        print("=" * 70)
        print("ACHAT A L'ASK, REVENTE AU BID -- le seul chiffre qui decide")
        print("=" * 70)
        med_creux = bilan(creux, "APRES UNE CHUTE")
        med_temoin = bilan(temoins, "TEMOIN (instants quelconques)")
        if med_creux is not None and med_temoin is not None:
            print()
            print(f"ECART CREUX - TEMOIN : {med_creux - med_temoin:+.2f} points")
            if med_creux <= 0:
                print("  Acheter les creux PERD de l'argent une fois l'ecart paye.")
                print("  DipArb ne tient pas sur ces marches.")
            elif med_creux <= med_temoin:
                print("  Les creux ne font pas mieux que des instants au hasard :")
                print("  ce qu'on prenait pour un signal est du bruit.")
            else:
                print("  Les creux battent le hasard ET couvrent l'ecart.")
                print("  A confirmer sur une seance plus longue avant d'engager.")
    else:
        print()
        print(f"aucun creux n'a pu etre suivi {DELAI_RETOUR_S:.0f} s -- rallonger la seance.")
    print(f"journal : {chemin}")
    print()
    # Le bloc ci-dessous ne juge que la FREQUENCE des chutes. Il a rendu
    # « DipArb devient testable » dans la meme sortie ou la simulation
    # d'aller-retour concluait l'inverse : deux verdicts opposes a l'ecran.
    # C'est la simulation qui tranche -- compter des chutes ne dit rien de ce
    # qu'elles rapportent. On ne garde donc ici qu'un constat de frequence.
    if compte[SEUIL_DIPARB] == 0:
        # Un zero est un RESULTAT, a condition d'avoir vraiment regarde.
        if releves < 100:
            print("ATTENTION : trop peu de releves pour conclure. Rallonger.")
        else:
            print("AUCUNE dislocation au seuil de la strategie sur cette")
            print("periode. Ce n'est pas une panne : c'est la mesure. Coder")
            print("DipArb reviendrait a guetter un phenomene qui n'arrive pas.")
    else:
        print(f"{compte[SEUIL_DIPARB]} chutes au seuil DipArb sur la periode.")
        print("La FREQUENCE ne dit rien du GAIN : voir la simulation ci-dessus,")
        print("c'est elle qui tranche.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
