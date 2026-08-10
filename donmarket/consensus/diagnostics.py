"""Le test qui décide : ces membres sont-ils réellement indépendants ?

Un ensemble n'apporte quelque chose que si ses membres se trompent
DIFFÉREMMENT. C'est tout le principe : l'ECMWF fait tourner 51 membres en
perturbant ses conditions initiales, précisément pour qu'ils divergent.

Si N membres sont corrélés en moyenne à ρ, le nombre de votes réellement
indépendants n'est pas N mais :

    N_eff = N / (1 + (N − 1) × ρ)

C'est la taille d'échantillon effective sous corrélation uniforme, une formule
classique. Les ordres de grandeur qu'elle donne sont brutaux :

    ρ = 0,00  →  31 membres valent 31 votes
    ρ = 0,50  →  31 membres valent ~2 votes
    ρ = 0,90  →  31 membres valent ~1,1 vote

Autrement dit : un seuil de 28 voix sur 31 paraît exigeant, mais si ρ vaut 0,9
il est franchi dès que « le » modèle sous-jacent — car il n'y en a qu'un — dit
oui. Le seuil ne filtre alors rien du tout ; il donne la sensation d'un
consensus large là où il n'y a qu'un avis répété.

Module PUR : aucun appel réseau, aucune écriture.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from .ensemble import Consensus, Member, Vote, decide, vote_all


def vote_history(
    members: Sequence[Member], prices: Sequence[float], *, step: int = 1
) -> tuple[tuple[Vote, ...], ...]:
    """Rejoue l'ensemble sur toute la série : une ligne de votes par instant.

    On avance point par point comme le ferait le système en direct, en ne
    donnant à chaque fois que le passé — jamais la suite de la série.
    """
    history: list[tuple[Vote, ...]] = []
    for end in range(2, len(prices) + 1, max(1, step)):
        history.append(vote_all(members, prices[:end]))
    return tuple(history)


def _series(history: Sequence[Sequence[Vote]], index: int) -> list[float]:
    return [float(row[index].value) for row in history]


def pairwise_correlations(history: Sequence[Sequence[Vote]]) -> list[float]:
    """Corrélations entre toutes les paires de membres, votes codés +1/0/−1.

    Une paire dont l'un des deux ne varie jamais (il s'abstient tout du long,
    par exemple) n'a pas de corrélation définie : elle est écartée plutôt que
    comptée pour zéro, ce qui ferait passer l'ensemble pour plus indépendant
    qu'il n'est.
    """
    if not history:
        return []
    count = len(history[0])
    series = [_series(history, index) for index in range(count)]

    correlations: list[float] = []
    for left in range(count):
        for right in range(left + 1, count):
            try:
                correlations.append(
                    statistics.correlation(series[left], series[right])
                )
            except (statistics.StatisticsError, ValueError):
                continue
    return correlations


def mean_correlation(history: Sequence[Sequence[Vote]]) -> float | None:
    correlations = pairwise_correlations(history)
    if not correlations:
        return None
    return statistics.fmean(correlations)


def effective_members(history: Sequence[Sequence[Vote]]) -> float | None:
    """Combien de votes VRAIMENT indépendants valent ces membres."""
    if not history:
        return None
    count = len(history[0])
    if count <= 1:
        return float(count)
    rho = mean_correlation(history)
    if rho is None:
        return None
    # Une corrélation moyenne négative ne crée pas d'information supplémentaire :
    # on plafonne à N plutôt que de laisser la formule produire un nombre de
    # votes supérieur au nombre de membres.
    denominator = 1.0 + (count - 1) * rho
    if denominator <= 0:
        return float(count)
    return min(float(count), count / denominator)


@dataclass(frozen=True)
class EnsembleReport:
    """Ce qu'on a le droit de dire d'un ensemble après l'avoir mesuré."""

    members: int
    observations: int
    mean_correlation: float | None
    effective_members: float | None
    decisions: int  # instants où la supermajorité a été atteinte
    abstentions: int
    threshold: int

    @property
    def decision_rate(self) -> float:
        if self.observations == 0:
            return 0.0
        return self.decisions / self.observations

    @property
    def verdict(self) -> str:
        """La phrase à lire en premier, et sans complaisance."""
        if self.effective_members is None:
            return "Ensemble non mesurable : pas assez de variation dans les votes."
        if self.effective_members < 2.0:
            return (
                f"{self.members} membres ne valent que "
                f"{self.effective_members:.1f} vote indépendant : ce n'est pas un "
                "ensemble, c'est un seul modèle répété."
            )
        if self.effective_members < self.members / 4.0:
            return (
                f"{self.members} membres ne valent que "
                f"{self.effective_members:.1f} votes indépendants : le seuil de "
                f"{self.threshold} mesure surtout la corrélation des membres."
            )
        return (
            f"{self.members} membres valent {self.effective_members:.1f} votes "
            "indépendants : l'ensemble apporte de la diversité réelle."
        )


def analyse(
    members: Sequence[Member],
    prices: Sequence[float],
    *,
    threshold: int,
    step: int = 1,
) -> EnsembleReport:
    """Rejoue l'ensemble et rend son compte rendu, verdict compris."""
    history = vote_history(members, prices, step=step)
    consensuses: list[Consensus] = [decide(row, threshold=threshold) for row in history]
    decisions = sum(1 for c in consensuses if c.decision is not Vote.ABSTAIN)

    return EnsembleReport(
        members=len(members),
        observations=len(history),
        mean_correlation=mean_correlation(history),
        effective_members=effective_members(history),
        decisions=decisions,
        abstentions=len(history) - decisions,
        threshold=threshold,
    )
