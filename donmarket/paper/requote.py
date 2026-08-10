"""Recoter : reposter une cote que le milieu a distancée.

## Pourquoi ce module existe (mesuré le 2026-08-06)

Une cote figée meurt. Douze minutes de marché réel, deux ordres, **aucun
remplissage**, et pourtant :

| Fenêtre | Rendement de notre cote |
|---|---|
| 8 premières minutes | 65,5 $/jour |
| 4 dernières minutes | **20,7 $/jour** |

Les deux tiers du rendement perdus en douze minutes, uniquement parce que le
milieu s'est éloigné d'un ordre qui ne bouge pas. Le score décroît en
`((v−s)/v)²` : la distance grandit, le carré l'amplifie, et au bord de la bande
il n'en reste rien. Tous les rendements exprimés en **%/jour** supposent qu'on
tient le score toute la journée — supposition fausse pour un ordre immobile.

## Ce que recoter coûte vraiment

Pas de frais : annuler et reposter est gratuit sur le CLOB. Le coût est
ailleurs, et il est réel — **on achète plus cher**. Poursuivre un milieu qui
monte, c'est relever son prix d'achat, donc dégrader son prix de revient sur les
parts obtenues ensuite. Ce coût-là n'a pas besoin d'être modélisé : il se paie
tout seul dans le compte, puisque les remplissages se font au prix de la cote du
moment.

C'est exactement le piège que `backtest/replay` mesure sous le nom de
« recotage naïf » : vendu à 0,51, recoté autour de 0,53, on rachète à 0,52. La
règle ci-dessous recote **strictement moins souvent** que ce rejeu, qui recote
chaque minute quoi qu'il arrive : elle attend que le score se soit réellement
dégradé. Le coût mesuré par le rejeu reste donc une borne haute de ce que la
poursuite nous coûte — pas une valeur alignée, une borne.

## La règle

Recoter quand notre score tombe sous une fraction de ce qu'un ordre frais
obtiendrait au même instant. Le seuil se règle : à 1,0 on recote au moindre
mouvement (poursuite maximale, prix de revient dégradé), à 0,0 jamais (le
comportement mesuré ci-dessus). Aucune valeur n'est évidente — c'est ce que
l'A/B doit trancher, et c'est pourquoi le seuil est un paramètre et non une
constante enfouie.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..analysis.scoring import planned_price, score_on_book
from ..api.clob import Book
from ..execute.orders import round_down_to_tick
from .fills import RestingOrder

# Sous cette fraction du score d'un ordre frais, la cote est jugée distancée.
# 0,5 signifie « on accepte de perdre la moitié de son score avant de bouger » —
# un compromis assumé entre marquer des points et poursuivre le prix.
DEFAULT_MIN_SCORE_RATIO = 0.5


@dataclass(frozen=True)
class RequotePolicy:
    """Quand une cote mérite d'être annulée et reposée.

    `enabled=False` conserve exactement le comportement d'origine (cote figée),
    ce qui permet de faire vivre les deux politiques côte à côte sur les mêmes
    carnets — la seule comparaison qui ait un sens, le marché changeant d'une
    exécution à l'autre.
    """

    min_score_ratio: float = DEFAULT_MIN_SCORE_RATIO
    enabled: bool = True


def requoted(
    order: RestingOrder,
    book: Book,
    max_spread: float,
    policy: RequotePolicy,
) -> RestingOrder | None:
    """L'ordre qui remplace celui-ci, ou `None` s'il tient toujours.

    Rend `None` dans tous les cas douteux — carnet illisible, ordre servi,
    politique désactivée — parce qu'un remplacement est une action et qu'on
    n'agit pas sur une lecture incertaine.
    """
    if not policy.enabled or order.remaining <= 0 or max_spread <= 0:
        return None

    target = planned_price(book, max_spread)
    if target is None:
        return None
    step = book.tick_size or 0.0
    price = round_down_to_tick(target, step) if step > 0 else target
    if price <= 0 or price == order.price:
        return None

    fresh = score_on_book(book, price, order.remaining, max_spread)
    current = score_on_book(book, order.price, order.remaining, max_spread)
    if fresh is None or current is None or fresh <= 0:
        return None
    if current >= policy.min_score_ratio * fresh:
        return None
    # Ne jamais recoter vers PIRE : si le carnet a bougé de telle sorte que
    # notre ordre marque déjà plus que le placement idéal, le déplacer serait
    # une perte sèche déguisée en entretien.
    if fresh <= current:
        return None

    # Un ordre neuf pour les parts qui restent à obtenir. Le compte ne bouge
    # pas : rien n'est débité tant que rien n'est rempli, donc recoter ne coûte
    # aucun dollar sur le moment — seulement un prix de revient différent plus
    # tard.
    return replace(order, price=price, size=order.remaining, filled=0.0)
