"""Adaptateur « Prediction Trading » de Binance.

CE QUE CE PAQUET EST, ET CE QU'IL N'EST PAS.

Binance ne tient pas de marché de prédiction : il en revend un. Le change-log
officiel nomme le fournisseur en toutes lettres — `vendor = predict_fun`. Les
marchés atteints ici sont donc **les mêmes** que ceux de `donmarket/predictfun/`,
vus par une autre porte.

Ce qui change, et c'est décisif pour ce projet :

  - **L'accès.** Predict.fun en direct exige une clé mainnet distribuée par
    ticket Discord (401 sans elle) ET un portefeuille BNB Chain pour signer.
    Par Binance, c'est une clé d'API Binance ordinaire, signée en HMAC, avec la
    permission « Prediction Trading » cochée.
  - **L'argent.** Plus de pont on-chain : `transfer/outbound` fait passer les
    fonds du compte SPOT ou FUNDING vers le portefeuille de prédiction, en
    interne.
  - **Ce qui devient mesurable.** Predict.fun en direct n'offre NI historique
    de transactions NI historique de prix, ce qui rendait le taux d'exécution —
    seul terme qui transforme un rebate en rendement — non mesurable. Ici il y
    a `order/history`, `position/settled-history`, `pnl/query` et un flux
    carnet en direct. C'est la première fois que la mesure manquante devient
    atteignable.

CE QUI RESTE FAUX DE DIRE, tant qu'aucune clé n'a tourné :

  - Aucun rendement n'a été mesuré ici. Rien, dans ce paquet, ne produit un
    « %/jour » — pour la même raison que côté Predict.fun.
  - Les schémas de RÉPONSE REST ne sont publiés nulle part : les parseurs sont
    défensifs et lèvent au lieu de rendre du vide. Voir `model`.
  - Le chemin armé n'a jamais tourné. Comme le moteur Polymarket, il est écrit,
    plafonné et testé sur ce qu'il REFUSE de faire.

Paquet volontairement ISOLÉ : il n'importe ni `model.py`, ni `api/clob.py`, ni
`analysis/scoring.py`. La seule dépendance interne partagée est
`execute/limits.py`, un portier générique — le réécrire aurait produit deux
plafonds à tenir à jour au lieu d'un.
"""

from __future__ import annotations

from .api import BinancePredictionClient, Credentials
from .model import (
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionLevel,
    PredictionMarket,
    parse_book,
    parse_market,
)
from .signing import canonical_query, redact, sign, signed_query
from .trade import (
    ExecutionOutcome,
    PredictionOrder,
    PredictionTrader,
    Quote,
    parse_quote,
)

__all__ = [
    "BinanceApiError",
    "BinancePredictionClient",
    "BinanceSchemaError",
    "Credentials",
    "ExecutionOutcome",
    "PredictionBook",
    "PredictionLevel",
    "PredictionMarket",
    "PredictionOrder",
    "PredictionTrader",
    "Quote",
    "canonical_query",
    "parse_book",
    "parse_market",
    "parse_quote",
    "redact",
    "sign",
    "signed_query",
]
