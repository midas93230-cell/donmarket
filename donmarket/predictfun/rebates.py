"""La formule de récompense de Predict.fun — et pourquoi ce n'est PAS celle de Polymarket.

Module PUR.

====================================================================
CE QUI A ÉTÉ MESURÉ / ÉTABLI LE 2026-08-09, ET CE QUI RESTE INCONNU
====================================================================

Predict.fun a DEUX mécanismes de rémunération du teneur. Un seul est calculable.

--- 1. MAKER REBATE (USDT réels) — CALCULABLE, implémenté ici ---

    « On every fill on an eligible market, the maker receives 25% of the taker
      fee paid on that fill. The rebate is paid directly in the fill
      transaction — there's no accrual, no claim step, and no minimum payout. »

Les frais du preneur suivent une formule publiée, VÉRIFIÉE contre le barème de
la doc (21 lignes, base 2 %, 100 parts) par `tests/test_predictfun.py` :

    frais_bruts = taux_base × min(prix, 1 − prix) × parts

D'où la récompense du teneur, par part exécutée :

    rebate/part = 0,25 × taux_base × min(prix, 1 − prix)

RENVERSEMENT COMPLET DE L'ÉCONOMIE PAR RAPPORT À POLYMARKET. Là-bas, un pool
quotidien est partagé au prorata d'un score de distance au milieu : on est payé
pour POSER et NE PAS être exécuté, et l'exécution est le coût (sélection
adverse). Ici, on n'est payé QUE si l'on est exécuté, et le montant ne dépend
ni de la taille au repos, ni de la distance au milieu, ni de la concurrence.

Ce que ça supprime : la dilution par les autres teneurs, le piège du « notre
ordre déplace le milieu qu'il mesure », `competing_q`, `own_q`, tout le calcul
de part de pool. Aucun de ces concepts n'a de sens ici.
Ce que ça n'enlève pas : la sélection adverse. Le rebate est encaissé
exactement au moment où l'on se fait ramasser du mauvais côté ; savoir s'il la
couvre est une question EMPIRIQUE, non résolue (voir `breakeven_adverse_move`).

Bornes de validité, à ne pas oublier :
  - Marchés éligibles : **UP/DOWN crypto uniquement** au lancement. L'API
    n'expose AUCUN champ d'éligibilité — la doc dit que c'est un badge dans
    l'interface. `looks_rebate_eligible` est donc une heuristique, pas une
    lecture.
  - Programme d'essai : **prend fin le 16/09/2026**. Un rendement calculé
    au-delà de cette date est nul, pas « probablement reconduit ».

--- 2. PREDICT POINTS (PP) — NON CALCULABLE, délibérément non implémenté ---

La doc décrit un second mécanisme, celui qui ressemble à Polymarket : un taux
PP/heure par marché, des instantanés fréquents du carnet, et des conditions
(taille minimale, écart sous le seuil du marché, les deux côtés, « the tightest
spreads earn the most », 5 niveaux d'étoiles).

Trois trous rendent tout chiffrage malhonnête :
  a) La FONCTION de score n'est publiée nulle part. « Les écarts les plus
     serrés gagnent le plus » n'est pas une formule.
  b) Le taux PP/hr n'est pas dans l'API. Le champ `rewards` existe
     (`{"current": null, "schedule": []}`) mais est resté vide sur les
     20 marchés distincts accessibles en testnet.
  c) Un PP n'a pas de valeur publiée en dollars.

Porter `analysis/scoring.py` ici pour boucher (a) reviendrait à inventer trois
paramètres et à les présenter comme une mesure. On ne le fait pas. Quand le
champ `rewards` se remplira sur mainnet, on mesurera ; d'ici là ce module
n'expose rien sur les points.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

# --- Constantes issues de la doc et de la mesure -------------------------

# Part des frais du preneur reversée au teneur, sur chaque exécution.
MAKER_REBATE_SHARE = 0.25

# Fin annoncée de l'essai. Après cette date, la récompense vaut zéro tant que
# Predict.fun n'a pas annoncé de reconduction.
REBATE_TRIAL_ENDS = date(2026, 9, 16)

# Taux de base mesuré en direct sur TOUS les marchés de l'échantillon :
# `feeRateBps = 200`. Sert de repli quand le marché ne porte pas son taux.
DEFAULT_FEE_RATE = 0.02

# Remise de 10 % sur les frais si un parrainage est actif. Elle réduit le rebate
# du teneur d'autant : la récompense est un pourcentage des frais RÉELLEMENT
# payés par le preneur, pas des frais théoriques.
REFERRAL_FEE_MULTIPLIER = 0.9

# Montant minimal d'un ordre, en USDT (doc « Limits »).
MIN_ORDER_USDT = 1.0

# Au lancement, seuls les marchés crypto UP/DOWN paient le rebate.
_UP_DOWN_MARKERS = ("up-down", "up_down", "updown", "up-or-down")


def taker_fee_per_share(
    price: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Frais payés par le PRENEUR, par part, à ce prix.

        frais/part = taux × min(prix, 1 − prix)

    Vérifié contre le barème publié (21 prix, base 2 %, 100 parts).

    Contre-intuitif et important : les frais culminent à p = 0,5 et s'effondrent
    aux extrêmes. Un pari à 0,99 coûte 0,02 % du notionnel, un pari à 0,50 en
    coûte 2 %. Raisonner en « pourcentage du notionnel » constant est faux.
    """
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"prix hors [0, 1] : {price}")
    raw = fee_rate * min(price, 1.0 - price)
    return raw * REFERRAL_FEE_MULTIPLIER if discounted else raw


def taker_fee(
    price: float, shares: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Frais totaux du preneur pour `shares` parts à `price`."""
    if shares < 0:
        raise ValueError(f"parts négatives : {shares}")
    return taker_fee_per_share(price, fee_rate=fee_rate, discounted=discounted) * shares


def taker_fee_pct_of_notional(
    price: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Frais en fraction du notionnel échangé — ce que « 2 % » veut dire ici.

        pct = taux × min(p, 1−p) / p

    Vaut le taux plein à p = 0,5 et tombe vers zéro quand p → 1.
    """
    if price <= 0.0:
        raise ValueError("prix nul : le pourcentage du notionnel n'est pas défini")
    return taker_fee_per_share(price, fee_rate=fee_rate, discounted=discounted) / price


def maker_rebate_per_share(
    price: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Récompense du TENEUR, par part EXÉCUTÉE.

        rebate/part = 0,25 × taux × min(prix, 1 − prix)

    Le mot « exécutée » porte tout le poids. Cette fonction ne dit rien du
    rendement d'un ordre posé : un ordre jamais rempli rapporte exactement zéro,
    quelles que soient sa taille et sa durée au carnet. Convertir ceci en
    « %/jour » exige un TAUX DE REMPLISSAGE mesuré, que ni le backtest ni l'API
    ne donnent — il n'existe sur Predict.fun ni endpoint de transactions ni
    historique de prix. Voir `rebate_yield_on_filled_notional`.
    """
    return MAKER_REBATE_SHARE * taker_fee_per_share(
        price, fee_rate=fee_rate, discounted=discounted
    )


def maker_rebate(
    price: float, shares: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Récompense totale du teneur pour `shares` parts exécutées à `price`."""
    if shares < 0:
        raise ValueError(f"parts négatives : {shares}")
    return maker_rebate_per_share(price, fee_rate=fee_rate, discounted=discounted) * shares


def rebate_yield_on_filled_notional(
    price: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Rendement du rebate en fraction du capital RÉELLEMENT engagé dans l'exécution.

        rendement = rebate/part ÷ prix = 0,25 × taux × min(p, 1−p) / p

    C'est la seule normalisation honnête disponible : elle rapporte le gain au
    dollar effectivement dépensé sur ce remplissage. Ce N'EST PAS un rendement
    par jour ni par dollar de capital immobilisé — il n'y a aucun facteur temps
    ici, parce que la donnée manquante (à quelle fréquence est-on exécuté) n'est
    pas mesurable au niveau d'accès actuel.

    Maximum à p = 0,5 : 0,25 × 0,02 × 0,5 / 0,5 = **0,5 %** du notionnel exécuté.
    """
    return maker_rebate_per_share(price, fee_rate=fee_rate, discounted=discounted) / price


def breakeven_adverse_move(
    price: float, *, fee_rate: float = DEFAULT_FEE_RATE, discounted: bool = False
) -> float:
    """Mouvement de prix défavorable, en points, qu'un rebate compense tout juste.

    Le rebate est encaissé exactement quand on est exécuté, c'est-à-dire quand
    quelqu'un veut le côté opposé au nôtre. La question qui décide de tout est
    donc : le rebate couvre-t-il la sélection adverse ? Cette fonction donne le
    seuil au-delà duquel la réponse est non.

    Lecture : à p = 0,5, base 2 %, le rebate vaut 0,0025 $/part. Le teneur est
    perdant dès que le prix bouge contre lui de plus de **0,25 cent** après
    exécution — soit un quart de pas sur un marché à `decimalPrecision = 2`.
    C'est une barre BASSE. Elle n'invalide pas la stratégie, elle dit que le
    verdict se joue sur une mesure de dérive post-exécution qui reste à faire.
    """
    return maker_rebate_per_share(price, fee_rate=fee_rate, discounted=discounted)


def program_is_running(when: date | datetime | None = None) -> bool:
    """L'essai de maker rebate est-il encore en cours à cette date ?

    Sans reconduction annoncée, tout rendement postérieur au 16/09/2026 vaut
    zéro. On préfère un faux négatif à un rendement fantôme.
    """
    if when is None:
        when = datetime.now(timezone.utc).date()
    elif isinstance(when, datetime):
        when = when.astimezone(timezone.utc).date()
    return when <= REBATE_TRIAL_ENDS


def looks_rebate_eligible(category_slug: str, market_variant: str | None = None) -> bool:
    """HEURISTIQUE d'éligibilité au rebate — ce n'est PAS une lecture de l'API.

    La doc limite le rebate aux marchés crypto UP/DOWN et dit que l'éligibilité
    se voit à un badge dans l'interface. Aucun champ de `/v1/markets` ne porte
    ce badge : ni `rewards`, ni `marketVariant`, ni rien d'autre — vérifié sur
    l'ensemble des champs renvoyés.

    On reconnaît donc le motif dans `categorySlug` (mesuré :
    « btc-usd-up-down-2025-12-07-00-00 ») et dans `marketVariant`
    (« CRYPTO_UP_DOWN » côté schéma). Un vrai/faux rendu ici doit être présenté
    comme une supposition, jamais comme un fait — c'est ce que fait `scan.py`.
    """
    haystack = f"{category_slug} {market_variant or ''}".casefold()
    if "crypto_up_down" in haystack.replace("-", "_"):
        return True
    return any(marker in haystack for marker in _UP_DOWN_MARKERS)
