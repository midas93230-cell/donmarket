# -*- coding: utf-8 -*-
r"""Publie `docs/arbitrage.html` : une contradiction de prix qu'on ne peut pas prendre.

    .venv\Scripts\python tools\page_arbitrage.py

## Pourquoi cette page existe

C'est le resultat qu'on avait enterre. Un lecteur l'a reclame :

    « A price contradiction that survives because the tooling can't reach it,
      not because nobody noticed. That's a completely different claim to
      "the market is inefficient" and much more useful. »

Il avait raison, et sa seconde remarque a corrige la conclusion : si c'est
atteignable hors SDK, le resultat est « couteux a atteindre » et non
« impossible » -- et les deux se degradent a des vitesses differentes. Verifie :
le SDK connait trois contrats negRisk sur Polygon, et notre propre portefeuille
detient deja une autorisation illimitee sur l'adaptateur. Donc atteignable.

## Ce que la page refuse de dire

Elle ne dit pas « le marche est inefficient ». Elle dit que trois evenements
portent des prix qui se contredisent arithmetiquement, que l'ecart survit au
carnet reel avec de la profondeur, et que le chemin pour le prendre existe mais
coute plus qu'il ne rapporte a notre taille. Ce sont trois affirmations
differentes et seule la troisieme explique pourquoi personne ne l'a pris.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.path.insert(0, "tools")
import enveloppe  # noqa: E402

# Mesure du 2026-08-31, verifiee sur le carnet en direct jambe par jambe.
MESURE = "31 August 2026"
CAS = (
    ("Fed Decision in October", 5, 1.0340, 1.0110, 1.10, 20626),
    ("Pro Football: 2027 Champion", 33, 2.0660, 1.0070, 0.70, 551180),
    ("Fed Decision in December", 5, 1.0560, 1.0070, 0.70, 21339),
)
PROFONDEUR = ((5, 1.0110), (25, 1.0110), (100, 1.0106), (500, 1.0093))
ADAPTATEUR = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"


def page(quand: str) -> str:
    lignes = "".join(
        f"<tr><td>{nom}</td><td class='num'>{n}</td>"
        f"<td class='num'>{ask:.4f}</td><td class='num'>{bid:.4f}</td>"
        f"<td class='num'>{ecart:.2f}%</td><td class='num'>{vol:,.0f}</td></tr>"
        for nom, n, ask, bid, ecart, vol in CAS)
    prof = "".join(
        f"<tr><td class='num'>{n}</td><td class='num'>{s:.4f}</td>"
        f"<td class='num'>{100 * (s - 1):+.2f}%</td></tr>"
        for n, s in PROFONDEUR)

    tuiles = enveloppe.tuile("3", "of 172", "contradictions found",
                             "Events with mutually exclusive outcomes whose "
                             "prices do not sum to $1.")
    tuiles += enveloppe.tuile("1.10", "%", "largest gap",
                              "Survives the live book with real depth to $500.")
    tuiles += enveloppe.tuile("0", "takeable", "why it persists",
                              "The client exposes no conversion. The contract "
                              "does &mdash; at a cost that exceeds the gap.")

    return enveloppe.debut(
        titre="A price contradiction nobody can take",
        chapeau=f"DONMARKET &middot; MEASURED {MESURE.upper()}",
        dek="Three Polymarket events price mutually exclusive outcomes so that "
            "the set does not sum to $1. The gap survives the live order book. "
            "<b>It persists because the tooling can't reach it &mdash; not "
            "because nobody noticed.</b>",
        signature="Read-only &middot; No key &middot; No orders placed "
                  "&middot; Reproducible",
        og="Three Polymarket events price mutually exclusive outcomes so the "
           "set does not sum to $1. The gap is real, survives the live book, "
           "and is not takeable.",
        tuiles=tuiles) + f"""
<section>
<h2>The arithmetic</h2>
<p>A <code>negRisk</code> event has mutually exclusive outcomes: exactly one
happens. Holding one share of every outcome therefore pays exactly $1, whatever
occurs. So if the outcome prices don't sum to $1, something is wrong with the
prices and not with your forecast. No opinion required.</p>

<div class="scroller"><table>
<thead><tr><th class="l">Event</th><th class="num">Legs</th>
<th class="num">Sum of asks</th><th class="num">Sum of bids</th>
<th class="num">Gap</th><th class="num">24h volume ($)</th></tr></thead>
<tbody>{lignes}</tbody>
</table></div>

<p>Three out of 172 events with exclusive outcomes, scanned across the 400 most
active. Everything else summed to $1 within half a percent.</p>
</section>

<section>
<h2>It survives the real book</h2>
<p class="sub">A displayed price is not a price. This walks the live bid side
level by level, which is what an order would actually do.</p>
<div class="scroller"><table>
<thead><tr><th class="num">Shares per leg</th><th class="num">Sum obtained</th>
<th class="num">Gap</th></tr></thead>
<tbody>{prof}</tbody>
</table></div>
<p>Depth to $500 with only mild decay. This is not dust.</p>
</section>

<section>
<h2>And it cannot be taken</h2>
<p>To hold one share of every outcome you must either mint the complete set for
$1, or buy each leg. <b>The Python client exposes no negRisk conversion.</b>
<code>split_position</code> splits collateral into YES + NO of a <i>single</i>
binary market, not into one share of each of five outcomes. So assembling the
set means buying every leg at the ask &mdash; 1.0340 &mdash; to sell it at
1.0110. That is a 2.3% <i>loss</i>, not a 1.1% gain.</p>

<p><b>I first published this as "impossible". That was wrong, and a reader
corrected the framing.</b> If the mechanism is reachable outside the client,
the finding is "expensive to reach" rather than "impossible" &mdash; and those
two decay at very different speeds.</p>

<p>Checked: the client's own configuration carries three negRisk contract
addresses on Polygon, and the wallet I measure from already holds an unlimited
allowance to the adapter at <code>{ADAPTATEUR}</code>. The contract is
deployed and I am already approved to call it. I simply cannot reach it through
the client.</p>

<div class="callout">
<h3>So the honest finding is: reachable, priced out at my size</h3>
<p>Direct contract calls instead of a client. Gas on every leg. No client-side
validation of anything. An on-chain operation that cannot be cancelled once
signed. Against a gap worth $0.055 on a $5 stake, that is not a trade &mdash;
it is a wager on my own contract code being right the first time. It stays open
until someone with enough capital to amortise the tooling bothers.</p>
</div>
</section>

<section>
<h2>Why this is worth publishing at all</h2>
<p>"The market is inefficient" and "the market is efficient given the tools
people actually have" are different claims, and only the second explains what
you see. The contradiction isn't there because nobody looked. It's there
because looking is free and taking is not.</p>
<p>Everything above is reproducible with no key and no account:</p>
<pre>python tools/incoherences.py
python tools/incoherences.py --verifier fed-decision-in-october-20260617190323537</pre>
<p>The scanner prints <b>NONE</b> far more often than it prints a result, and
that line is kept deliberately. A scanner that stops reporting nothing is a
scanner that has started agreeing with you.</p>
<p class="small">Generated {quand}. Measured {MESURE}; the gaps quoted are that
snapshot, and gaps of this kind move within hours. Re-run rather than trust the
page.</p>
</section>
""" + enveloppe.fin("arbitrage.html")


def main() -> int:
    quand = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    with open("docs/arbitrage.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(page(quand))
    print("docs/arbitrage.html ecrit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
