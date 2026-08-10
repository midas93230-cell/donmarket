"""Le compte : du cash, des positions, et un solde qui se réévalue.

## Pourquoi le solde bouge, et par quels trois canaux exactement

1. **Les remplissages** déplacent du cash vers de l'inventaire. À eux seuls ils
   ne créent ni ne détruisent de valeur : payer 0,45 $ une part qui en vaut
   0,45 laisse le solde inchangé. C'est voulu — un achat n'est pas une perte.
2. **Le prix** réévalue l'inventaire à chaque relevé. C'est de là que viennent
   les gains et les pertes, et c'est le canal qui bouge en permanence.
3. **Les récompenses** ajoutent du cash sans rien retirer. C'est la seule
   entrée nette, et c'est toute la stratégie.

## Le choix qui mérite d'être défendu : pas de vente

`plan_orders` ne pose que des ACHATS, sur les deux branches. Acheter YES à
0,45 et NO à 0,53 revient à détenir un jeu complet payé 0,98 $ qui vaudra
1,00 $ à la résolution. La stratégie ne sort jamais d'une position : elle
attend. Un `SELL` est donc refusé plutôt que silencieusement accepté — le
jour où une sortie sera codée, elle devra être conçue, pas héritée par accident.

## Ce que la valorisation suppose

L'inventaire est valorisé au milieu du carnet. C'est la convention la moins
flatteuse disponible sans modéliser l'impact : liquider vraiment ces parts
coûterait la moitié de la fourchette de plus. Sur un carnet serré l'écart est
négligeable ; sur un carnet large il ne l'est pas, et c'est une raison de plus
de ne pas jouer les carnets larges.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class PaperFill:
    """Un remplissage : des parts acquises à un prix, à un instant."""

    token_id: str
    price: float
    size: float  # en parts
    filled_at: datetime

    @property
    def cost_usd(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class Position:
    """Ce que l'on détient sur un jeton, et ce qu'on l'a payé."""

    token_id: str
    shares: float
    cost_usd: float

    @property
    def average_price(self) -> float:
        """Prix de revient moyen. Zéro part rend zéro plutôt que de diviser."""
        if self.shares <= 0:
            return 0.0
        return self.cost_usd / self.shares

    def value_at(self, mark: float) -> float:
        return self.shares * mark

    def unrealised_at(self, mark: float) -> float:
        return self.value_at(mark) - self.cost_usd

    def with_fill(self, fill: PaperFill) -> "Position":
        return replace(
            self,
            shares=self.shares + fill.size,
            cost_usd=self.cost_usd + fill.cost_usd,
        )


class InsufficientCash(Exception):
    """Le compte n'a pas de quoi payer ce remplissage.

    Levée plutôt que rabotée : un compte de démonstration qui laisse passer un
    achat impayable enseigne une stratégie qui ne tiendrait pas en réel.
    """


@dataclass(frozen=True)
class PaperAccount:
    """Le compte de démonstration, immuable — chaque écriture rend un compte neuf."""

    starting_usd: float
    cash_usd: float
    positions: tuple[Position, ...] = ()
    rewards_usd: float = 0.0
    fills: tuple[PaperFill, ...] = ()

    @classmethod
    def opening(cls, starting_usd: float) -> "PaperAccount":
        """Un compte neuf : tout en cash, rien en position."""
        if starting_usd <= 0:
            raise ValueError("le capital de démonstration doit être positif")
        return cls(starting_usd=starting_usd, cash_usd=starting_usd)

    def position(self, token_id: str) -> Position | None:
        for held in self.positions:
            if held.token_id == token_id:
                return held
        return None

    @property
    def invested_usd(self) -> float:
        """Dollars sortis du cash et immobilisés en parts."""
        return sum(held.cost_usd for held in self.positions)

    def inventory_value(self, marks: Mapping[str, float]) -> float:
        """Valeur de l'inventaire aux prix fournis.

        Un jeton sans prix courant est valorisé à son prix de revient, pas à
        zéro : une cotation manquante est une ignorance, pas une perte totale.
        """
        total = 0.0
        for held in self.positions:
            mark = marks.get(held.token_id)
            total += held.cost_usd if mark is None else held.value_at(mark)
        return total

    def equity(self, marks: Mapping[str, float]) -> float:
        """Le solde : cash disponible plus inventaire au prix courant.

        Les récompenses n'apparaissent pas ici en propre — elles sont déjà
        entrées dans le cash au moment où elles ont été créditées. Les
        rajouter les compterait deux fois.
        """
        return self.cash_usd + self.inventory_value(marks)

    def pnl(self, marks: Mapping[str, float]) -> float:
        """Gain ou perte depuis l'ouverture, en dollars."""
        return self.equity(marks) - self.starting_usd

    def pnl_pct(self, marks: Mapping[str, float]) -> float:
        return self.pnl(marks) / self.starting_usd * 100.0

    def with_fill(self, fill: PaperFill) -> "PaperAccount":
        """Encaisse un remplissage : le cash paie, la position grossit."""
        if fill.size <= 0:
            return self
        if fill.cost_usd > self.cash_usd + 1e-9:
            raise InsufficientCash(
                f"{fill.cost_usd:.2f} $ demandés, {self.cash_usd:.2f} $ disponibles"
            )
        existing = self.position(fill.token_id)
        if existing is None:
            grown = (*self.positions, Position(fill.token_id, fill.size, fill.cost_usd))
        else:
            grown = tuple(
                held.with_fill(fill) if held.token_id == fill.token_id else held
                for held in self.positions
            )
        return replace(
            self,
            cash_usd=self.cash_usd - fill.cost_usd,
            positions=grown,
            fills=(*self.fills, fill),
        )

    def with_reward(self, usd: float) -> "PaperAccount":
        """Crédite une récompense de liquidité — la seule entrée nette."""
        if usd <= 0:
            return self
        return replace(
            self,
            cash_usd=self.cash_usd + usd,
            rewards_usd=self.rewards_usd + usd,
        )
