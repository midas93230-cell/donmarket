# -*- coding: utf-8 -*-
"""Teste EN REEL ce que rapportent les recompenses de liquidite a petit capital.

    .venv/Scripts/python tools/tester_recompenses.py --bankroll 4
    .venv/Scripts/python tools/tester_recompenses.py --bankroll 4 --arm

Sans `--arm`, rien ne part : l'outil selectionne, chiffre et affiche.

## Pourquoi cet outil existe alors que `cli.py rewards` existe deja

`cli.py rewards` lit l'univers par **Gamma, qui plafonne a offset=2100** et trie
par volume decroissant. Il ne voit donc que les GROS marches -- ceux dont le
seuil d'entree est eleve -- et conclut « 0 finançable » a 4 $. Mesure du
2026-08-22 : `list_current_rewards()` cote CLOB rend **16 418** marches
recompenses sans cette limite, dont **13 567 a 20 parts** de seuil. Le
« ticket ~100 $ » qui a fait ecarter les recompenses depuis juillet portait sur
199 marches sur 16 418.

## Ce que l'outil mesure, et ce qu'il refuse de pretendre

Il ne PREDIT aucun rendement. L'unite de `market_competitiveness` est inconnue :
si elle ne correspond pas au score `S(v,s)=((v-s)/v)^2*b`, tout calcul de part
captee est faux. Ce projet s'est deja trompe deux fois sur exactement ce genre
d'unite (arbitrage a 28 000 %, +238 %/jour du 31/07). L'outil pose donc un
ticket reel et laisse `get_total_earnings_for_user_for_day` trancher le
lendemain -- un chiffre mesure contre une estimation.

## Le piege de placement, et pourquoi on rejoint le meilleur bid

Mesure du 31/07 : un ordre poste a `m - v/2` DEPLACE le milieu dont il mesure sa
distance, et la distance finale vaut `(A-B)/2 + v/2`. On ne marque alors que si
l'ecart du carnet tient dans trois bandes.

On evite le probleme au lieu de le modeliser : on rejoint le **meilleur bid
existant**. A ce prix il y a deja de la liquidite, donc notre ordre ne deplace
pas le milieu, et notre distance vaut exactement `(A-B)/2`. Marquer se reduit
alors a `ecart <= 2 * bande`, un critere verifiable sans simulation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("recompenses")

# Taille de l'echantillon scanne. Chaque candidat coute deux appels (rewards +
# carnet) : au-dela de quelques centaines le scan devient plus long que la
# decision qu'il eclaire.
TAILLE_ECHANTILLON = 220

# On rejoint le meilleur bid, donc la distance vaut (A-B)/2 : marquer exige
# `(A-B)/2 <= bande`, soit un ecart d'au plus DEUX bandes. Voir le docstring.
BANDES_MAX = 2.0

# Heures minimales avant resolution. Une position de fourniture de liquidite
# doit survivre a la nuit pour qu'on lise ses gains le lendemain ; un marche qui
# se resout entre-temps ne mesure rien et transforme le ticket en pari.
HEURES_MIN = 24.0

# Bande de prix ou le bareme ne penalise pas, et ou une bande de quelques cents
# garde un sens. Memes bornes que `making/core` -- meme raison de fond.
MIN_PRIX = 0.10
MAX_PRIX = 0.90


def heures_restantes(fin, maintenant) -> float | None:
    """Heures avant resolution, ou None si la date est illisible.

    None fait ECARTER : supposer « c'est loin » ferait poster precisement sur
    les marches dont on ignore la fermeture. Meme regle que `making/core`.
    """
    if fin is None:
        return None
    if isinstance(fin, str):
        # Gamma rend l'ISO-8601 avec un « Z » que `fromisoformat` refuse
        # avant Python 3.11 et accepte mal selon les variantes.
        try:
            fin = datetime.fromisoformat(fin.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(fin, datetime):
        try:
            fin = datetime(fin.year, fin.month, fin.day, tzinfo=timezone.utc)
        except AttributeError:
            return None
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=timezone.utc)
    return (fin - maintenant).total_seconds() / 3600.0


def meilleur(niveaux):
    """Les carnets Polymarket arrivent PIRE PRIX EN PREMIER : le meilleur est
    en derniere position (mesure le 2026-07-26). Lire `[0]` donnerait le pire."""
    return float(niveaux[-1].price) if niveaux else None


def selectionner(client, bankroll: float, taille: int = TAILLE_ECHANTILLON,
                 prix_min: float = MIN_PRIX):
    """Rend les candidats finançables ET marquables, les mieux dotes d'abord."""
    from donmarket.analysis.scoring import distance_after_posting, order_score
    from donmarket.making.runner import flatten

    pool = [
        x
        for x in flatten(client.list_current_rewards())
        if float(x.rewards_min_size or 0) > 0 and float(x.total_daily_rate or 0) > 0
    ]
    logger.info("%d marches recompenses actifs (source CLOB, sans limite Gamma)", len(pool))

    random.seed(11)
    echantillon = random.sample(pool, min(taille, len(pool)))

    candidats: list[dict] = []
    motifs = {"trop cher": 0, "carnet trop large": 0, "carnet incomplet": 0,
              "illisible": 0}

    for brut in echantillon:
        try:
            lignes = [i for p in client.list_market_rewards(condition_id=brut.condition_id)
                      for i in p.items]
            if not lignes or not lignes[0].tokens:
                motifs["illisible"] += 1
                continue
            m = lignes[0]
            bande = float(m.rewards_max_spread or 0) / 100.0
            parts = float(m.rewards_min_size)
            jeton = min(m.tokens, key=lambda t: float(t.price))
            prix = float(jeton.price)
            if not (0.0 < prix < 1.0):
                motifs["illisible"] += 1
                continue
            # PRIX EXTREMES ECARTES. Deux raisons, et la seconde est la vraie.
            # 1) ATTENTION AU SENS DU BAREME -- je l'avais ecrit A L'ENVERS ici
            #    le 22/08, et ça a coute un ticket condamne d'avance. Le `/3`
            #    n'est pas une penalite hors bande : c'est la CLEMENCE accordee
            #    DANS [0,10 ; 0,90], celle qui autorise a marquer en ne cotant
            #    qu'un seul cote. DEHORS, `Qmin = min(Qone, Qtwo)` s'applique
            #    strictement, donc un cote unique vaut exactement ZERO.
            #    Le filtre qui compte est celui sur le MILIEU, plus bas.
            # 2) Une bande de 4,5 cents autour d'un prix de 1 cent ne veut rien
            #    dire : le milieu ne peut pas descendre sous zero. Le scan du
            #    22/08 remontait « NVIDIA Q2 gross margin 78%+ » a 0,010, pool
            #    86 $/j, concurrence 0,00 -- un marche que plus personne ne cote
            #    parce que la reponse est acquise. Concurrence nulle n'y signale
            #    pas une aubaine mais un desert, exactement comme l'ecart de
            #    28 000 % du 28/07 signalait un carnet vide.
            if not (prix_min <= prix <= MAX_PRIX):
                motifs["prix extreme"] = motifs.get("prix extreme", 0) + 1
                continue
            if parts * prix > bankroll:
                motifs["trop cher"] += 1
                continue

            carnet = client.get_order_book(token_id=jeton.token_id)
            bid, ask = meilleur(carnet.bids), meilleur(carnet.asks)
            if bid is None or ask is None:
                motifs["carnet incomplet"] += 1
                continue
            if (ask - bid) / 2.0 > bande:
                motifs["carnet trop large"] += 1
                continue
            # LE MEME GARDE-FOU, SUR LE PRIX REELLEMENT POSTE. Le filtre plus
            # haut porte sur le prix du jeton ; l'ordre part au meilleur BID,
            # qui est plus bas. Le 22/08 l'ecart a suffi a poser un ticket a
            # 0,065 alors que la borne annoncee etait 0,10 -- un garde-fou qui
            # controle une valeur et en laisse partir une autre ne garde rien.
            if not (prix_min <= bid <= MAX_PRIX):
                motifs["bid hors bande"] = motifs.get("bid hors bande", 0) + 1
                continue

            # LE MILIEU, ET NON LE PRIX DU JETON, DECIDE DE LA CLEMENCE.
            # Formule relevee dans `analysis/scoring` :
            #   Qmin = max(min(Qone,Qtwo), max(Qone,Qtwo)/3)  si milieu in [0,10;0,90]
            #   Qmin = min(Qone, Qtwo)                        sinon
            # Le /3 n'est PAS une penalite hors bande -- c'est la CLEMENCE qui
            # permet de marquer en ne cotant qu'UN SEUL cote. Dehors, c'est le
            # `min` strict : un seul cote donne exactement ZERO.
            #
            # Mesure du 23/08 : le ticket « McCaffrey » pose a 0,050 avait un
            # milieu de 0,085. Hors bande, donc Qmin = min(q, 0) = 0. Il etait
            # condamne des la pose, et il a effectivement rendu 0 sur la
            # journee. J'avais ecrit ce commentaire A L'ENVERS la veille.
            milieu = (bid + ask) / 2.0
            if not (MIN_PRIX <= milieu <= MAX_PRIX):
                motifs["milieu hors bande (score nul)"] = (
                    motifs.get("milieu hors bande (score nul)", 0) + 1
                )
                continue

            # LE SCORE REEL, calcule par le module qui sait le faire. Les
            # filtres ci-dessus sont des approximations ; `order_score` tient
            # compte du deplacement du milieu par notre propre ordre -- le
            # piege du 31/07, qui avait produit un « +238 %/jour » imaginaire.
            distance = distance_after_posting(bid, ask, bid)
            if distance is None:
                motifs["carnet incomplet"] += 1
                continue
            score = order_score(parts, distance, bande)
            if score <= 0.0:
                motifs["score nul"] = motifs.get("score nul", 0) + 1
                continue

            candidats.append({
                "condition_id": brut.condition_id,
                "token_id": jeton.token_id,
                "slug": getattr(m, "market_slug", None),
                "question": m.question,
                "issue": getattr(jeton, "outcome", "?"),
                "prix_bid": bid,
                "ecart": ask - bid,
                "bande": bande,
                "parts": parts,
                "cout": parts * bid,
                "milieu": milieu,
                "score": score,
                "taux_jour": float(brut.total_daily_rate),
                "concurrence": float(m.market_competitiveness or 0.0),
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("candidat ecarte : %s", exc)
            motifs["illisible"] += 1

    # LA CONCURRENCE D'ABORD, le pool ensuite. Ce n'est pas le classement d'une
    # strategie, c'est celui d'un TEST : on veut le cas le PLUS FAVORABLE et le
    # moins cher, parce qu'un rendement nul dans ces conditions-la condamne la
    # piste entiere, alors qu'un rendement nul sur un marche dispute n'apprend
    # rien. Le scan du 22/08 illustre l'ecart : trier par pool remontait Taipei
    # (20 $/j, ticket 3,60 $, concurrence 30,8) devant un marche a 19 $/j,
    # ticket 0,44 $ et concurrence 0,7 -- huit fois moins cher, quarante fois
    # moins dispute, pour le meme pool.
    #
    # On ne calcule toujours AUCUN rendement : l'unite de `concurrence` n'est
    # pas etablie. Comparer deux concurrences entre elles reste licite, en
    # deduire une part captee ne l'est pas.
    candidats.sort(key=lambda c: (c["concurrence"], -c["taux_jour"]))

    # L'ECHEANCE EN DERNIER, et seulement sur la tete de liste. Elle coute un
    # appel de plus par candidat (`list_market_rewards` ne la porte pas), donc
    # on ne la paye que pour ceux qu'on pourrait reellement retenir.
    #
    # Ce filtre n'est pas cosmetique : le premier candidat du scan du 22/08
    # etait un marche sismique fermant LE LENDEMAIN. Poster dessus aurait
    # transforme le ticket en pari et rendu la mesure du lendemain illisible --
    # on aurait mesure une resolution, pas une recompense.
    maintenant = datetime.now(timezone.utc)
    tete = candidats[:40]

    # LECTURE EN LOT, et non un appel par candidat. Le premier jet faisait un
    # `get_market(slug=...)` par marche : sur 61 candidats il a fini en
    # TransportError (read timeout), et comme l'echec retombait sur le meme
    # chemin que « echeance trop proche », l'outil a annonce « 0 candidat ».
    # Une panne reseau ne doit JAMAIS se lire comme un verdict sur les donnees.
    # Gamma via le SDK rend `TransportError: read operation timed out` de façon
    # reproductible ici (mesure du 22/08, sur `get_market` comme sur
    # `list_markets`). On interroge donc Gamma en direct, avec un delai
    # confortable -- c'est la meme API, seul le client change.
    import httpx

    echeances: dict[str, object] = {}
    with httpx.Client(timeout=30.0) as http:
        for depart in range(0, len(tete), 20):
            tranche = tete[depart:depart + 20]
            for essai in range(2):
                try:
                    reponse = http.get(
                        "https://gamma-api.polymarket.com/markets",
                        params=[("condition_ids", c["condition_id"]) for c in tranche]
                        + [("limit", "100")],
                    )
                    reponse.raise_for_status()
                    for ligne in reponse.json():
                        cid = ligne.get("conditionId")
                        if cid:
                            echeances[cid] = ligne.get("endDate")
                    break
                except Exception as exc:  # noqa: BLE001
                    if essai:
                        logger.warning("echeances illisibles pour %d marches : %s",
                                       len(tranche), exc)

    retenus: list[dict] = []
    for c in tete:
        if len(retenus) >= 10:
            break
        if c["condition_id"] not in echeances:
            # Ni retenu ni compte comme « trop proche » : on ne sait pas.
            motifs["echeance illisible"] = motifs.get("echeance illisible", 0) + 1
            continue
        restantes = heures_restantes(echeances[c["condition_id"]], maintenant)
        if restantes is None:
            motifs["echeance illisible"] = motifs.get("echeance illisible", 0) + 1
            continue
        if restantes < HEURES_MIN:
            motifs["echeance trop proche"] = motifs.get("echeance trop proche", 0) + 1
            continue
        c["heures"] = restantes
        retenus.append(c)
    return retenus, motifs, len(echantillon)


def main() -> int:
    from dotenv import load_dotenv
    from polymarket import SecureClient

    from donmarket.builder.attribution import order_attribution
    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll", type=float, required=True)
    parser.add_argument(
        "--echantillon", type=int, default=TAILLE_ECHANTILLON,
        help=(
            "marches a sonder. LE DEFAUT EST PETIT DEVANT L UNIVERS : 220 sur "
            "plus de 16 000. Conclure « aucun candidat » sur cet echantillon "
            "est une faute -- c est 1,4 % du gisement. Elargir des qu on "
            "cherche un ticket sous un budget serre."
        ),
    )
    parser.add_argument(
        "--prix-min", type=float, default=MIN_PRIX,
        help=(
            "prix plancher du ticket (defaut 0,10). SOUS 0,10 LE BAREME "
            "PENALISE `Qmin` PAR TROIS -- c'est un arbitrage assume, pas un "
            "reglage neutre : on accepte le tiers du score pour deployer du "
            "capital qui dormirait sinon. Le cout d'entree vaut 20 parts x ce "
            "prix, donc 0,10 impose un ticket d'au moins 2,00 $."
        ),
    )
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )

    print("=" * 74)
    print("RECOMPENSES DE LIQUIDITE -- TEST A PETIT CAPITAL")
    print("=" * 74)

    candidats, motifs, taille = selectionner(
        client, args.bankroll, args.echantillon, args.prix_min
    )

    print(f"\nEchantillon    : {taille} marches")
    print(f"Retenus        : {len(candidats)}  (finançables a {args.bankroll:.2f} $ ET marquables)")
    print(f"Ecartes        : {motifs}")

    if not candidats:
        # NE JAMAIS ANNONCER UN VERDICT QUAND ON A ETE AVEUGLE.
        # Le 22/08, un scan a rendu « aucun candidat » alors que QUARANTE
        # marches avaient passe tous les filtres metier et n'avaient echoue
        # que sur une panne DNS a la lecture de leur echeance. Le message
        # affirmait « ce n'est pas une panne, c'est le resultat » : c'etait
        # faux, et exactement l'erreur que cet outil est cense ne pas commettre.
        aveugles = motifs.get("echeance illisible", 0) + motifs.get("illisible", 0)
        if aveugles:
            print(f"\nAucun candidat RETENU, mais {aveugles} marche(s) n'ont pas "
                  f"pu etre lus (reseau).")
            print("CE N'EST DONC PAS UN RESULTAT -- c'est un scan incomplet.")
            print("Relancer avant d'en conclure quoi que ce soit.")
            return 2
        print("\nAucun candidat. Ce n'est pas une panne, c'est le resultat.")
        return 1

    print(f"\n{'$/jour':>7} {'concur':>9} {'cout':>7} {'ecart':>7} {'bande':>7}  question")
    for c in candidats[:10]:
        print(f"{c['taux_jour']:>7.1f} {c['concurrence']:>9.2f} {c['cout']:>7.2f} "
              f"{c['ecart']:>7.3f} {c['bande']:>7.3f}  {c['question'][:40]}")

    choix = candidats[0]
    print("\n" + "-" * 74)
    print("CANDIDAT RETENU")
    print(f"  {choix['question']}")
    print(f"  issue « {choix['issue']} »  |  {choix['parts']:.0f} parts @ {choix['prix_bid']:.3f} "
          f"= {choix['cout']:.2f} $")
    print(f"  pool {choix['taux_jour']:.1f} $/jour  |  concurrence mesuree {choix['concurrence']:.2f}")
    print("-" * 74)
    print("\nAUCUN RENDEMENT N'EST ANNONCE ICI. L'unite de `concurrence` n'est pas")
    print("etablie, donc toute part captee serait un chiffre invente. Le ticket")
    print("est pose pour que la MESURE de demain tranche :")
    print("  get_total_earnings_for_user_for_day(date='AAAA-MM-JJ')")

    # Annoncee avant le desarmement : sinon l'etat d'attribution n'est
    # lisible qu'en engageant reellement le ticket.
    attribution = order_attribution()
    print("attribution : " + attribution.phrase)

    if not args.arm:
        print("\nDESARME -- rien n'a ete envoye. Ajouter --arm pour poser le ticket.")
        return 0

    # Tout en mot-cle : `place_limit_order` refuse le positionnel (mesure du
    # 22/08, meme famille de pieges que `get_order_book(token_id=...)`).
    recu = client.place_limit_order(
        token_id=choix["token_id"],
        price=choix["prix_bid"],
        size=choix["parts"],
        side="BUY",
        # Un ticket de recompense doit RESTER au carnet : traverser l'ecart
        # nous ferait preneur, donc payeur, et supprimerait la liquidite qu'on
        # est justement paye pour fournir.
        post_only=True,
        # DEUX REMUNERATIONS DISTINCTES, et cet outil n'en cherchait qu'une.
        # Les recompenses de liquidite se comptent sur la PRESENCE au carnet ;
        # les frais builder se comptent sur le VOLUME ROUTE. Un ticket pose
        # sans `builder_code` peut donc marquer des points de recompense en
        # ne rapportant rien du cote builder -- et la mesure du lendemain,
        # `get_total_earnings_for_user_for_day`, ne montre que la premiere
        # moitie. C'est ce qui rendait le zero builder credible.
        builder_code=attribution.code,
    )
    ok = bool(getattr(recu, "success", getattr(recu, "ok", False)))
    print(f"\nORDRE POSE : ok={ok}  {getattr(recu, 'order_id', '?')}")
    if not ok:
        print(f"  refuse : {recu}")
        return 1

    journal = {
        "pose_le": datetime.now(timezone.utc).isoformat(),
        "jour_a_mesurer": datetime.now(timezone.utc).date().isoformat(),
        **{k: choix[k] for k in
           ("condition_id", "token_id", "question", "issue", "prix_bid",
            "parts", "cout", "taux_jour", "concurrence")},
    }
    chemin = os.path.join(os.environ.get("TEMP", "."), "recompense-test.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)
    print(f"  journal : {chemin}")
    print("\nA FAIRE DEMAIN : lire les gains reels du jour, et les comparer au pool.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
