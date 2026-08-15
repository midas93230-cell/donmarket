"""Ce qu'un builder encaisse réellement — et pourquoi le maximum, pas la médiane.

Un builder attache son code à chaque ordre qu'il route ; à l'exécution,
Polymarket prélève un **frais builder** en plus du frais de plateforme et le
verse au portefeuille du profil. C'est le seul mécanisme rencontré dans ce
dépôt qui rémunère un SERVICE et non une opinion sur un prix : il ne suppose
aucun edge, seulement du volume routé.

## La base de calcul : le notionnel USDC, et ce n'était pas évident

La doc écrit `builder_fee = notional × bps / 10000` sans dire ce que « notional »
désigne — or sur un marché binaire, une part vaut 1 $ à l'échéance, si bien que
« notionnel » peut légitimement vouloir dire *parts*. Les deux lectures diffèrent
d'un facteur `1/prix`, soit ×25 sur un marché à 0,04.

Tranché par la mesure du 2026-08-15, sur les 16 couples builder×côté du top 20
qui facturent quelque chose. Le bon modèle est celui dont le taux implicite est
CONSTANT ; on compare donc l'étalement `(max−min)/médiane` :

    base = sizeUsdc (notionnel)   étalement 0,000 à 0,492
    base = size     (parts)       étalement 0,801 à 2,341

Sans recouvrement entre les deux familles. MetaMask est le cas le plus net :
400,00 bps sur le notionnel avec un étalement de **0,000** sur 261 lignes, contre
3,82 à 399,60 bps sur les parts. La base est donc le notionnel USDC.

## Pourquoi le taux configuré s'estime par le MAXIMUM

Le taux implicite d'une ligne (`builderFee / sizeUsdc`) est systématiquement
**inférieur ou égal** au taux configuré : le montant versé est tronqué à une
poignée de décimales, et une troncature ne peut que diminuer. Les petites
lignes tirent donc la médiane vers le bas sans rien dire du réglage.

La mesure le confirme : les MAXIMA sont tous des nombres ronds — 5,00 / 10,00 /
25,00 / 50,00 / 100,00 / 400,00 bps — alors que les médianes et les minima ne le
sont pas (traderline : min 2,54 côté teneur pour un réglage à 5). On retient
donc le maximum, et on publie l'étalement pour que l'appelant puisse douter.

**Limite assumée** : un builder peut reconfigurer son taux. L'endpoint mêle les
époques sans les dater, donc le maximum est « le plus haut taux jamais observé »,
pas forcément celui d'aujourd'hui. `FeeSchedule.epochs_suspected` le signale
quand les lignes se regroupent nettement autour de plusieurs paliers ronds.

## Le frais de PLATEFORME est un autre animal

Il ne va pas au builder, il ne concerne que le preneur (MESURÉ : 162/162 lignes
teneur à zéro sur le code 0x00…01, puis 0 ligne teneur facturée sur les 936),
et sa forme n'est pas un pourcentage du notionnel mais une proportionnelle à la
VARIANCE :

    frais_plateforme = taux_marché × parts × prix × (1 − prix)

Vérifié en comparant les trois formes candidates sur 774 exécutions preneur :
`parts × p × (1−p)` donne un étalement de 1,03 contre 1,91 pour
`parts × min(p, 1−p)` et 1,99 pour le notionnel. Le `taux_marché` varie D'UN
MARCHÉ À L'AUTRE (0,0280 à 0,0720 mesurés) : aucune valeur n'est codée en dur
ici, la fonction l'exige en argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..config import (
    BUILDER_PUBLISHED_MAX_MAKER_BPS,
    BUILDER_PUBLISHED_MAX_TAKER_BPS,
)

BPS_PER_UNIT = 10_000.0

# En deçà de ce notionnel, la troncature du montant versé pèse plus lourd que
# le taux lui-même : une ligne à 1 $ facturée 400 bps vaut 0,04 $, et la mesure
# du 2026-08-15 en montre une à 0,000000. Ces lignes ne sont pas fausses, elles
# sont juste muettes sur le réglage — on les exclut de l'inférence, pas des
# totaux.
MIN_NOTIONAL_FOR_INFERENCE_USD = 5.0

# Deux taux implicites plus proches que cela sont considérés comme le même
# palier. La granularité publiée est de 1 bp ; on prend une marge.
BPS_CLUSTER_TOLERANCE = 0.5

# Marge sous laquelle un taux est considéré AU plafond et non au-dessus. La
# granularité publiée est de 1 bp : aucun réglage réel ne tombe dans cette
# demi-marge, seul le bruit de virgule flottante y vit.
CAP_TOLERANCE_BPS = 0.5


class FeeModelError(ValueError):
    """Entrée incompatible avec le modèle de frais."""


def builder_fee_usd(notional_usd: float, bps: float) -> float:
    """Frais builder d'une exécution, à partir du notionnel USDC.

    C'est la formule documentée, avec la base tranchée par la mesure. Elle est
    délibérément linéaire et sans plafond : le plafond publié (100 bps preneur)
    est démenti par MetaMask à 400 bps, donc l'imposer ici rendrait un chiffre
    faux sur un builder réel.
    """
    if notional_usd < 0:
        raise FeeModelError(f"notionnel négatif : {notional_usd}")
    if bps < 0:
        raise FeeModelError(f"taux négatif : {bps} bps")
    return notional_usd * bps / BPS_PER_UNIT


def platform_fee_usd(shares: float, price: float, market_rate: float) -> float:
    """Frais de plateforme d'une exécution PRENEUR (le teneur ne paie rien).

    Proportionnel à `prix × (1 − prix)`, donc maximal à 0,50 et quasi nul sur
    les issues très probables ou très improbables — l'inverse d'un pourcentage
    du notionnel. `market_rate` n'a pas de défaut à dessein : il varie d'un
    marché à l'autre (0,0280 à 0,0720 mesurés) et une valeur par défaut serait
    fausse la plupart du temps.
    """
    if not 0.0 <= price <= 1.0:
        raise FeeModelError(f"prix hors [0, 1] : {price}")
    if shares < 0:
        raise FeeModelError(f"parts négatives : {shares}")
    if market_rate < 0:
        raise FeeModelError(f"taux de marché négatif : {market_rate}")
    return market_rate * shares * price * (1.0 - price)


def published_max_bps(side: str) -> int:
    """Maximum ANNONCÉ pour ce côté — un repère, pas une règle (cf. MetaMask)."""
    key = side.upper()
    if key == "TAKER":
        return BUILDER_PUBLISHED_MAX_TAKER_BPS
    if key == "MAKER":
        return BUILDER_PUBLISHED_MAX_MAKER_BPS
    raise FeeModelError(f"côté inconnu : {side!r} (attendu TAKER ou MAKER)")


@dataclass(frozen=True)
class ImpliedRate:
    """Le taux d'un côté, tel qu'il ressort des exécutions observées."""

    side: str
    samples: int
    bps: float  # l'estimateur : le maximum observé
    min_bps: float
    median_bps: float
    notional_usd: float
    fee_usd: float
    clusters: tuple[float, ...]  # paliers ronds distincts repérés

    @property
    def dispersion(self) -> float:
        """`(max − min) / médiane` — 0 quand toutes les lignes s'accordent."""
        if self.median_bps <= 0:
            return float("inf")
        return (self.bps - self.min_bps) / self.median_bps

    @property
    def exceeds_published_cap(self) -> bool:
        """Vrai quand le taux dépasse VRAIMENT le maximum annoncé par la doc.

        La tolérance n'est pas une coquetterie. Le taux implicite se calcule par
        `frais / notionnel × 10000`, et un builder réglé pile à 50 bps produit
        `50.000000000000007` en virgule flottante. Sans marge, ce bit de bruit
        fait accuser un builder d'enfreindre un plafond public — alors que
        l'affichage, arrondi, montre 50,0 et donne l'accusation pour
        inexplicable.

        Cas réel : Bagel, mesuré à 100,0/50,0 — exactement aux deux plafonds —
        était signalé « au-dessus » à côté de MetaMask et RedotPay qui, eux,
        facturent 400 bps. Mélanger les deux aurait discrédité la seule mesure
        qui compte ici.

        Une demi-point de base de marge est sûr : la granularité publiée est de
        1 bp, donc aucun réglage réel ne se trouve dans cet intervalle.
        """
        return self.bps > published_max_bps(self.side) + CAP_TOLERANCE_BPS

    @property
    def epochs_suspected(self) -> bool:
        """Plusieurs paliers ronds nets : le taux a probablement changé."""
        return len(self.clusters) > 1


def _cluster_round_levels(values: Sequence[float]) -> tuple[float, ...]:
    """Repère les paliers ronds où les lignes s'accumulent.

    Un réglage produit une accumulation exacte à sa valeur (les grosses lignes,
    peu tronquées). Deux accumulations = deux réglages successifs, et donc un
    maximum qui ne décrit plus le présent. On ne retient qu'un palier réellement
    peuplé : une valeur isolée est du bruit de troncature, pas une époque.
    """
    if not values:
        return ()
    counts: dict[float, int] = {}
    for v in values:
        key = round(v, 2)
        counts[key] = counts.get(key, 0) + 1
    # Un palier crédible rassemble au moins 5 % des lignes, et au moins 3.
    floor = max(3, int(0.05 * len(values)))
    peaks = sorted(v for v, n in counts.items() if n >= floor)
    merged: list[float] = []
    for v in peaks:
        if merged and abs(v - merged[-1]) <= BPS_CLUSTER_TOLERANCE:
            merged[-1] = max(merged[-1], v)
            continue
        merged.append(v)
    return tuple(merged)


def infer_rate(trades: Iterable["object"], side: str) -> ImpliedRate | None:
    """Déduit le taux configuré d'un côté à partir des exécutions.

    `trades` porte des objets exposant `.trade_type`, `.notional_usd` et
    `.builder_fee_usd` (cf. `builder.api.BuilderTrade`). Rend `None` quand
    aucune ligne exploitable n'existe — un builder à 0 bps est un fait, pas une
    absence de données, et se distingue par `bps == 0` avec des échantillons.
    """
    key = side.upper()
    published_max_bps(key)  # valide le côté, lève sinon

    kept = [t for t in trades if getattr(t, "trade_type", "").upper() == key]
    if not kept:
        return None

    notional_total = sum(float(t.notional_usd) for t in kept)
    fee_total = sum(float(t.builder_fee_usd) for t in kept)

    usable = [
        t
        for t in kept
        if float(t.notional_usd) >= MIN_NOTIONAL_FOR_INFERENCE_USD
    ]
    if not usable:
        # Rien d'assez gros pour lire un taux : on rend quand même les totaux,
        # avec un estimateur global honnête plutôt que rien.
        implied = (fee_total / notional_total * BPS_PER_UNIT) if notional_total > 0 else 0.0
        return ImpliedRate(
            side=key,
            samples=len(kept),
            bps=implied,
            min_bps=implied,
            median_bps=implied,
            notional_usd=notional_total,
            fee_usd=fee_total,
            clusters=(),
        )

    rates = sorted(
        float(t.builder_fee_usd) / float(t.notional_usd) * BPS_PER_UNIT for t in usable
    )
    return ImpliedRate(
        side=key,
        samples=len(kept),
        bps=rates[-1],
        min_bps=rates[0],
        median_bps=rates[len(rates) // 2],
        notional_usd=notional_total,
        fee_usd=fee_total,
        clusters=_cluster_round_levels(rates),
    )


@dataclass(frozen=True)
class FeeSchedule:
    """Le barème d'un builder, des deux côtés, tel que MESURÉ."""

    taker: ImpliedRate | None
    maker: ImpliedRate | None

    @property
    def charges_nothing(self) -> bool:
        """Vrai quand le builder route du volume sans rien prélever.

        Ce n'est pas un cas marginal : au 2026-08-15, la plupart des premiers
        du classement (betmoar, Gate, polytraderpro, standtrade, Jupiter…)
        facturent 0 bps. Le volume n'est donc pas le revenu, et un classement
        par volume ne dit rien des ventes.
        """
        sides = [s for s in (self.taker, self.maker) if s is not None]
        return bool(sides) and all(s.bps == 0.0 for s in sides)

    @property
    def measured_fee_usd(self) -> float:
        """Somme réellement encaissée sur les lignes vues (aucune projection)."""
        return sum(s.fee_usd for s in (self.taker, self.maker) if s is not None)

    @property
    def measured_notional_usd(self) -> float:
        return sum(s.notional_usd for s in (self.taker, self.maker) if s is not None)

    @property
    def blended_bps(self) -> float | None:
        """Taux moyen effectif sur l'échantillon, tous côtés confondus.

        C'est CE taux, et non le taux preneur, qu'il faut appliquer à un volume
        pour estimer un revenu : la part teneur/preneur pèse autant que le
        barème. Mesuré : traderline facture 10/5 bps mais encaisse 7,32 bps en
        moyenne parce que 213 de ses 300 lignes sont du côté teneur.
        """
        if self.measured_notional_usd <= 0:
            return None
        return self.measured_fee_usd / self.measured_notional_usd * BPS_PER_UNIT

    @property
    def exceeds_published_cap(self) -> bool:
        return any(
            s.exceeds_published_cap for s in (self.taker, self.maker) if s is not None
        )


def infer_schedule(trades: Sequence["object"]) -> FeeSchedule:
    """Barème complet à partir d'un échantillon d'exécutions attribuées."""
    return FeeSchedule(
        taker=infer_rate(trades, "TAKER"),
        maker=infer_rate(trades, "MAKER"),
    )
