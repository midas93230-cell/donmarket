"""Les plafonds durs — ce qui décide qu'un ordre NE part PAS.

Ce module est volontairement séparé du moteur, et volontairement pur : pas de
réseau, pas de clé, pas d'horloge. Ce qui autorise une dépense d'argent réel
doit être vérifiable ligne à ligne et testable sans compte.

## Trois plafonds, et pourquoi trois

Un seul plafond global ne suffit pas. Il laisserait tout le capital partir sur
un unique marché — précisément celui que le classement remonte en tête, donc
précisément celui où le modèle de risque se trompe le plus (mesuré le
01/08/2026 : les pires écarts sont tous des marchés à gros pool).

- `max_total_usd` borne l'exposition totale ;
- `max_per_market_usd` borne la concentration ;
- `max_orders` borne le nombre de tickets, parce qu'une erreur de boucle coûte
  autant qu'une erreur de montant.

## Aucune valeur par défaut sur le capital

`max_total_usd` n'a pas de défaut et n'en aura pas. Une valeur par défaut sur
un plafond de dépense est une décision prise à la place du propriétaire du
compte, et elle s'applique silencieusement le jour où l'appelant oublie le
paramètre.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ExecutionLimits:
    """Plafonds durs. Aucun ordre ne passe au-delà, quelle que soit l'analyse.

    Ces bornes ne sont pas des conseils : le portier les applique avant
    signature, donc avant qu'aucun dollar ne puisse bouger.
    """

    max_total_usd: float  # sans défaut : c'est au propriétaire du compte de le fixer
    max_per_market_usd: float
    max_orders: int

    def __post_init__(self) -> None:
        """Un plafond nul ou négatif est un refus, jamais une absence de limite.

        Sans cette vérification, `max_total_usd=0` laisserait tout passer par
        comparaison flottante mal orientée un jour de refactorisation.
        """
        if self.max_total_usd <= 0:
            raise ValueError("max_total_usd doit être strictement positif")
        if self.max_per_market_usd <= 0:
            raise ValueError("max_per_market_usd doit être strictement positif")
        if self.max_orders <= 0:
            raise ValueError("max_orders doit être strictement positif")
        if self.max_per_market_usd > self.max_total_usd:
            raise ValueError(
                "max_per_market_usd dépasse max_total_usd : le plafond par "
                "marché ne peut pas être plus permissif que le plafond global"
            )


@dataclass(frozen=True)
class GateDecision:
    """Le verdict du portier : ce qui passe, ce qui est refusé, et pourquoi.

    Les refusés sont RENVOYÉS avec leur motif plutôt que filtrés en silence.
    Un ordre qui disparaît sans explication laisse croire à un bug de la
    stratégie alors que c'est le plafond qui a fait son travail.
    """

    allowed: tuple[object, ...]
    refused: tuple[tuple[object, str], ...]

    @property
    def allowed_count(self) -> int:
        return len(self.allowed)

    @property
    def refused_count(self) -> int:
        return len(self.refused)


def order_cost_usd(order) -> float:
    """Ce qu'un ordre immobilise réellement, en dollars.

    Un ordre d'achat de `size` parts à `price` coûte `size × price`. C'est le
    montant qui sort du portefeuille — à ne pas confondre avec le capital
    engagé du jeu complet (1 $ la paire) utilisé par `analysis/rewards` pour
    exprimer un rendement.
    """
    return max(float(order.size), 0.0) * max(float(order.price), 0.0)


def gate(
    orders: Sequence[object],
    *,
    limits: ExecutionLimits,
    already_engaged_usd: float = 0.0,
) -> GateDecision:
    """Applique les plafonds dans l'ordre où ils sont posés.

    `already_engaged_usd` est ce qui est DÉJÀ immobilisé sur le compte : sans
    lui, relancer le moteur deux fois engagerait deux fois le plafond, chaque
    exécution se croyant seule au monde.

    Les ordres sont examinés dans l'ordre reçu et le premier qui dépasse est
    refusé — sans réordonner, sans « optimiser » le remplissage du plafond.
    Un portier qui choisit quoi garder n'est plus un portier, c'est une
    stratégie, et il faudrait alors la tester comme telle.
    """
    allowed: list[object] = []
    refused: list[tuple[object, str]] = []

    running_total = max(already_engaged_usd, 0.0)
    per_market: dict[str, float] = {}

    for order in orders:
        cost = order_cost_usd(order)
        market = getattr(order, "condition_id", "")

        if len(allowed) >= limits.max_orders:
            refused.append((order, f"plafond de {limits.max_orders} ordres atteint"))
            continue

        market_total = per_market.get(market, 0.0) + cost
        if market_total > limits.max_per_market_usd:
            refused.append(
                (
                    order,
                    f"{market_total:.2f} $ sur ce marché > plafond "
                    f"{limits.max_per_market_usd:.2f} $",
                )
            )
            continue

        if running_total + cost > limits.max_total_usd:
            refused.append(
                (
                    order,
                    f"{running_total + cost:.2f} $ engagés au total > plafond "
                    f"{limits.max_total_usd:.2f} $",
                )
            )
            continue

        allowed.append(order)
        running_total += cost
        per_market[market] = market_total

    return GateDecision(allowed=tuple(allowed), refused=tuple(refused))
