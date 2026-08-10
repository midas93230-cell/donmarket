"""Le mode ombre : mesurer le rendement réel sans risquer un dollar.

## Ce que ce module rend possible, et qu'aucun backtest ne peut faire

`backtest/` ne mesure que le COÛT, et le dit : `/prices-history` rend les prix
passés, jamais les carnets passés, donc ni la concurrence, ni les scores, ni la
part du pool ne sont reconstituables après coup.

En direct, cette limite tombe. À chaque relevé on voit le carnet, donc on sait
ce que NOS ordres auraient marqué et ce que les autres marquaient au même
instant. Accumulés minute après minute, ces relevés donnent la part de pool
réellement obtenue — le seul terme que la stratégie n'avait jamais mesuré.

## Pourquoi il faut passer par là avant d'engager de l'argent

Le rendement affiché par `analysis/rewards` est un INSTANTANÉ : il suppose que
la concurrence observée à la seconde du balayage vaut pour les 24 h suivantes.
Rien ne le garantit — un seul teneur qui arrive divise la part par deux. Le
mode ombre remplace cette supposition par une moyenne mesurée.

## Ce qu'il ne mesure toujours pas

La file d'attente et le remplissage. Un ordre en ombre n'est jamais devant
personne dans le carnet, et il ne modifie pas le comportement des autres. Le
score, lui, ne dépend que du prix et de la taille : celui-là est exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

# Un jeu complet vaut 1 $ — même unité que `analysis/rewards` et
# `backtest/replay` (piège n° 9 du README).
FULL_SET_USD = 1.0

MINUTES_PER_DAY = 1440.0

# En dessous, la couverture est trop courte pour qu'une extrapolation à la
# journée veuille dire quelque chose. Trente minutes, c'est déjà peu ; c'est le
# minimum en dessous duquel le chiffre serait franchement trompeur.
MIN_MINUTES_TO_EXTRAPOLATE = 30.0


@dataclass(frozen=True)
class ShadowSample:
    """Un relevé : ce que nos ordres auraient marqué à cet instant précis.

    Les deux scores voyagent ensemble parce que la part est leur RAPPORT.
    Garder l'un sans l'autre laisserait recalculer une part avec un
    dénominateur dont on ignore l'échelle.
    """

    condition_id: str
    observed_at: datetime
    own_q: float  # score de nos deux ordres, mesuré sur le carnet du moment
    competing_q: float  # score déjà posté par les autres, même instant
    daily_pool: float  # dollars par jour versés par le marché
    engaged_usd: float  # capital qu'il faudrait immobiliser
    midpoint: float

    @property
    def share(self) -> float:
        """Part du pool à cet instant, entre 0 et 1.

        Un score propre nul rend zéro sans division : sur un carnet large,
        notre ordre sort de la bande qualifiante et ne marque rien, quel que
        soit le capital immobilisé.
        """
        if self.own_q <= 0:
            return 0.0
        total = self.own_q + self.competing_q
        if total <= 0:
            return 0.0
        return self.own_q / total

    @property
    def usd_per_minute(self) -> float:
        """Dollars gagnés pendant la minute que ce relevé représente."""
        return self.daily_pool * self.share / MINUTES_PER_DAY


@dataclass(frozen=True)
class ShadowLedger:
    """Les relevés d'un marché, et le rendement qu'ils mesurent.

    Volontairement séparé de `RewardCandidate` : celui-ci porte une ESTIMATION
    faite à un instant, celui-là une MESURE accumulée. Les confondre dans un
    même objet ferait disparaître la seule distinction qui compte ici.
    """

    condition_id: str
    question: str
    samples: tuple[ShadowSample, ...] = ()

    @property
    def observations(self) -> int:
        return len(self.samples)

    @property
    def minutes_covered(self) -> float:
        """Durée réellement couverte, en minutes, bornes comprises.

        Comptée sur les horodatages et non sur le nombre de relevés : un fil
        qui décroche laisse des trous, et compter les relevés reviendrait à
        prétendre que le temps mort a été observé.
        """
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1].observed_at - self.samples[0].observed_at
        return span.total_seconds() / 60.0

    @property
    def engaged_usd(self) -> float:
        return self.samples[0].engaged_usd if self.samples else 0.0

    @property
    def median_share(self) -> float | None:
        """Part médiane du pool — plus honnête que la moyenne.

        Une part passe à 1,0 dès qu'un carnet se vide une minute. Sur une
        moyenne, ces minutes-là portent tout le résultat ; sur une médiane,
        elles restent ce qu'elles sont : des exceptions.
        """
        if not self.samples:
            return None
        return median(sample.share for sample in self.samples)

    @property
    def measured_usd_per_day(self) -> float | None:
        """Gain mesuré, ramené à une journée pleine.

        `None` tant que la couverture est trop courte pour extrapoler : une
        minute d'observation ne dit rien d'une journée, et l'annoncer en
        « $/jour » donnerait à trois relevés l'allure d'un rendement.
        """
        if self.minutes_covered < MIN_MINUTES_TO_EXTRAPOLATE:
            return None
        earned = sum(sample.usd_per_minute for sample in self.samples)
        # Le gain moyen par relevé, ramené à une journée de relevés. Passer par
        # la moyenne plutôt que par la somme évite qu'un fil qui échantillonne
        # deux fois plus vite annonce deux fois plus de dollars.
        per_sample = earned / self.observations
        return per_sample * MINUTES_PER_DAY

    @property
    def measured_yield(self) -> float | None:
        """Rendement mesuré en % du capital engagé, par jour."""
        usd = self.measured_usd_per_day
        if usd is None or self.engaged_usd <= 0:
            return None
        return usd / self.engaged_usd * 100.0

    def with_sample(self, sample: ShadowSample) -> "ShadowLedger":
        """Renvoie un nouveau registre — les relevés ne se modifient jamais."""
        return ShadowLedger(
            condition_id=self.condition_id,
            question=self.question,
            samples=(*self.samples, sample),
        )


def compare_to_estimate(
    ledger: ShadowLedger, estimated_yield: float
) -> tuple[float, float] | None:
    """Écart entre le rendement ESTIMÉ par le scan et celui MESURÉ en ombre.

    Renvoie `(mesuré, écart)`, ou `None` si la mesure n'est pas encore lisible.
    L'écart est ce qui dira si l'instantané du balayage vaut quelque chose :
    positif, le scan sous-estime ; négatif, il promet ce qu'il ne tient pas.
    """
    measured = ledger.measured_yield
    if measured is None:
        return None
    return measured, measured - estimated_yield
