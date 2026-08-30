# -*- coding: utf-8 -*-
"""Publie `docs/404.html`, la page servie quand une adresse ne mene nulle part.

    .venv/Scripts/python tools/page_404.py

## Pourquoi elle existe

Le 2026-08-30, quelqu'un a rapporte que « le site est inaccessible ». Les six
pages repondaient 200. Ce qui ne repondait pas, c'est
`midas93230-cell.github.io` SANS `/donmarket/` -- une adresse recopiee a la
main, tronquee, qui tombe sur une erreur nue. Une personne qui voit ca ne
signale pas une faute de frappe : elle conclut que le site est mort, et elle
ne revient pas.

## Ce qu'elle peut et ne peut pas rattraper

Elle rattrape tout ce qui est FAUX SOUS `/donmarket/` : une page renommee, un
lien mal recopie, une majuscule de trop. Elle ne peut PAS rattraper la racine
`midas93230-cell.github.io/`, qui appartient a un autre site GitHub Pages,
inexistant. La seule parade la-bas est de toujours coller l'adresse complete.

Pas de redirection automatique : renvoyer quelqu'un ailleurs sans lui dire
pourquoi transforme une erreur comprehensible en comportement inexplicable.
On dit ce qui s'est passe, et on donne les liens.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "tools")
import enveloppe  # noqa: E402


def main() -> int:
    corps = enveloppe.debut(
        titre="That page moved, or never existed",
        chapeau="DONMARKET &middot; 404",
        dek="Nothing here &mdash; but the site is alive. The most common cause "
            "is an address copied by hand without the <code>/donmarket/</code> "
            "part.",
        signature="Public APIs only &middot; Read-only &middot; Reproducible",
        og="That page moved, or never existed. The site is alive — here is "
           "where to go.")
    corps += """
<section>
<h2>Where you probably wanted to go</h2>
<p>Every page below is live. If you arrived from a link someone pasted, the
address most likely lost its <code>/donmarket/</code> segment on the way.</p>
<ul>
<li><a href="./verify.html"><b>Can you verify a Polymarket track record?</b></a>
&mdash; the top wallets, reconciled on chain against the PnL they advertise.</li>
<li><a href="./health.html"><b>Book health</b></a> &mdash; which order books are
actually tradable, measured every day.</li>
<li><a href="./work.html"><b>Work with DON</b></a> &mdash; what I do, what it
costs, and how to reach me.</li>
<li><a href="./"><b>Builders Radar</b></a> &mdash; the front page.</li>
</ul>
<p class="small">The full address of this site is
<code>https://midas93230-cell.github.io/donmarket/</code> &mdash; the last
segment matters.</p>
</section>
"""
    corps += enveloppe.fin()
    with open("docs/404.html", "w", encoding="utf-8", newline="\n") as f:
        f.write(corps)
    print("docs/404.html ecrit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
