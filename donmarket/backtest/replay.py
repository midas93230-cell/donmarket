"""Ce qu'un teneur de marché aurait réellement encaissé, minute par minute.

## Pourquoi une seule série de prix suffit

Acheter NO à `q` est économiquement identique à vendre YES à `1 − q` : les deux
livrent la même exposition et se dénouent au même dollar. Coter les deux
branches d'un marché, comme le fait `execute/orders`, revient donc à coter YES
**des deux côtés** autour du milieu :

    acheter YES à m − d   et   acheter NO à (1 − m) − d  ⟺  vendre YES à m + d

Le rejeu se ramène ainsi à une tenue de marché symétrique sur une série
unique — celle que `/prices-history` sait rendre.

## Le modèle de remplissage, et ce qu'il ne sait pas

À chaque pas on cote `m ∓ d`, puis on regarde le prix suivant. S'il passe sous
notre achat, on est acheté ; s'il passe au-dessus de notre vente, on est vendu.
Puis on recote autour du nouveau prix.

Trois choses que ce modèle ignore, toutes à dire avant de lire un résultat :

1. **La file d'attente n'existe pas ici.** Sans carnet passé, impossible de
   savoir si quelqu'un était devant nous à ce prix. On suppose donc un
   remplissage certain dès que le prix touche notre cote. C'est optimiste sur
   les remplissages favorables — et réaliste sur les défavorables, puisque
   ceux-là arrivent précisément quand tout le monde veut passer du même côté.
2. **Le chemin intra-minute est invisible.** Une mèche qui touche notre cote et
   revient dans la même minute ne laisse aucune trace dans la série.
3. **Aucune récompense n'est comptée.** Le carnet passé manquant, la part du
   pool est irrécupérable. Ce module mesure le COÛT ; le rendement se mesure en
   direct (`analysis/rewards`). Les additionner n'a de sens que si les deux
   viennent de la même période.

## Le recotage est ce qui fait perdre

Recoter autour du prix qui vient de nous remplir est le comportement naïf, et
c'est délibérément celui qu'on mesure : vendu à 0,51 puis recoté autour de
0,53, on rachète à 0,52 et on perd un cent sur un aller-retour qui avait l'air
gagnant. Un teneur qui ne recote pas porte un autre risque. Le premier est
mesurable ici, et il est déjà instructif.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Un jeu complet vaut 1 $ : c'est l'unité dans laquelle `analysis/rewards`
# exprime rendement ET dérive (voir le piège n° 9 du README). Rapporter le
# résultat au prix moyen (≈ 0,45 $) gonflerait tout d'un facteur ~2 et rendrait
# la comparaison avec le rendement fausse sans qu'aucune erreur ne se voie.
COMPLETE_SET_USD = 1.0

# Plafond d'inventaire par défaut, en multiples de la taille cotée. Sans
# plafond, un marché qui tend dans un sens accumule une position sans fin et le
# rejeu affiche une perte que personne n'aurait subie : un teneur réel arrête
# de coter le côté qui le charge.
DEFAULT_MAX_INVENTORY = 3.0


@dataclass(frozen=True)
class ReplayResult:
    """Le compte rendu d'un rejeu, décomposé pour être contestable.

    `total_pnl` seul ne dit pas d'où vient le résultat. La décomposition en
    `spread_capture` (ce que les allers-retours ont rapporté) et
    `inventory_cost` (ce que la position ouverte a repris) est le seul moyen de
    voir si la stratégie gagne sa fourchette ou la rend à la sélection adverse.
    """

    steps: int
    buys: int
    sells: int
    inventory: float  # en parts, signé — négatif = vendeur net de YES
    cash: float
    final_price: float
    engaged_usd: float
    total_pnl: float
    spread_capture: float

    @property
    def round_trips(self) -> int:
        """Allers-retours complets — un achat apparié à une vente."""
        return min(self.buys, self.sells)

    @property
    def inventory_cost(self) -> float:
        """Ce que la position ouverte a repris à la fourchette encaissée.

        Négatif = la sélection adverse a mangé une partie du gain théorique.
        C'est le chiffre que `PathStats.drift` cherche à majorer.
        """
        return self.total_pnl - self.spread_capture

    @property
    def pnl_pct(self) -> float:
        """Résultat en pourcent du capital engagé — comparable au rendement."""
        if self.engaged_usd <= 0:
            return 0.0
        return self.total_pnl / self.engaged_usd * 100.0


def replay_quotes(
    prices: Sequence[float],
    *,
    half_spread: float,
    size: float,
    max_inventory: float = DEFAULT_MAX_INVENTORY,
) -> ReplayResult:
    """Rejoue une cotation symétrique sur une série de prix.

    `half_spread` est la distance au milieu de chaque côté — la même `d` que
    `analysis/scoring.quote_spread` calcule pour maximiser le score. `size` est
    la taille cotée en parts, typiquement `rewardsMinSize`.

    Une série de moins de deux points ne produit aucun remplissage : il faut un
    prix pour coter et le suivant pour savoir s'il nous a touchés.
    """
    engaged = max(size, 0.0) * COMPLETE_SET_USD
    if len(prices) < 2 or size <= 0 or half_spread <= 0:
        return ReplayResult(
            steps=len(prices),
            buys=0,
            sells=0,
            inventory=0.0,
            cash=0.0,
            final_price=float(prices[-1]) if prices else 0.0,
            engaged_usd=engaged,
            total_pnl=0.0,
            spread_capture=0.0,
        )

    cap = max_inventory * size
    inventory = 0.0
    cash = 0.0
    buys = 0
    sells = 0

    for previous, current in zip(prices, prices[1:]):
        bid = previous - half_spread
        ask = previous + half_spread

        # Le plafond ne bloque que le côté qui CHARGE la position ; l'autre
        # côté reste posté, c'est lui qui la débouclera.
        if current <= bid and inventory < cap:
            inventory += size
            cash -= bid * size
            buys += 1
        elif current >= ask and inventory > -cap:
            inventory -= size
            cash += ask * size
            sells += 1

    final_price = float(prices[-1])
    total_pnl = cash + inventory * final_price
    spread_capture = min(buys, sells) * 2.0 * half_spread * size

    return ReplayResult(
        steps=len(prices),
        buys=buys,
        sells=sells,
        inventory=inventory,
        cash=cash,
        final_price=final_price,
        engaged_usd=engaged,
        total_pnl=total_pnl,
        spread_capture=spread_capture,
    )
