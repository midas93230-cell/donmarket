# -*- coding: utf-8 -*-
"""Mesure la SANTE des carnets Polymarket et publie `docs/health.html`.

    .venv/Scripts/python tools/sante_carnets.py
    .venv/Scripts/python tools/sante_carnets.py --marches 400

Lecture seule : aucune cle, aucune authentification, aucun ordre. Tout vient
des API publiques, donc n'importe qui peut reproduire le chiffre.

## Le probleme que personne ne resout

Avant de poser un ordre sur Polymarket, rien ne dit si le carnet est VIVANT.
L'interface affiche un prix et un ecart ; elle ne dit pas que l'ecart de 19 %
qu'on regarde est large parce que personne n'y echange, ni que le ticket
minimal depasse le capital, ni que les deux cotes sont si desequilibres qu'on
sera rempli d'un cote et jamais de l'autre.

Ces quatre pieges nous ont coute cinq jours et de l'argent reel :
  - un ordre au meilleur bid a passe QUATORZE HEURES sans un remplissage, sur
    un carnet qui avait pourtant de la profondeur des deux cotes ;
  - un ecart de 17,4 % s'est revele etre un carnet PAS ENCORE FORME, referme a
    2 % en une heure des l'arrivee des vrais teneurs ;
  - des marches « a 86 $/jour de recompenses et concurrence nulle » etaient des
    marches morts dont la reponse etait acquise ;
  - une position de 2,15 $ est devenue INVENDABLE pour six milliemes de part
    sous le minimum d'ordre.

## Le fait central que cette page publie

MESURE DU 23/08, six marches au meme instant : la ou il y a du volume, l'ecart
vaut UN TICK ; le seul ecart large etait sur le marche que personne ne trade.
**Un ecart n'est pas une occasion a saisir avant les autres : c'est le prix de
l'absence de contrepartie.** Cette page rend ce fait visible marche par marche,
au lieu de laisser chacun le redecouvrir a ses frais.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("sante")

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"

# Volume 24 h sous lequel un carnet est repute ENDORMI. Mesure du 21/08 : un
# ordre a passe 14 h au meilleur bid sur un marche a moins de 500 $/jour.
VOLUME_ENDORMI = 500.0

# Parts au meilleur prix en dessous desquelles il n'y a pas de contrepartie
# reelle -- on ne peut ni etre rempli, ni ressortir.
PROFONDEUR_MINCE = 20.0

# Au-dela, l'ecart ne signale plus une occasion mais une absence de cotation.
ECART_SUSPECT = 0.10


def verdict(ecart_rel, volume, prof_bid, prof_ask, prix):
    """Rend (code, phrase). Le code sert au tri et a la couleur."""
    if prix is None or not (0.0 < prix < 1.0):
        return "mort", "prix hors bande, marche resolu ou sans cotation"
    if prof_bid < PROFONDEUR_MINCE and prof_ask < PROFONDEUR_MINCE:
        return "mort", "aucune contrepartie des deux cotes"
    if volume < VOLUME_ENDORMI and ecart_rel >= ECART_SUSPECT:
        # LE PIEGE PRINCIPAL, et le plus tentant : un gros ecart sur un marche
        # que personne ne trade. Il paie sur le papier et ne se remplit jamais.
        return "piege", f"ecart large ({100 * ecart_rel:.0f} %) mais {volume:,.0f} $ de volume : personne pour vous servir"
    if volume < VOLUME_ENDORMI:
        return "lent", f"seulement {volume:,.0f} $ echanges en 24 h -- attente longue"
    if prof_bid < PROFONDEUR_MINCE or prof_ask < PROFONDEUR_MINCE:
        cote = "achat" if prof_bid < prof_ask else "vente"
        return "desequilibre", f"cote {cote} presque vide : rempli d'un cote, coince de l'autre"
    if ecart_rel <= 0.025:
        return "efficient", "carnet serre et actif : rien a capturer, mais on entre et on sort"
    return "tradable", f"ecart {100 * ecart_rel:.1f} % avec volume et profondeur des deux cotes"


ORDRE = {"tradable": 0, "efficient": 1, "desequilibre": 2, "lent": 3,
         "piege": 4, "mort": 5}

# Dossier des releves quotidiens. C'EST LA SEULE CHOSE QUE PERSONNE D'AUTRE
# N'AURA. Struct, Polynode et Snag vendent de la donnee brute et de la vitesse ;
# on ne peut pas rivaliser la-dessus et on n'a pas a essayer. Ce qu'on fait
# qu'ils ne font pas, c'est rendre un VERDICT -- et un verdict ne vaut vraiment
# que dans la duree : « ce marche est mort depuis six jours » a une valeur
# qu'aucun instantane n'a. Personne ne peut rattraper cet historique
# retroactivement, parce qu'il faut avoir mesure les verdicts jour apres jour.
HISTORIQUE = "docs/history"

# CE QUE L'ARCHIVE GARDE DE CHAQUE CARNET, CHAQUE JOUR. Elle n'a longtemps
# retenu que le verdict, en jetant le soir meme les colonnes qui le JUSTIFIENT.
# C'etait une perte seche : le verdict dit qu'un carnet etait mort, les prix
# disent a quoi ressemblait un carnet VIVANT ce jour-la -- donc a quelle
# frequence le montage d'entree apparait, la question qui decide s'il faut
# continuer a en chercher un. Aucune de ces deux reponses ne se rattrape apres
# coup. Memes noms de champs que `health.json` : un seul vocabulaire pour la
# page du jour et pour l'archive, sans table de correspondance a maintenir.
ARCHIVE = ("verdict", "bid", "ask", "tick", "prof_bid", "prof_ask",
           "ticket_min", "volume24h")


def ligne_archivee(ligne: dict) -> dict:
    """Ce qu'on garde d'un carnet pour toujours, extrait du releve du jour.

    Fonction nommee plutot qu'une expression enfouie dans `main()` : c'est la
    seule ecriture de ce fichier qu'on ne pourra jamais refaire, elle merite
    d'etre testable sans reseau.
    """
    return {c: ligne[c] for c in ARCHIVE if c in ligne}


def charger_historique(jours: int = 30) -> dict[str, list[tuple[str, str]]]:
    """Rend {slug: [(date, verdict), ...]} du plus ancien au plus recent."""
    import glob
    import os

    series: dict[str, list[tuple[str, str]]] = {}
    fichiers = sorted(glob.glob(os.path.join(HISTORIQUE, "*.json")))[-jours:]
    for chemin in fichiers:
        jour = os.path.basename(chemin)[:-5]
        try:
            with open(chemin, encoding="utf-8") as f:
                releve = json.load(f)
        except (OSError, ValueError):
            # Un releve illisible ne doit pas faire echouer la page : on
            # continue avec ce qu'on a, l'historique est un bonus.
            continue
        for slug, mesure in releve.items():
            # LES DEUX FORMATS SE LISENT ICI. Les releves du 26 au 29 aout 2026
            # ne portent que le verdict, une chaine ; depuis, chaque ligne est
            # un objet qui garde aussi les prix. Ne lire que le nouveau format
            # jetterait les quatre premiers jours d'historique -- exactement ce
            # que ce fichier existe pour empecher.
            code = mesure["verdict"] if isinstance(mesure, dict) else mesure
            series.setdefault(slug, []).append((jour, code))
    return series


def persistance(serie: list[tuple[str, str]], code_actuel: str) -> int:
    """Depuis combien de releves consecutifs ce marche porte-t-il ce verdict ?

    Rend 1 si c'est nouveau (le releve du jour compte pour un). Un chiffre
    eleve sur « mort » ou « piege » est le signal le plus utile de la page :
    un carnet mort depuis une semaine ne se reveillera probablement pas.
    """
    compte = 1
    for _, code in reversed(serie):
        if code != code_actuel:
            break
        compte += 1
    return compte


def relever(session, nb_marches: int) -> list[dict]:
    # GAMMA PLAFONNE A 100 PAR REPONSE, quoi qu'on mette dans `limit`. Demander
    # 500 en rend 100 sans le dire -- on croit avoir balaye cinq fois plus de
    # marches qu'on n'en a lu. Meme famille de piege que le plafond a
    # offset=2100 qui faisait conclure « 0 finançable » le 22/08 : une API qui
    # tronque en silence fabrique des conclusions fausses.
    marches = []
    for depart in range(0, nb_marches, 100):
        try:
            reponse = session.get(
                GAMMA,
                params={"closed": "false", "limit": "100", "offset": str(depart),
                        "order": "volume24hr", "ascending": "false"},
                timeout=40,
            )
            reponse.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("page a l'offset %d illisible : %s", depart, exc)
            break
        page_lue = reponse.json()
        if not page_lue:
            break
        marches.extend(page_lue)
    # LE MEME MARCHE REVIENT SUR DEUX PAGES. Le tri se fait sur `volume24hr`,
    # qui bouge PENDANT la pagination : un marche qui gagne du volume recule
    # d'une page et se fait relire. Mesure du 2026-08-26 : 36 doublons sur 799
    # lignes, soit 763 marches distincts annonces comme 800. Un chiffre publie
    # ne vaut que s'il compte des choses distinctes.
    vus = set()
    uniques = []
    for m in marches:
        cle = m.get("conditionId") or m.get("slug")
        if cle in vus:
            continue
        vus.add(cle)
        uniques.append(m)
    doublons = len(marches) - len(uniques)
    marches = uniques[:nb_marches]
    logger.info("%d marches a sonder (%d doublons ecartes)", len(marches), doublons)

    lignes = []
    for i, m in enumerate(marches):
        if i and i % 50 == 0:
            logger.info("  %d / %d", i, len(marches))
        try:
            jetons = json.loads(m.get("clobTokenIds") or "[]")
        except ValueError:
            continue
        if not jetons:
            continue
        try:
            r = session.get(f"{CLOB}/book", params={"token_id": jetons[0]}, timeout=20)
            r.raise_for_status()
            carnet = r.json()
        except Exception:  # noqa: BLE001
            continue
        bids = carnet.get("bids") or []
        asks = carnet.get("asks") or []
        # Les carnets Polymarket arrivent PIRE PRIX EN PREMIER : le meilleur est
        # en derniere position (mesure du 26/07). Lire [0] donnerait le pire.
        bid = float(bids[-1]["price"]) if bids else None
        ask = float(asks[-1]["price"]) if asks else None
        pb = float(bids[-1]["size"]) if bids else 0.0
        pa = float(asks[-1]["size"]) if asks else 0.0
        if bid is None or ask is None or bid <= 0:
            code, phrase = "mort", "un cote du carnet est vide"
            ecart_rel = 0.0
        else:
            ecart_rel = (ask - bid) / bid
            code, phrase = verdict(ecart_rel, float(m.get("volume24hr") or 0), pb, pa, bid)
        taille_min = float(m.get("orderMinSize") or 5)
        lignes.append({
            "slug": str(m.get("slug") or ""),
            "question": str(m.get("question") or "")[:90],
            "bid": bid, "ask": ask,
            "ecart_pct": 100 * ecart_rel,
            "prof_bid": pb, "prof_ask": pa,
            "volume24h": float(m.get("volume24hr") or 0),
            "ticket_min": taille_min * (bid or 0),
            "tick": float(m.get("orderPriceMinTickSize") or 0.01),
            "verdict": code, "phrase": phrase,
        })
    return lignes


def loi_du_tick(lignes: list[dict]) -> dict:
    """La regle centrale de ce projet, RECALCULEE a chaque mesure.

    Elle a longtemps ete publiee comme absolue (« wherever there is volume, the
    spread is exactly one tick, without a single exception »). Verifie le
    2026-08-28 sur les carnets vivants a plus de 50 000 $/jour : c'est vrai a
    78 % quand le tick vaut 0,01, et a 43 % seulement quand il vaut 0,001 --
    c'est-a-dire sur le sport, ou la queue monte a SOIXANTE ticks.

    Relire ces carnets-la en direct explique l'ecart : bid a 0,999 et aucun ask.
    Ce sont des matchs en cours ou deja joues. L'intuition tenait, sa raison
    non : ce n'est pas « personne ne trade ce marche », c'est « ce marche est
    deja decide ».

    On ne reecrit donc pas la phrase a la main : on publie le chiffre du jour.
    Une regle qu'on republie sans la remesurer finit toujours par mentir.
    """
    vivants = [l for l in lignes
               if l["verdict"] in ("tradable", "efficient")
               and l["bid"] and l["ask"] and l["volume24h"] > 50000]
    par_tick = {}
    for l in vivants:
        t = l.get("tick") or 0.01
        n = round((l["ask"] - l["bid"]) / t)
        d = par_tick.setdefault(t, {"total": 0, "un": 0, "max": 0})
        d["total"] += 1
        d["un"] += 1 if n == 1 else 0
        d["max"] = max(d["max"], n)
    return par_tick


def page(lignes: list[dict], style: str) -> str:
    e = html.escape
    quand = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    compte = {}
    for l in lignes:
        compte[l["verdict"]] = compte.get(l["verdict"], 0) + 1
    total = len(lignes) or 1

    tuiles = ""
    for code, titre, note in (
        ("tradable", "worth quoting", "spread, volume and depth on both sides"),
        ("efficient", "tight and alive", "one tick — nothing to capture, but you can exit"),
        ("piege", "traps", "wide spread, no volume — pays on paper, never fills"),
        ("mort", "dead books", "no counterparty, or already resolved"),
    ):
        n = compte.get(code, 0)
        tuiles += (f"<div class='t {code}'><span class='n'>{n}</span>"
                   f"<span class='l'>{titre}</span><p>{note}</p></div>")

    lignes.sort(key=lambda l: (ORDRE.get(l["verdict"], 9), -l["volume24h"]))
    rangs = ""
    for l in lignes[:120]:
        rangs += (
            f"<tr class='{l['verdict']}'>"
            f"<td class='l'>{e(l['question'])}</td>"
            f"<td class='num'>{l['volume24h']:,.0f}</td>"
            f"<td class='num'>{l['ecart_pct']:.1f}%</td>"
            f"<td class='num'>{l['prof_bid']:.0f} / {l['prof_ask']:.0f}</td>"
            f"<td class='num'>${l['ticket_min']:.2f}</td>"
            # LE CHIFFRE QUE PERSONNE D'AUTRE NE PEUT PRODUIRE. Il faut avoir
            # mesure les verdicts jour apres jour ; il ne se reconstitue pas
            # retroactivement. « mort depuis 6 releves » vaut plus qu'un
            # instantane : un carnet mort depuis une semaine ne se reveille pas.
            f"<td class='num'>{l.get('persistance', 1)}</td>"
            f"<td class='verdict'><b>{l['verdict']}</b><br><span>{e(l['phrase'])}</span></td>"
            f"</tr>"
        )

    extra = """
  .t{padding-right:18px}
  .t .n{font-weight:700;margin-right:.4em}
  .t p{margin:.35rem 18px .1rem 0}
  .t.tradable .n{color:var(--teal)} .t.efficient .n{color:var(--ink)}
  .t.piege .n{color:var(--brass)} .t.mort .n{color:var(--alarm)}
  tr.tradable td:first-child{border-left:3px solid var(--teal)}
  tr.piege td:first-child{border-left:3px solid var(--brass)}
  tr.mort td{color:var(--ink-3)}
  tr.desequilibre td:first-child{border-left:3px solid var(--brass)}
  td.verdict b{text-transform:uppercase;font-size:11px;letter-spacing:.06em}
  td.verdict span{color:var(--ink-3);font-size:12px}
  td.num{font-family:var(--data);font-variant-numeric:tabular-nums;
         text-align:right;white-space:nowrap}
  td.l{max-width:340px}
  .scroller{overflow-x:auto}
  a{color:var(--teal)}
  .cta{border:1px solid var(--teal);border-radius:10px;padding:20px 22px;margin:26px 0}
  .cta h2{margin:0 0 8px;font-size:1.15rem}
  .cta p{margin:0 0 14px}
  .cta .go{display:inline-block;padding:10px 18px;border:1px solid var(--teal);
       border-radius:8px;text-decoration:none;font-weight:600}
"""
    loi = loi_du_tick(lignes)
    morceaux = []
    for t in sorted(loi):
        d = loi[t]
        if not d["total"]:
            continue
        morceaux.append(
            f"where the tick is {t:g}, {d['un']} of {d['total']} live books "
            f"({100 * d['un'] // d['total']} %) sit at exactly one tick "
            f"(widest: {d['max']})"
        )
    phrase_loi = (
        "Measured today on books above $50k a day: " + "; ".join(morceaux) + "."
        if morceaux else
        "Not enough live books today to state the rule."
    )
    return f"""<title>Polymarket Book Health</title>
<meta name="description" content="Every Polymarket book read from the CLOB and judged on spread, volume, depth on both sides and minimum ticket. Where the tick is 0.01, most live books sit at exactly one tick. Where it is 0.001, they do not." />
<meta property="og:type" content="website" />
<meta property="og:site_name" content="DONmarket" />
<meta property="og:url" content="https://midas93230-cell.github.io/donmarket/health.html" />
<meta property="og:title" content="Polymarket Book Health — which books are actually alive" />
<meta property="og:description" content="Every Polymarket book read from the CLOB and judged on spread, volume, depth on both sides and minimum ticket. Where the tick is 0.01, most live books sit at exactly one tick. Where it is 0.001, they do not." />
<meta name="twitter:card" content="summary" />
<meta name="twitter:title" content="Polymarket Book Health — which books are actually alive" />
<meta name="twitter:description" content="Every Polymarket book read from the CLOB and judged on spread, volume, depth on both sides and minimum ticket. Where the tick is 0.01, most live books sit at exactly one tick. Where it is 0.001, they do not." />
<style>{style}{extra}</style>
<div class="wrap">
  <header class="stack masthead">
    <p class="eyebrow">Polymarket &middot; Independent measurement</p>
    <h1>Which books are actually alive</h1>
    <p class="dek">Polymarket shows you a price and a spread. It does not tell you whether anyone is on the other side. These {len(lignes)} books were read directly from the CLOB and judged on four things: spread, volume, depth on <i>both</i> sides, and the minimum ticket you would have to commit.</p>
    <p class="byline"><span>{quand}</span><span>Public endpoints, no API key</span><span>Read-only</span></p>
  </header>

  <div class="callout">
    <p><b>The rule this page exists to make visible, and its limit.</b> {phrase_loi} Reading the widest of them live explains the gap: a bid at 0.999 and no ask at all &mdash; matches in progress or already decided. So a wide spread still means no counterparty. It just does not always mean nobody trades it. Sorting by spread finds traps, not edges.</p>
  </div>

  <div class="thesis">{tuiles}</div>

  <div class="cta">
    <h2>These verdicts are enforced, not just published</h2>
    <p>The same measurement runs inside <b>DONmarket</b>, a page that connects your
    wallet and re-reads the book before anything is sent &mdash; then refuses the order
    and says why: a dead or trapped book, a size at exactly the minimum (any partial
    fill strands a remainder you cannot sell), a sell posted above the best ask, an
    order that crosses the spread when you did not mean to. Nothing to install, no
    account, and no key ever leaves your wallet.</p>
    <a class="go" href="./app.html">Open the app &rarr;</a>
  </div>

  <section>
    <h2>Every book, worst-case first</h2>
    <p class="sub">Sorted so the tradable ones come first and the traps are impossible to miss. Depth is shares at the best bid / best ask. Ticket is what the minimum order size actually costs you at the current bid &mdash; commit less than twice that and a partial fill can leave you holding something you cannot sell.</p>
    <div class="scroller"><table>
      <tr><th class="l">Market</th><th>24h volume</th><th>Spread</th><th>Depth bid / ask</th><th>Min ticket</th><th>Runs</th><th>Verdict</th></tr>
      {rangs}
    </table></div>
  </section>

  <footer>
    <p>Built by Abdoul Lahad Amar. Method and source: <a href="https://github.com/midas93230-cell/donmarket">github.com/midas93230-cell/donmarket</a>.
    Related: <a href="./app.html">The app</a> &middot; <a href="./verify.html">Can you verify a track record?</a> &middot; <a href="./work.html">Work with DON</a> &middot; <a href="./">Builders Radar</a> &middot; <a href="./python.html">Python SDK traps</a> &middot; <a href="./strategies.html">Six strategies, measured</a>.</p>
    <p>No affiliation with Polymarket. A snapshot goes stale &mdash; re-run the script rather than trusting an old page. Nothing here is financial advice.</p>
  </footer>
</div>"""


def main() -> int:
    import httpx

    parser = argparse.ArgumentParser()
    parser.add_argument("--marches", type=int, default=300)
    args = parser.parse_args()

    with httpx.Client() as session:
        debut = time.monotonic()
        lignes = relever(session, args.marches)

    if not lignes:
        print("aucun carnet lu -- rien n'est ecrit.")
        return 1

    # L'HISTORIQUE SE LIT AVANT D'ECRIRE LE RELEVE DU JOUR. `persistance()`
    # compte deja le releve courant pour un ; si le fichier d'aujourd'hui etait
    # deja charge, un second passage dans la journee le compterait deux fois.
    aujourdhui = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    series = charger_historique()
    for ligne in lignes:
        passe = [(j, c) for j, c in series.get(ligne["slug"], []) if j != aujourdhui]
        ligne["persistance"] = persistance(passe, ligne["verdict"])

    os.makedirs(HISTORIQUE, exist_ok=True)
    with open(f"{HISTORIQUE}/{aujourdhui}.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump({l["slug"]: ligne_archivee(l) for l in lignes},
                  f, ensure_ascii=False)

    with open("docs/_template.html", encoding="utf-8") as f:
        style = f.read().split("<style>", 1)[1].split("</style>")[0]
    with open("docs/health.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(page(lignes, style))
    with open("docs/health.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(lignes, f, indent=1, ensure_ascii=False)

    # LA DATE DE MESURE, PAS CELLE DE LECTURE. La page affichait la date du jour
    # où on l'ouvrait : le 27 août, elle datait du 27 des chiffres relevés le 26.
    # Sur une page dont toute la crédibilité tient à la mesure, se tromper d'un
    # jour suffit à la perdre. La source de vérité est ici, pas dans le
    # navigateur.
    with open("docs/health-meta.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {
                "mesure": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "carnets": len(lignes),
                "duree_s": round(time.monotonic() - debut),
            },
            f,
            ensure_ascii=False,
        )

    compte = {}
    for l in lignes:
        compte[l["verdict"]] = compte.get(l["verdict"], 0) + 1
    print(f"\ndocs/health.html -- {len(lignes)} carnets en {time.monotonic() - debut:.0f} s")
    for code in sorted(compte, key=lambda c: ORDRE.get(c, 9)):
        print(f"   {code:>14} : {compte[code]:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
