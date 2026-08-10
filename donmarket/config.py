"""Configuration centrale de DONmarket.

Aucun secret n'est codé en dur : tout vient de l'environnement ou du .env local.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# Points d'entrée publics de Polymarket (lecture seule, aucune clé requise).
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CLOB_WS_BASE = "wss://ws-subscriptions-clob.polymarket.com/ws"

# MESURÉ sur l'API, pas supposé : Gamma plafonne à 100 marchés par page quelle
# que soit la limite demandée, et refuse (422) tout offset au-delà de ~2100.
# Le CLOB sert 1000 par page mais commence par ~50 000 marchés déjà clos.
CLOB_PAGE_SIZE = 1000
GAMMA_PAGE_SIZE = 100
GAMMA_MAX_OFFSET = 2100

# Un carnet d'ordres par requête serait trop lent sur des dizaines de milliers
# de tokens : le CLOB expose un endpoint groupé acceptant plusieurs token_id.
BOOKS_BATCH_SIZE = 100

# --- Predict.fun (la « Prédiction » de Binance Wallet) -------------------
# Place de marché distincte, sur BNB Chain. Aucun réglage Polymarket ne s'y
# applique. Voir donmarket/predictfun/ pour ce qui a été mesuré.
PREDICT_BASE_URLS = {
    "testnet": "https://api-testnet.predict.fun",
    "mainnet": "https://api.predict.fun",
}

# MESURÉ le 2026-08-09 : le serveur plafonne une page à 20 lignes quelle que soit
# la valeur de `limit` (100 demandées → 20 servies).
PREDICT_PAGE_SIZE = 20

# MESURÉ : sur testnet le curseur NE BOUGE JAMAIS — 30 pages demandées ont rendu
# 600 lignes pour 20 marchés distincts. Ce plafond n'est donc pas un budget de
# performance, c'est un garde-fou au cas où la détection de piétinement
# échouerait. Le quota annoncé est de 240 requêtes/minute.
PREDICT_MAX_PAGES = 25


# --- Binance Prediction Trading (le MÊME Predict.fun, par la porte Binance) ---
# VÉRIFIÉ le 2026-08-09 contre api.binance.com : les 26 routes documentées
# existent en production (une route inventée rend 404, les vraies rendent -2014).
# Le change-log Binance nomme le fournisseur : `vendor = predict_fun`. C'est donc
# la même place de marché que donmarket/predictfun/, atteinte par le compte
# Binance au lieu d'une clé Discord et d'un portefeuille BNB Chain.
BINANCE_BASE = "https://api.binance.com"
BINANCE_PREDICTION_PREFIX = "/sapi/v1/w3w/wallet/prediction"
BINANCE_PREDICTION_WS = "wss://api.binance.com/sapi/wss"

# MESURÉ le 2026-08-09 : contrairement aux données de marché du spot, AUCUNE
# route prédiction n'est publique. Sans en-tête `X-MBX-APIKEY` → `-2014
# API-key format invalid` ; avec une clé bidon → `-2008 Invalid Api-Key ID`.
# Même `category/list` et `order-book`. Il n'existe donc pas de mode lecture
# anonyme, et pas de testnet documenté pour ce produit.
BINANCE_PREDICTION_NEEDS_KEY = True

# Fenêtre de validité d'une requête signée. Binance rejette (-1021) au-delà,
# ce qui rend le module sensible à l'horloge de la machine.
BINANCE_RECV_WINDOW_MS = 5000

# Minimum d'un ordre MARKET documenté dans le change-log du 2026-06-16 :
# `amountIn` ≳ 1,5 USDT, « varies by market depth ». Les LIMIT n'y sont pas
# soumis. Valeur indicative, à revérifier en direct dès qu'une clé existe.
BINANCE_MARKET_ORDER_MIN_USDT = 1.5


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un nombre, reçu {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True)
class Settings:
    """Réglages d'exécution, immuables une fois chargés."""

    data_dir: Path
    db_path: Path
    http_timeout: float
    max_concurrency: int
    user_agent: str

    @staticmethod
    def load() -> "Settings":
        data_dir = Path(os.getenv("DONMARKET_DATA_DIR") or (ROOT_DIR / "data"))
        return Settings(
            data_dir=data_dir,
            db_path=data_dir / "donmarket.db",
            http_timeout=_env_float("DONMARKET_HTTP_TIMEOUT", 30.0),
            max_concurrency=_env_int("DONMARKET_MAX_CONCURRENCY", 8),
            user_agent=os.getenv("DONMARKET_USER_AGENT", "donmarket/0.1"),
        )


SETTINGS = Settings.load()
