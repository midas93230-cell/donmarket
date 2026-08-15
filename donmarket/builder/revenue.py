"""Les ventes des builders — et pourquoi le classement officiel les cache.

Polymarket publie un classement par VOLUME routé. Ce n'est pas un classement
par revenu, et l'écart n'est pas un détail de présentation : au 2026-08-15,
sur les 20 premiers du classement historique, **la majorité facture 0 bps**.
betmoar est premier avec 2,06 milliards de dollars routés et ne prélève rien ;
Gate, polytraderpro, standtrade, Jupiter, MagicMarkets non plus. Pendant ce
temps Polycule, Bullpen et Polygun facturent 100 bps sur un volume cent fois
moindre.

Le volume est donc, au mieux, la moitié de l'information. Ce module fournit
l'autre moitié : le taux réellement pratiqué, mesuré exécution par exécution
(`fees.infer_schedule`), et le revenu qui en découle.

## Trois réserves qui accompagnent chaque chiffre, et ne se négocient pas

1. **L'unité de `volume` n'est pas établie.** Le champ pourrait compter des
   dollars de notionnel ou des parts. La vérifier exigerait d'aspirer un
   builder entier, or même les plus petits du top 50 dépassent 12 000
   exécutions. Tout revenu ESTIMÉ à partir du classement hérite de cette
   incertitude — d'où `RevenueEstimate.is_measured` à faux.
2. **L'échantillon d'exécutions est tronqué** dès que le builder dépasse
   `max_pages × 300` lignes. Les frais encaissés qu'on en tire sont un
   PLANCHER, jamais un total.
3. **Le taux mesuré est le plus haut jamais observé**, pas forcément l'actuel :
   l'endpoint mêle les époques sans les dater (cf. `fees`).

Un chiffre qui ne peut pas porter ces réserves n'est pas publié : le rapport
les imprime AVANT les nombres, comme `BacktestReport.sample_complaints`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .api import LeaderboardEntry, TradeSample
from .fees import BPS_PER_UNIT, FeeSchedule, infer_schedule

# Le classement « ALL » couvre l'historique entier du programme, dont le premier
# jour observé dans la série quotidienne est le 2025-10-10 : sa durée n'est donc
# pas une constante et ne figure pas ici.
PERIOD_DAYS = {"DAY": 1.0, "WEEK": 7.0, "MONTH": 30.0}


@dataclass(frozen=True)
class RevenueEstimate:
    """Ce qu'un builder encaisse — mesuré d'un côté, estimé de l'autre."""

    builder: str
    code: str
    rank: int
    active_users: int
    volume: float
    period: str
    schedule: FeeSchedule
    sample: TradeSample
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blended_bps(self) -> float | None:
        """Taux effectif tous côtés confondus, mesuré sur l'échantillon."""
        return self.schedule.blended_bps

    @property
    def measured_fee_usd(self) -> float:
        """Frais RÉELLEMENT vus dans l'échantillon. Un plancher si tronqué."""
        return self.schedule.measured_fee_usd

    @property
    def estimated_period_revenue_usd(self) -> float | None:
        """Revenu sur la période du classement — une ESTIMATION, pas une mesure.

        Produit du volume publié par le taux effectif mesuré. Rend `None` quand
        le taux n'a pas pu être mesuré : un revenu calculé sur un taux inconnu
        serait une invention, et zéro serait un mensonge différent.
        """
        bps = self.blended_bps
        if bps is None:
            return None
        return self.volume * bps / BPS_PER_UNIT

    @property
    def estimated_daily_revenue_usd(self) -> float | None:
        """Revenu ramené au jour, quand la période le permet."""
        total = self.estimated_period_revenue_usd
        days = PERIOD_DAYS.get(self.period.upper())
        if total is None or days is None:
            return None
        return total / days

    @property
    def revenue_per_user_usd(self) -> float | None:
        """Ce qu'un utilisateur actif rapporte — la vraie unité économique.

        C'est le chiffre qui dit si un builder vit de quelques baleines ou
        d'une audience. MESURÉ : Jupiter et MagicMarkets routent des millions
        avec 1 à 3 utilisateurs actifs ; polymtrade en a 31 289. Deux métiers
        différents sous le même classement.
        """
        total = self.estimated_period_revenue_usd
        if total is None or self.active_users <= 0:
            return None
        return total / self.active_users

    @property
    def is_measured(self) -> bool:
        """Faux : aucun revenu d'ici n'est une mesure directe.

        Seul `measured_fee_usd` en est une, et seulement sur l'échantillon vu.
        La propriété existe pour que l'affichage ne puisse pas l'oublier.
        """
        return False


def _caveats_for(
    entry: LeaderboardEntry, schedule: FeeSchedule, sample: TradeSample
) -> tuple[str, ...]:
    notes: list[str] = []
    if entry.volume_unit_is_assumed:
        notes.append("l'unité du volume publié n'a pas pu être vérifiée (dollars ou parts)")
    if sample.truncated:
        notes.append(
            f"échantillon tronqué à {len(sample)} exécutions ({sample.pages} pages) : "
            "les frais vus sont un plancher"
        )
    if schedule.exceeds_published_cap:
        notes.append(
            "taux supérieur au maximum publié (100 bps preneur / 50 bps teneur) — "
            "le plafond documenté n'est pas appliqué"
        )
    for side in (schedule.taker, schedule.maker):
        if side is not None and side.epochs_suspected:
            notes.append(
                f"côté {side.side} : plusieurs paliers ronds observés "
                f"({', '.join(f'{c:g}' for c in side.clusters)} bps) — le taux a changé"
            )
    if schedule.charges_nothing:
        notes.append("ce builder ne prélève rien : volume élevé, revenu nul")
    return tuple(notes)


def build_estimate(
    entry: LeaderboardEntry,
    sample: TradeSample,
    *,
    period: str = "ALL",
) -> RevenueEstimate:
    """Assemble le classement et les exécutions en un estimé annoté."""
    schedule = infer_schedule(sample.trades)
    return RevenueEstimate(
        builder=entry.builder,
        code=entry.code,
        rank=entry.rank,
        active_users=entry.active_users,
        volume=entry.volume,
        period=period.upper(),
        schedule=schedule,
        sample=sample,
        caveats=_caveats_for(entry, schedule, sample),
    )


def rank_by_revenue(estimates: Sequence[RevenueEstimate]) -> tuple[RevenueEstimate, ...]:
    """Reclasse par revenu estimé, décroissant.

    Les builders dont le taux n'a pas pu être mesuré sont rejetés en fin de
    liste plutôt que traités comme des zéros : « inconnu » et « gratuit » sont
    deux réponses différentes, et les confondre récompenserait l'absence de
    données.
    """

    def key(e: RevenueEstimate) -> tuple[int, float]:
        revenue = e.estimated_period_revenue_usd
        if revenue is None:
            return (1, 0.0)
        return (0, -revenue)

    return tuple(sorted(estimates, key=key))


@dataclass(frozen=True)
class Projection:
    """Ce que RAPPORTERAIT un volume donné à un barème donné.

    Sert à répondre à la seule question qui compte pour un builder qui démarre :
    combien faut-il router pour que ça vaille la peine. Aucune donnée de marché
    n'entre ici — c'est de l'arithmétique explicite, pas une prévision.
    """

    daily_volume_usd: float
    taker_bps: float
    maker_bps: float
    taker_share: float  # part du notionnel exécutée côté preneur, dans [0, 1]

    def __post_init__(self) -> None:
        if not 0.0 <= self.taker_share <= 1.0:
            raise ValueError(f"taker_share hors [0, 1] : {self.taker_share}")
        if self.daily_volume_usd < 0:
            raise ValueError(f"volume négatif : {self.daily_volume_usd}")

    @property
    def blended_bps(self) -> float:
        return self.taker_bps * self.taker_share + self.maker_bps * (1.0 - self.taker_share)

    @property
    def daily_usd(self) -> float:
        return self.daily_volume_usd * self.blended_bps / BPS_PER_UNIT

    @property
    def monthly_usd(self) -> float:
        return self.daily_usd * 30.0

    @property
    def yearly_usd(self) -> float:
        return self.daily_usd * 365.0


def volume_needed_for(
    target_daily_usd: float,
    *,
    taker_bps: float,
    maker_bps: float,
    taker_share: float = 0.5,
) -> float:
    """Volume quotidien requis pour atteindre un revenu visé.

    L'inverse de `Projection`, et la forme la plus utile de la question : à
    50 bps mélangés (0,50 %), viser 10 $/jour demande 2 000 $ de volume routé
    par jour, et 100 $/jour en demande 20 000. Ce chiffre doit être affiché
    avant qu'on écrive une ligne d'intégration, pas après.
    """
    probe = Projection(
        daily_volume_usd=1.0,
        taker_bps=taker_bps,
        maker_bps=maker_bps,
        taker_share=taker_share,
    )
    if probe.blended_bps <= 0:
        raise ValueError("un barème à 0 bps ne produit aucun revenu, quel que soit le volume")
    return target_daily_usd * BPS_PER_UNIT / probe.blended_bps
