# -*- coding: utf-8 -*-
"""L'enveloppe visuelle commune a toutes les pages publiees.

## Le probleme qu'elle resout

`docs/_template.html` contient un vrai systeme de design -- bandeau de titre,
tuiles de chiffres, tableaux encadres, palette claire ET sombre, chiffres en
chasse fixe. Les pages generees n'en reprenaient QUE les couleurs : elles
emettaient des `<h1>` et des `<table>` nus, sans enveloppe, sans bandeau, sans
tuiles. Elles n'etaient pas mal dessinees, elles n'etaient pas dessinees.

Consequence commerciale, pas seulement esthetique : `verify.html` et
`work.html` sont les deux pages qu'on envoie a des gens qui peuvent payer. Une
page qui a l'air d'un brouillon fait douter des chiffres qu'elle porte, meme
quand ils sont justes.

## Pourquoi un module et pas trois copies

Trois generateurs qui redefinissent chacun leur en-tete divergent en deux
semaines. Ici l'identite vit a UN endroit : on la corrige une fois pour toutes
les pages, comme les liens de navigation qu'on a du reparer dans les
generateurs plutot que dans le HTML produit.
"""

from __future__ import annotations

import html

GABARIT = "docs/_template.html"

# Ce que le gabarit ne definit pas encore : les quelques selecteurs que les
# pages generees utilisent reellement. Ajoutes ici plutot que dans le gabarit
# pour ne pas toucher a la page d'accueil, qui vit sa propre vie.
COMPLEMENT = """
  .lede{font-family:var(--display); font-size:clamp(1.05rem,2.2vw,1.28rem);
        line-height:1.55; color:var(--ink-2); max-width:64ch; margin:0 0 18px}
  .lede b{color:var(--ink)}
  section{margin-top:42px}
  pre{font-family:var(--data); font-size:13px; line-height:1.5;
      background:var(--surface-2); border:1px solid var(--rule);
      padding:14px 16px; overflow-x:auto; margin:18px 0; max-width:68ch}
  code{font-family:var(--data); font-size:.92em}
  td.num,th.num{text-align:right; font-family:var(--data);
                font-variant-numeric:tabular-nums; white-space:nowrap}
  tbody td{padding:9px 14px; border-top:1px solid var(--rule)}
  tbody tr:hover{background:var(--surface-2)}
  .small{font-size:13px; color:var(--ink-3)}
  .foot{margin-top:56px; padding-top:20px; border-top:1px solid var(--rule);
        font-size:13.5px; color:var(--ink-3)}
  .foot a{color:var(--teal)}
  a{color:var(--teal)}
"""


def style() -> str:
    """Le CSS du gabarit, plus les selecteurs que les pages generees emploient."""
    with open(GABARIT, encoding="utf-8") as f:
        brut = f.read().split("<style>", 1)[1].split("</style>")[0]
    return brut + COMPLEMENT


def tuile(chiffre: str, unite: str, etiquette: str, phrase: str) -> str:
    """Une tuile de chiffre. Le chiffre d'abord, l'explication ensuite --
    c'est ce qu'on lit sur un telephone en trois secondes."""
    return (f"<div class='tile'><span class='lab'>{html.escape(etiquette)}</span>"
            f"<span class='n'>{chiffre}"
            f"{f'<small> {html.escape(unite)}</small>' if unite else ''}</span>"
            f"<p>{phrase}</p></div>")


def debut(*, titre: str, chapeau: str, dek: str, signature: str,
          og: str, tuiles: str = "") -> str:
    """Tete de page et bandeau de titre, identiques d'une page a l'autre."""
    bloc = f"<div class='thesis'>{tuiles}</div>" if tuiles else ""
    return f"""<meta charset="utf-8">
<title>{html.escape(titre)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="{html.escape(titre)}">
<meta property="og:description" content="{html.escape(og)}">
<style>{style()}</style>
<div class="wrap stack">
<header class="masthead stack">
  <p class="eyebrow">{html.escape(chapeau)}</p>
  <h1>{html.escape(titre)}</h1>
  <p class="dek">{dek}</p>
  <p class="byline">{signature}</p>
</header>
{bloc}
<main>"""


def fin(courante: str = "") -> str:
    """Pied de page et navigation. UNE PAGE QUE RIEN NE LIE EST INVISIBLE :
    `app.html` est restee orpheline des jours faute de ce bloc."""
    pages = (("./", "Builders Radar"), ("./health.html", "Book health"),
             ("./verify.html", "Wallet verification"),
             ("./work.html", "Work with DON"), ("./app.html", "The app"))
    liens = " &middot; ".join(
        f"<b>{nom}</b>" if chemin.endswith(courante) and courante
        else f"<a href='{chemin}'>{nom}</a>" for chemin, nom in pages)
    return f"""</main>
<footer class="foot">
<p>{liens}</p>
<p>Built by DON. Method and source:
<a href="https://github.com/midas93230-cell/donmarket">github.com/midas93230-cell/donmarket</a>.
Everything here is measured from public APIs and reproducible &mdash; no key,
no authentication, no orders placed.</p>
</footer>
</div>
"""
