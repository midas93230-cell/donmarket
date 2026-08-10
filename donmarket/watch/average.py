"""Moyenne pondérée par le temps — et pas par le nombre d'observations.

La distinction n'est pas un raffinement, c'est la correction elle-même.

Mesuré le 2026-07-29 : la liquidité concurrente d'un pool oscille entre 52 $ et
171 $ en 45 secondes, faisant passer son rendement net de +30 à +83 %/jour. Or
Polymarket ne paie pas sur la valeur qu'on lit à l'instant où on regarde : il
paie sur des relevés échantillonnés tout au long de la journée. Le chiffre
pertinent est donc la moyenne de la concurrence DANS LE TEMPS.

Pourquoi pondérer par le temps plutôt que faire la moyenne des messages reçus :
les mises à jour n'arrivent pas régulièrement, elles arrivent par rafales. Une
moyenne des messages compterait chaque rafale autant que les longues périodes
calmes qui les séparent — c'est-à-dire qu'elle donnerait le plus de poids aux
moments d'agitation, précisément ceux où le carnet est le plus encombré. Elle
surestimerait donc systématiquement la concurrence, dans le sens qui nous fait
rater les pools déserts.

Une valeur qui tient 30 secondes doit peser 30 fois plus qu'une valeur qui tient
une seconde. C'est tout ce que fait ce module.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class TimeAverage:
    """Accumulateur immuable : chaque observation rend un nouvel accumulateur.

    `total` est une somme de valeur × durée, `seconds` la durée couverte. La
    dernière valeur est gardée à part parce que son segment n'est pas encore
    fermé : on ne sait pas combien de temps elle va tenir.
    """

    total: float = 0.0
    seconds: float = 0.0
    last_value: float | None = None
    last_at: float | None = None
    samples: int = 0

    def observe(self, value: float, *, at: float) -> "TimeAverage":
        """Enregistre une valeur constatée à l'instant `at`.

        La première observation n'ajoute aucune durée : on ne sait pas depuis
        quand elle tenait, et le supposer inventerait du passé.
        """
        if self.last_at is None:
            return replace(self, last_value=value, last_at=at, samples=1)

        elapsed = max(0.0, at - self.last_at)
        return TimeAverage(
            total=self.total + (self.last_value or 0.0) * elapsed,
            seconds=self.seconds + elapsed,
            last_value=value,
            last_at=at,
            samples=self.samples + 1,
        )

    def mean_at(self, now: float) -> float | None:
        """Moyenne à l'instant `now`, segment courant compris.

        Compter le temps écoulé depuis la dernière observation n'est pas un
        détail : quand le flux se tait, c'est que la valeur TIENT. Une moyenne
        qui se figerait au dernier message dirait le contraire de la réalité —
        un carnet qui se vide et reste vide continuerait d'afficher son
        encombrement passé.
        """
        if self.last_at is None:
            return None

        elapsed = max(0.0, now - self.last_at)
        seconds = self.seconds + elapsed
        if seconds <= 0:
            return self.last_value
        return (self.total + (self.last_value or 0.0) * elapsed) / seconds

    def covered_seconds(self, now: float) -> float:
        """Durée réellement couverte — de quoi juger si la moyenne veut dire quelque chose."""
        if self.last_at is None:
            return 0.0
        return self.seconds + max(0.0, now - self.last_at)
