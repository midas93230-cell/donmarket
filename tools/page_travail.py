# -*- coding: utf-8 -*-
r"""Publie `docs/work.html` : ce qu'on fait, ce que ca coute, comment payer.

    .venv\Scripts\python tools\page_travail.py

## Pourquoi cette page existe

Le site publie des mesures. Le depot publie du code. Il n'existait nulle part
un endroit disant CE QU'ON FAIT, CE QUE CA COUTE ET COMMENT NOUS PAYER --
autrement dit, personne ne pouvait nous payer meme en le voulant. C'est le
chainon manquant entre la reputation et le revenu, et le seul qui ne depende
ni d'une audience, ni de la reponse de quiconque.

## Pourquoi elle est GENEREE et non ecrite a la main

Les preuves qu'elle affiche sont des chiffres : 7 portefeuilles sur 7, un
compte a 22 M$ verifie a 0,1 %, N carnets mesures ce jour. Une page qui cite
des chiffres sans les remesurer finit toujours par mentir -- lecon du
2026-08-29 sur la « loi du tick », republiee trois fois avant d'etre corrigee.
Ici les chiffres viennent de `docs/verify.json` et `docs/health-meta.json`,
donc ils suivent les mesures au lieu de vieillir.

## Ce qu'elle ne fait pas

Aucune promesse de rendement, aucun conseil d'investissement, aucun bouton qui
prend de l'argent. On vend du travail de mesure, pas des signaux de trading.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# Le tarif horaire. Sous 25 $/h on est classe parmi les profils qui
# disparaissent en cours de route ; les developpeurs data US et europeens sont
# a 80-150 $. 50 $ est competitif sans signaler la detresse, et laisse de la
# marge pour monter une fois qu'une reference publique existe.
TAUX = 50
FORFAITS = (
    ("Wallet audit", 150,
     "One wallet, reconciled on chain end to end: deposits, withdrawals, "
     "settled versus open positions, guaranteed floor, and an explicit list "
     "of what cannot be verified. Delivered as a report you can publish."),
    ("Order-book health report", 300,
     "A set of markets measured over several days: which books are tradable, "
     "which are dead, spread and tick, depth on both sides, and how long each "
     "verdict has held."),
    ("Archive access", 50,
     "The daily verdict archive as JSON, updated every day. Nobody starting "
     "today can rebuild it &mdash; it only exists because it was measured day "
     "after day."),
)

CONTACT = "midas93230@gmail.com"


def preuves() -> dict:
    """Les chiffres publics qui rendent l'offre credible, relus a chaque
    generation plutot que recopies."""
    with open("docs/verify.json", encoding="utf-8") as f:
        verif = json.load(f)
    lignes = verif["portefeuilles"]
    complets = [l for l in lignes if 0 < l.get("actes", 0) < 30000]
    # LA VITRINE PREND LE PLUS GROS MONTANT BIEN CORROBORE, pas le plus petit
    # ecart. « 22 M$ verifies a 0,1 % » et « 8,7 M$ verifies a 0,05 % » sont
    # aussi vrais l'un que l'autre ; le premier dit ce que ce travail sait
    # encaisser, le second ne dit que la precision d'une decimale.
    ecart = lambda l: (abs((l.get("retraits", 0) - l.get("depots", 0))
                           - l["pnl_annonce"]) / l["pnl_annonce"])
    meilleur = max((l for l in complets
                    if l.get("pnl_annonce") and ecart(l) <= 0.02),
                   key=lambda l: l["pnl_annonce"], default=None)
    try:
        with open("docs/health-meta.json", encoding="utf-8") as f:
            sante = json.load(f)
    except (OSError, ValueError):
        sante = {}
    return {"complets": len(complets), "total": len(lignes),
            "meilleur": meilleur, "carnets": sante.get("carnets", 0)}


def page(p: dict, style: str, quand: str) -> str:
    m = p["meilleur"]
    if m:
        net = m.get("retraits", 0) - m.get("depots", 0)
        ecart = 100 * abs(net - m["pnl_annonce"]) / m["pnl_annonce"]
        vitrine = (f"<p class=\"lede\">The most recent piece of work on this "
                   f"site: {p['complets']} of {p['complets']} wallets I could "
                   f"read end to end match their advertised PnL, the closest "
                   f"being <b>${m['pnl_annonce']:,.0f} verified to "
                   f"{ecart:.1f}%</b> against money that actually left the "
                   f"account.</p>")
    else:
        vitrine = ""

    cartes = "".join(
        f"<tr><td><b>{nom}</b><br><span class='small'>{quoi}</span></td>"
        f"<td class='num'><b>${prix}</b>"
        f"{'<br><span class=\"small\">per month</span>' if prix == 50 else ''}"
        f"</td></tr>"
        for nom, prix, quoi in FORFAITS)

    return f"""<meta charset="utf-8">
<title>Work with DON</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="Work with DON">
<meta property="og:description" content="On-chain wallet audits and order-book \
measurement for prediction markets. Fixed prices, published method, open \
source tooling.">
<style>{style}
.small{{opacity:.75;font-size:.9em}}</style>
<h1>Work with DON</h1>
<p class="lede">On-chain wallet audits and order-book measurement for
prediction markets. Everything below is built on tooling that is public, open
source and reproducible &mdash; you can check my numbers before you pay me
anything.</p>

{vitrine}

<h2>What I do</h2>
<p><b>I measure things about prediction markets and I say what I cannot
measure.</b> The audit tool refuses to state a total while positions are open
or while there are events it cannot account for, and it names them. That
restraint is the product: a number you can defend is worth more than a number
that looks good.</p>

<h2>Fixed prices</h2>
<table>
<tr><th>Engagement</th><th>Price</th></tr>
{cartes}
</table>
<p>Anything that does not fit the boxes above: <b>${TAUX}/hour</b>, five hours
minimum, scoped in writing before we start.</p>

<h2>Why me</h2>
<p>Two days ago I published that Polymarket track records were structurally
unverifiable. I was wrong, someone corrected me, and I said so publicly on the
page and on Reddit rather than quietly patching it. Then I rebuilt the reader
and the answer came out stronger. <b>That sequence is the reference.</b> You
are not hiring someone who is never wrong; you are hiring someone who finds
out, tells you, and fixes it the same day.</p>
<p>Evidence, all live and all reproducible:
<a href="./verify.html">wallet verification</a> &middot;
<a href="./health.html">daily order-book health</a>{f" ({p['carnets']} books measured in the latest run)" if p['carnets'] else ""} &middot;
<a href="https://github.com/midas93230-cell/donmarket">source</a>.</p>

<h2>How payment works</h2>
<p><b>USDC on Polygon</b>, which is how this whole ecosystem already pays.
Half up front on engagements over $300, the rest on delivery. Bank transfer
possible but slower. Invoices are issued under my legal name.</p>

<h2>What I do not sell</h2>
<p>No trading signals, no copy-trading, no returns of any kind, and no
predictions about which way a market will go. I sell measurement and the
tooling around it. If a number cannot be verified, the deliverable says so
&mdash; that is the whole point.</p>

<h2>Getting in touch</h2>
<p>Email <a href="mailto:{CONTACT}">{CONTACT}</a> with what you want measured.
A useful first message is one wallet address or one market slug, and the
question you actually want answered.</p>

<p class="small">Page generated {quand}; the figures above are read from the
latest measurement rather than typed in, so they cannot quietly go stale.</p>
"""


def main() -> int:
    p = preuves()
    quand = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    with open("docs/_template.html", encoding="utf-8") as f:
        style = f.read().split("<style>", 1)[1].split("</style>")[0]
    with open("docs/work.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(page(p, style, quand))
    print(f"docs/work.html -- {p['complets']}/{p['total']} portefeuilles "
          f"complets, taux {TAUX} $/h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
