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
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
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

# Avant Polymarket : borne basse du decoupage temporel. 2020-01-01 UTC.
DEBUT_POLYMARKET = 1577836800

# En dessous d'une heure, on cesse de couper. Une heure saturee a 5 000 actes
# signalerait un compte a plus d'un acte par seconde en continu -- si ca arrive,
# c'est un fait a publier, pas une fenetre a redecouper indefiniment.
FENETRE_MIN = 3600

# Pause entre deux fenetres. Le coût d'etre poli avec une API gratuite.
PAUSE = 0.25

# L'API publique, lue en direct. Le SDK plafonne ses pages a 100 lignes ; ici
# 500 passent, soit cinq fois moins de requetes pour le meme historique.
ACTIVITE = "https://data-api.polymarket.com/activity"
PAR_REQUETE = 500


class Acte:
    """Un acte d'activite, lu directement de l'API publique.

    ON NE PASSE PLUS PAR LE MODELE DU SDK. Mesure du 2026-08-30 : il refuse les
    `REDEEM` de certains portefeuilles (`side` et `asset` vides,
    `outcomeIndex: 999`) avec « TradeActivity response did not match expected
    shape », ce qui faisait echouer l'audit de comptes ENTIERS -- RN1, 4e du
    classement, disparaissait ainsi de l'etude. Pire : l'outil l'imputait a un
    delai reseau. Un verificateur qui perd des comptes en silence est
    exactement ce qu'il reproche aux captures d'ecran.

    Le JSON brut, lui, est parfaitement lisible, et `limit=500` rend CINQ FOIS
    plus d'actes par requete que les 100 du SDK -- ce qui divise d'autant la
    pression sur une API qui nous limitait.
    """

    __slots__ = ("type", "side", "shares", "amount", "price", "token_id",
                 "condition_id", "timestamp", "transaction_hash", "title")

    def __init__(self, brut: dict):
        self.type = (brut.get("type") or "").upper()
        self.side = (brut.get("side") or "") or None
        self.shares = brut.get("size")
        self.amount = brut.get("usdcSize")
        self.price = brut.get("price")
        # UN REDEEM N'A PAS DE JETON. Il arrive avec `asset` vide : le
        # remboursement porte sur le MARCHE, pas sur l'une de ses deux jambes.
        # On garde les deux identifiants separes pour pouvoir rattacher le
        # remboursement a la position qu'il solde -- sinon elle reste comptee
        # « ouverte » a jamais, et la page publie un chiffre faux.
        self.token_id = brut.get("asset") or None
        self.condition_id = brut.get("conditionId") or None
        self.transaction_hash = brut.get("transactionHash")
        self.title = brut.get("title") or ""
        horodatage = brut.get("timestamp")
        self.timestamp = (datetime.fromtimestamp(horodatage, timezone.utc)
                          if horodatage else None)


def _cle(acte) -> tuple:
    """Identifie un acte, sans le confondre avec son jumeau legitime."""
    return (getattr(acte, "transaction_hash", None), getattr(acte, "type", None),
            getattr(acte, "timestamp", None), getattr(acte, "token_id", None),
            str(getattr(acte, "shares", None)), str(getattr(acte, "amount", None)))


def _fenetre(client, wallet: str, debut: int, fin: int,
             essais: int = 4) -> tuple[list, bool]:
    """Lit UNE fenetre temporelle. Rend (actes, saturee).

    REESSAYER N'EST PAS DU CONFORT ICI. Lire un gros portefeuille en profondeur
    demande des milliers de requetes ; mesure du 2026-08-30 : 5 des 10 premiers
    du classement sont tombes en « read operation timed out », alors que les
    memes passaient seuls quelques minutes plus tot. Sans reprise, la moitie de
    l'echantillon disparait pour une raison qui n'a rien a voir avec les
    donnees -- et une etude amputee au hasard ne vaut rien.

    La fenetre est relue DEPUIS LE DEBUT a chaque essai : un lot partiel garde
    apres une coupure produirait un trou silencieux au milieu de l'historique,
    exactement le genre d'erreur que cet outil existe pour interdire.
    """
    for essai in range(essais):
        out, decalage = [], 0
        try:
            while decalage < PLAFOND_API:
                lot = client.get(ACTIVITE, params={
                    "user": wallet, "limit": PAR_REQUETE, "offset": decalage,
                    "start": debut, "end": fin,
                    # SANS CA, PAS DE DEPOTS -- donc pas de denominateur, donc
                    # rien. Le service met `excludeDepositsWithdrawals=true` par
                    # defaut et IGNORE un filtre de type qui les demande
                    # explicitement (`type=DEPOSIT` rend zero ligne). Mesure du
                    # 2026-08-30 : sans ce parametre, notre propre compte
                    # affichait 0,00 $ de depots au lieu de 8,01 $ -- l'outil
                    # perdait en silence la seule chose qu'il existe pour dire.
                    "excludeDepositsWithdrawals": "false"}, timeout=30).json()
                if not isinstance(lot, list):
                    raise RuntimeError(str(lot)[:120])
                out.extend(Acte(b) for b in lot)
                if len(lot) < PAR_REQUETE:
                    return out, False
                decalage += PAR_REQUETE
            return out, True  # le plafond de la fenetre est atteint
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if "offset" in message:
                return out, True
            if ("timed out" in message or "timeout" in message
                    or "read operation" in message) and essai < essais - 1:
                time.sleep(2 ** essai)
                continue
            raise
    return [], False


def collecter(client, wallet: str, limite: int) -> tuple[list, bool]:
    """Rend (actes, incomplet). Un `Paginator` itere des PAGES, pas des lignes
    -- piege mesure le 2026-08-20.

    LE PLAFOND DE 5 000 EST PAR FENETRE, PAS PAR PORTEFEUILLE. On a d'abord
    conclu l'inverse et on l'a ecrit publiquement : « 6 des 10 premiers sont
    structurellement invérifiables ». C'etait FAUX. Un membre du groupe
    Builders (fedoras, 2026-08-30) a donne la technique en quelques heures :
    « page inside start/end windows -- each window has its own offset budget ».
    Verifie le meme jour : janvier, mars et juin 2026 rendent chacun leurs
    actes sur un portefeuille que la lecture simple bloquait a 5 000.

    D'ou le decoupage adaptatif ci-dessous : une fenetre qui sature est coupee
    en deux, jusqu'a l'heure. L'historique complet devient atteignable, donc
    les gros portefeuilles deviennent auditables -- ce qui etait tout l'enjeu.

    LA LECON, plus chere que le bug : on a publie une impossibilite apres avoir
    epuise NOTRE facon de faire, pas toutes les facons de faire. Une limite
    qu'on n'a pas su contourner n'est pas une limite de la plateforme. Demander
    avant d'affirmer aurait coute un message ; l'affirmer a coute une
    correction publique.
    """
    fin = int(time.time())
    pile, actes, vus, incomplet = [(DEBUT_POLYMARKET, fin)], [], {}, False

    while pile and len(actes) < limite:
        debut, borne = pile.pop()
        # UNE API PUBLIQUE QUI NE NOUS DOIT RIEN. Mesure du 2026-08-30 : lire
        # trois gros portefeuilles d'affilee (65 000 actes) a fait tomber les
        # sept suivants en delai d'attente. Ce n'etait pas une panne, c'etait
        # une limitation de debit -- et insister ne la leve pas, ca l'aggrave.
        time.sleep(PAUSE)
        lot, saturee = _fenetre(client, wallet, debut, borne)
        if saturee and borne - debut > FENETRE_MIN:
            milieu = (debut + borne) // 2
            pile.extend([(debut, milieu), (milieu, borne)])
            continue
        if saturee:
            # Une heure entiere saturee : la, on renonce pour de bon.
            incomplet = True
        # LES FENETRES SE TOUCHENT AUX BORNES : un acte a la seconde de coupure
        # revient dans les deux moities et doublerait le PnL. Mais dedoublonner
        # naivement SOUS-COMPTE : un gros ordre rempli contre plusieurs teneurs
        # produit plusieurs actes au meme hash, meme jeton, meme taille -- des
        # lignes distinctes et legitimes. Mesure du 2026-08-30 : mintblade
        # tombait de 830 a 822 actes, soit 1 % de l'audit efface en silence.
        # D'ou le comptage par MULTIPLICITE : on garde, pour chaque cle, le
        # nombre maximal d'exemplaires vu dans UNE seule fenetre.
        groupes: dict = defaultdict(list)
        for a in lot:
            groupes[_cle(a)].append(a)
        for cle, exemplaires in groupes.items():
            manquants = len(exemplaires) - vus.get(cle, 0)
            if manquants > 0:
                vus[cle] = len(exemplaires)
                actes.extend(exemplaires[:manquants])
    # NE PAS CONFONDRE LES DEUX PLAFONDS. `incomplet` ne signale plus qu'une
    # saturation VRAIE (une heure pleine a 5 000 actes) ; atteindre `limite`
    # est notre propre garde-fou, que `--max` releve. Les melanger ferait
    # reapparaitre le « ce n'est pas notre limite » qu'on vient de retirer,
    # et republierait la meme erreur sous une autre forme.
    return actes[:limite], incomplet


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
    orphelins: list = []

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

        if t == "REDEEM" and not getattr(a, "token_id", None):
            # RATTACHEMENT DIFFERE. Un remboursement porte sur le MARCHE, pas
            # sur l'une de ses deux jambes : on ne saura a quelle position il
            # se rapporte qu'apres avoir lu tous les echanges. Le traiter tout
            # de suite creerait une position fantome et laisserait la vraie
            # ouverte pour toujours -- 6 soldees devenaient 5.
            orphelins.append((getattr(a, "condition_id", None), parts, montant,
                              getattr(a, "title", "") or ""))
            continue

        jeton = jetons[getattr(a, "token_id", None)]
        jeton["titre"] = jeton["titre"] or (getattr(a, "title", "") or "")
        jeton["condition"] = (jeton.get("condition")
                              or getattr(a, "condition_id", None))
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

    # SECOND PASSAGE : chaque remboursement rejoint la position qu'il solde.
    # On choisit, dans le meme marche, le jeton dont il reste le plus de parts
    # -- c'est celui qui a ete rembourse. Un remboursement qu'on ne sait pas
    # rattacher est GARDE sous sa propre cle plutot qu'ignore : perdre de
    # l'argent recu fausserait le plancher dans le bon sens, ce qui est pire
    # que de le fausser dans le mauvais.
    for condition, parts, montant, titre in orphelins:
        candidats = [(c, j) for c, j in jetons.items()
                     if j.get("condition") == condition
                     and j["achat_parts"] > j["vente_parts"] + j["redeem_parts"]]
        if candidats:
            cible = max(candidats, key=lambda cj: cj[1]["achat_parts"]
                        - cj[1]["vente_parts"] - cj[1]["redeem_parts"])[1]
        else:
            cible = jetons[f"redeem:{condition}"]
            cible["titre"] = cible["titre"] or titre
        cible["redeem_parts"] += parts
        cible["redeem_usdc"] += montant

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
            # MEME SEVERITE QUE LE PLAFOND DE L'API, parce que c'est la meme
            # ignorance. Le 2026-09-03 ce cas n'imprimait qu'un « ATTENTION »
            # au milieu du rapport puis annoncait « PLANCHER GARANTI : -16,3 %
            # ». Relance a 60000 actes sur le MEME portefeuille : 32 346 actes,
            # 74 jours au lieu de 11, plancher -4,2 %. Le premier chiffre etait
            # faux d'un facteur 4 et se donnait pour une garantie -- exactement
            # ce que cet outil existe pour denoncer.
            #
            # SEULE DIFFERENCE A CONSERVER : ce plafond-la est RATTRAPABLE.
            # Celui de l'API ne l'est pas, et y conseiller --max serait
            # conseiller une action impossible.
            print("\n  *** NON VERIFIABLE — HISTORIQUE TRONQUE PAR NOTRE "
                  "PROPRE PLAFOND. ***\n"
                  f"  La lecture s'est arretee a {limite} actes et "
                  "l'historique continue. Aucun\n  total, taux, ratio ni "
                  "plancher ci-dessous ne vaut pour ce compte : ils ne\n"
                  "  portent que sur la fenetre lue, qui est la plus RECENTE, "
                  "donc la moins\n  representative d'un historique long.\n"
                  f"  Relancer avec --max nettement au-dessus de {limite} "
                  "AVANT de citer un chiffre.")
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
    # `tronque` et non `plafond` : les DEUX troncatures interdisent le mot
    # « garanti », qu'elles viennent de l'API ou de notre propre limite.
    tronque = plafond or len(actes) >= limite
    titre = "PLANCHER GARANTI" if not tronque else \
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

    import httpx
    client = httpx.Client(headers={"accept": "application/json"})

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
