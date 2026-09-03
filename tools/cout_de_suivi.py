# -*- coding: utf-8 -*-
r"""Ce qu'un suiveur paie vraiment quand il copie un gros pari -- LECTURE SEULE.

    .venv\Scripts\python tools\cout_de_suivi.py <token_id> [<token_id> ...]
    .venv\Scripts\python tools\cout_de_suivi.py <token_id> --tailles 25,100,500
    .venv\Scripts\python tools\cout_de_suivi.py <token_id> --reussite 0.674

Aucune cle, aucun ordre, aucun fichier ecrit. Carnet public uniquement, donc
n'importe qui peut refaire le chiffre.

## Ce que cet outil mesure, et pourquoi personne ne le publie

Un vendeur de signaux annonce un prix d'entree et un taux de reussite. Les deux
sont pris AU MOMENT DE L'ALERTE. Mais le pari qui declenche l'alerte consomme
le carnet, et le suiveur remplit APRES, plus haut. L'ecart entre les deux est
le seul chiffre qui decide si suivre quelqu'un rapporte -- et il n'apparait
dans aucun bilan, parce que le mesurer demande le carnet, pas le prix affiche.

Cas mesure le 2026-09-02, `SpookyDegenaro` (« Whale Bot », Polymarket) :
67,4 % de reussite, ROI +13,1 %, prix d'entree moyen deduit ~0,60. Son
arithmetique est COHERENTE, verifiee, et son avantage est reel : le seuil de
rentabilite a 0,60 vaut 60 %, donc il a ~7,4 points d'avance.

Sept points, c'est mince. A 0,62 le seuil monte a 62 % et l'avance tombe a
5,4. A 0,65, il reste 2,4. **Tout tient dans cinq centimes de carnet.**

## Ce que cet outil REFUSE de faire

Il ne complete pas les parts manquantes au dernier prix connu quand le carnet
cede. Cela rendrait un prix moyen precis et faux -- la faute exacte que
`verifier_portefeuille.py` denonce, et qu'il a lui-meme commise le 2026-09-03
en annoncant un plancher « garanti » sur une fenetre tronquee. Ici un carnet
trop mince affiche CARNET EPUISE et la taille reellement obtenable.

Il ne dit pas non plus si le vendeur gagne de l'argent. Il dit ce que coute de
le suivre, ce qui est une autre question et la seule que le carnet sait
trancher.

## Sa limite, a dire avant qu'on la trouve

Il lit le carnet MAINTENANT, pas a la seconde de l'alerte. Ce qu'il rend est
donc un PLANCHER du cout de suivi : au moment de l'alerte le carnet vient
d'etre consomme par la baleine, donc il est plus mince, donc le cout reel est
au-dessus. Mesurer le vrai chiffre exige une capture continue -- chantier
separe, et la raison pour laquelle la donnee tick de `eguilesjr` a ete
demandee le 2026-09-03.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from donmarket.analysis.slippage import breakeven_win_rate, taker_fill  # noqa: E402
from donmarket.api.clob import fetch_books  # noqa: E402

TAILLES_DEFAUT = (25.0, 100.0, 500.0, 2000.0)


def _ligne(f, reussite: float | None) -> str:
    seuil = breakeven_win_rate(f.effective)
    bout = f"  avance {reussite - seuil:+.1%}" if reussite is not None else ""
    epuise = "  CARNET EPUISE" if f.exhausted else ""
    return (
        f"  {f.filled:>8.0f} parts  paye {f.effective:.4f}  "
        f"glissement {f.slippage:+.4f} ({f.slippage_pct:+.2%})  "
        f"seuil {seuil:.1%}{bout}{epuise}"
    )


def rapport(token_id: str, book, tailles, cote: str, reussite: float | None) -> None:
    print("=" * 74)
    print(f"JETON {token_id[:24]}...  cote {cote}")
    print("=" * 74)
    if book is None:
        # NE JAMAIS conclure « carnet vide » d'une lecture ratee : c'est la
        # meme faute que lire « 0 $ attribue » comme « pas de volume ».
        print("  Carnet illisible. Rien a conclure -- surtout pas qu'il est vide.")
        return

    niveaux = book.asks if cote == "BUY" else book.bids
    if not niveaux:
        print(f"  Aucun {'ask' if cote == 'BUY' else 'bid'} : rien a prendre.")
        return

    annonce = niveaux[0].price
    print(f"  prix affiche (ce que porte une alerte) : {annonce:.4f}")
    print(f"  seuil de rentabilite a ce prix         : "
          f"{breakeven_win_rate(annonce):.1%}")
    if reussite is not None:
        print(f"  avance annoncee                        : "
              f"{reussite - breakeven_win_rate(annonce):+.1%}")
        # GARDE-FOU CONTRE NOTRE PROPRE SORTIE. Un taux de reussite global
        # applique a UN marche ne veut rien dire : mesure du 2026-09-03, les
        # 67,4 % de SpookyDegenaro poses sur un marche a 0,15 affichent une
        # « avance » de +52,4 %, chiffre spectaculaire et vide. Le seuil, lui,
        # est valide partout : il ne depend que du prix.
        print("  ATTENTION : cette avance ne vaut QUE si le taux fourni est "
              "celui\n  de ce marche-ci. Un taux global pose sur un marche "
              "isole produit un\n  chiffre spectaculaire et vide. Le seuil, "
              "lui, ne depend que du prix.")
    print()

    for taille in tailles:
        f = taker_fill(book, cote, taille)
        if f is None:
            print(f"  {taille:>8.0f} parts  RIEN A MESURER")
            continue
        print(_ligne(f, reussite))


async def _run(args) -> int:
    # `fetch_books` rend DEJA un dict {token_id: Book} -- le reindexer sur
    # `b.token_id` iterait sur les cles, donc sur des chaines.
    books = await fetch_books(args.tokens)
    for token in args.tokens:
        rapport(token, books.get(token), args.tailles, args.cote, args.reussite)
        print()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tokens", nargs="+", help="token_id du marche a mesurer")
    p.add_argument("--tailles", default=None,
                   help="tailles en parts, separees par des virgules")
    p.add_argument("--cote", choices=("BUY", "SELL"), default="BUY",
                   help="sens de l'ordre du SUIVEUR (defaut BUY)")
    p.add_argument("--reussite", type=float, default=None,
                   help="taux de reussite annonce, ex. 0.674, pour afficher "
                        "l'avance restante apres glissement")
    args = p.parse_args()

    args.tailles = (
        tuple(float(t) for t in args.tailles.split(","))
        if args.tailles else TAILLES_DEFAUT
    )
    if args.reussite is not None and not 0.0 < args.reussite < 1.0:
        print("--reussite est une proportion dans ]0, 1[, pas un pourcentage.")
        return 1

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
