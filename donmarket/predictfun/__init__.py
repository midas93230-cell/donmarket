"""Adaptateur Predict.fun — la « Prédiction » que Binance Wallet affiche.

Predict.fun est un marché de prédiction on-chain sur BNB Chain. Binance n'en est
pas la contrepartie : c'est le fournisseur officiel de prédiction de Binance,
intégré dans son portefeuille. **Une clé API Binance n'y donne aucun accès.**

Ce paquet est VOLONTAIREMENT séparé du reste de DONmarket. Rien n'y est importé
de `donmarket.model`, `donmarket.api.clob` ni `donmarket.analysis.scoring` :
les quatre hypothèses structurantes de Polymarket sont FAUSSES ici, et chacune
échoue en silence si on les recycle. Mesuré le 2026-08-09 sur api-testnet :

1. Polymarket sert DEUX carnets par marché (un par token). Predict.fun en sert
   UN par marché, du point de vue « Yes ». Le côté « No » est DÉRIVÉ.
2. Polymarket ordonne les bids par prix CROISSANT (meilleur en dernier).
   Predict.fun les ordonne par prix DÉCROISSANT (meilleur en premier).
3. Polymarket décrit un palier par `{"price": "0.5", "size": "100"}` (chaînes).
   Predict.fun par la paire `[0.5, 100]` (flottants natifs).
4. Polymarket paie un POOL quotidien partagé au prorata d'un score de distance
   au milieu — on est payé pour NE PAS être rempli. Predict.fun paie 25 % des
   frais du preneur au teneur SUR CHAQUE EXÉCUTION — on est payé pour ÊTRE
   rempli. Porter `scoring.py` ici produirait un chiffre inventé.

Voir `rebates.py` pour la formule de récompense et ses bornes de validité.
"""

from __future__ import annotations

from .model import (
    PredictBook,
    PredictLevel,
    PredictMarket,
    PredictOutcome,
    PredictSchemaError,
    parse_book,
    parse_market,
)
from .rebates import (
    MAKER_REBATE_SHARE,
    REBATE_TRIAL_ENDS,
    maker_rebate_per_share,
    program_is_running,
    taker_fee_per_share,
)

__all__ = [
    "MAKER_REBATE_SHARE",
    "PredictBook",
    "PredictLevel",
    "PredictMarket",
    "PredictOutcome",
    "PredictSchemaError",
    "REBATE_TRIAL_ENDS",
    "maker_rebate_per_share",
    "parse_book",
    "parse_market",
    "program_is_running",
    "taker_fee_per_share",
]
