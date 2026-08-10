"""Les membres de l'ensemble et le vote à supermajorité.

Construit exactement ce que décrit la méthode : N chemins de prédiction
parallèles sur la même série, un vote, et l'abstention si la supermajorité
n'est pas atteinte. Les familles sont volontairement différentes les unes des
autres — élan, retour à la moyenne, cassure, filtre de volatilité — et
déclinées sur plusieurs horizons, ce qui est la façon habituelle d'arriver à
une trentaine de membres.

Le mot important est « volontairement ». Choisir des familles opposées est ce
qu'on peut faire de mieux pour les décorréler, et `diagnostics.py` mesure si ça
a suffi. Par construction, aucune de ces variantes ne perturbe les DONNÉES —
elles lisent toutes la même série. C'est la différence de fond avec un ensemble
météo, qui perturbe ses conditions initiales, et c'est pourquoi la mesure de
corrélation n'est pas un raffinement mais le test principal.

Module PUR : aucun appel réseau, aucune écriture.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence


class Vote(Enum):
    """Un avis de membre. L'abstention est un avis à part entière."""

    UP = 1
    DOWN = -1
    ABSTAIN = 0


Predictor = Callable[[Sequence[float]], Vote]


@dataclass(frozen=True)
class Member:
    """Un membre nommé — le nom sert au diagnostic, pas à la décision."""

    name: str
    predict: Predictor


def _slice(prices: Sequence[float], lookback: int) -> Sequence[float] | None:
    """Les `lookback` derniers points, ou None s'il n'y en a pas assez.

    Renvoyer None plutôt que de travailler sur une fenêtre tronquée : un membre
    qui vote sur trois points quand on lui en demande soixante ne vote pas sur
    ce qu'on croit, et son avis se mélangerait aux autres sans qu'on le sache.
    """
    if lookback < 2 or len(prices) < lookback:
        return None
    return prices[-lookback:]


def momentum(lookback: int, threshold: float) -> Predictor:
    """Suit la tendance : la hausse récente appelle la hausse."""

    def predict(prices: Sequence[float]) -> Vote:
        window = _slice(prices, lookback)
        if window is None:
            return Vote.ABSTAIN
        change = window[-1] - window[0]
        if change > threshold:
            return Vote.UP
        if change < -threshold:
            return Vote.DOWN
        return Vote.ABSTAIN

    return predict


def mean_reversion(lookback: int, threshold: float) -> Predictor:
    """Parie contre l'écart : au-dessus de sa moyenne, le prix redescend."""

    def predict(prices: Sequence[float]) -> Vote:
        window = _slice(prices, lookback)
        if window is None:
            return Vote.ABSTAIN
        gap = window[-1] - statistics.fmean(window)
        if gap > threshold:
            return Vote.DOWN
        if gap < -threshold:
            return Vote.UP
        return Vote.ABSTAIN

    return predict


def breakout(lookback: int) -> Predictor:
    """Ne vote que sur un nouvel extrême de la fenêtre."""

    def predict(prices: Sequence[float]) -> Vote:
        window = _slice(prices, lookback)
        if window is None:
            return Vote.ABSTAIN
        last, past = window[-1], window[:-1]
        if last > max(past):
            return Vote.UP
        if last < min(past):
            return Vote.DOWN
        return Vote.ABSTAIN

    return predict


def volatility_filter(lookback: int, max_volatility: float) -> Predictor:
    """Élan, mais muet quand la série s'agite trop pour vouloir dire quelque chose."""

    def predict(prices: Sequence[float]) -> Vote:
        window = _slice(prices, lookback)
        if window is None:
            return Vote.ABSTAIN
        steps = [abs(b - a) for a, b in zip(window, window[1:])]
        if not steps or statistics.fmean(steps) > max_volatility:
            return Vote.ABSTAIN
        return Vote.UP if window[-1] > window[0] else Vote.DOWN

    return predict


# Horizons en minutes : la série Polymarket est échantillonnée à la minute.
LOOKBACKS = (5, 10, 15, 20, 30, 45, 60, 90)

# Seuils en dollars de probabilité. 0,005 = un demi-cent, soit cinq ticks.
THRESHOLDS = (0.002, 0.005, 0.010)


def build_ensemble(size: int = 31) -> tuple[Member, ...]:
    """Fabrique `size` membres, en alternant les familles.

    L'alternance n'est pas cosmétique : construire d'abord les 8 élans, puis
    les 8 retours à la moyenne, donnerait un ensemble tronqué très déséquilibré
    dès que `size` n'est pas un multiple du nombre de familles.
    """
    families: list[list[Member]] = [[], [], [], []]
    for lookback in LOOKBACKS:
        for threshold in THRESHOLDS:
            families[0].append(
                Member(f"elan-{lookback}-{threshold}", momentum(lookback, threshold))
            )
            families[1].append(
                Member(
                    f"retour-{lookback}-{threshold}",
                    mean_reversion(lookback, threshold),
                )
            )
        families[2].append(Member(f"cassure-{lookback}", breakout(lookback)))
        families[3].append(
            Member(f"calme-{lookback}", volatility_filter(lookback, 0.004))
        )

    # Tourniquet entre familles : un membre de chacune, à tour de rôle. Toute
    # troncature reste ainsi un mélange, ce qui est le seul espoir d'obtenir
    # une once de décorrélation — et `diagnostics.py` dira si ça a suffi.
    ordered: list[Member] = []
    for rank in range(max(len(family) for family in families)):
        for family in families:
            if rank < len(family):
                ordered.append(family[rank])
    return tuple(ordered[:size])


def vote_all(members: Sequence[Member], prices: Sequence[float]) -> tuple[Vote, ...]:
    return tuple(member.predict(prices) for member in members)


@dataclass(frozen=True)
class Consensus:
    """Le dépouillement d'un vote."""

    up: int
    down: int
    abstain: int
    threshold: int
    decision: Vote

    @property
    def voters(self) -> int:
        """Membres qui se sont prononcés — les abstentions ne comptent pas."""
        return self.up + self.down

    @property
    def total(self) -> int:
        return self.up + self.down + self.abstain


def decide(votes: Sequence[Vote], *, threshold: int) -> Consensus:
    """Applique la supermajorité. En dessous du seuil : abstention, pas de pari.

    Le seuil porte sur le nombre de voix DANS UN SENS, rapporté au total des
    membres — abstentions comprises. C'est le point qui rend la méthode
    prudente : trente membres muets et un seul qui parle ne font pas
    l'unanimité, ils font un membre isolé.
    """
    up = sum(1 for vote in votes if vote is Vote.UP)
    down = sum(1 for vote in votes if vote is Vote.DOWN)
    abstain = sum(1 for vote in votes if vote is Vote.ABSTAIN)

    decision = Vote.ABSTAIN
    if up >= threshold and up > down:
        decision = Vote.UP
    elif down >= threshold and down > up:
        decision = Vote.DOWN

    return Consensus(
        up=up, down=down, abstain=abstain, threshold=threshold, decision=decision
    )
