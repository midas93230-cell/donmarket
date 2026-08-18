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

# --- Programme Builders (frais sur le volume routé) ----------------------
# Les statistiques builder ne vivent PAS sur gamma ni sur le CLOB mais sur une
# troisième base publique, sans clé (MESURÉ le 2026-08-15).
DATA_API_BASE = "https://data-api.polymarket.com"

# MESURÉ : `/builder/trades` sert 300 lignes par page et IGNORE `limit`
# (limit=5 → 300 servies, limit=1000 → 300 servies).
BUILDER_PAGE_SIZE = 300

# MESURÉ : contrairement à Gamma et à Predict.fun, le curseur AVANCE ici. Il
# encode l'offset en base64 (« MzAw » = 300, « NjAw » = 600) et la fin de flux
# est signalée par base64 de « -1 ». Une boucle qui ne connaît pas ce sentinelle
# repart de l'offset -1 et repagine indéfiniment.
BUILDER_CURSOR_END = "LTE="

# Garde-fou d'aspiration : MESURÉ le 2026-08-15, même les plus petits builders
# du top 50 dépassent 12 000 exécutions. Aspirer un builder entier n'est pas un
# geste anodin, d'où un plafond explicite que l'appelant doit lever sciemment.
BUILDER_MAX_PAGES = 40

# Maxima PUBLIÉS sur docs.polymarket.com/programs/builders/fees.
# MESURÉ le 2026-08-15 : ce ne sont PAS des plafonds durs. MetaMask facture
# 400,00 bps côté preneur (étalement du taux implicite : 0,000 sur 261 lignes),
# soit 4× le maximum annoncé. Ces constantes servent donc à SIGNALER un
# dépassement, jamais à rejeter une mesure au motif qu'elle les excède.
BUILDER_PUBLISHED_MAX_TAKER_BPS = 100
BUILDER_PUBLISHED_MAX_MAKER_BPS = 50


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

# Route PUBLIQUE et non signée qui donne l'heure du serveur. C'est la seule du
# domaine qui réponde sans clé — et c'est précisément ce qu'il faut, puisqu'on
# l'interroge quand la signature vient d'être refusée.
BINANCE_TIME_PATH = "/api/v3/time"

# MESURÉ le 2026-08-18 sur cette machine : horloge en avance de ~6 000 ms sur
# Binance, `w32time` arrêté depuis des semaines et dérive libre. Binance refuse
# au-delà de 1 000 ms d'AVANCE quelle que soit `recvWindow` — augmenter
# `recvWindow` ne répare donc RIEN dans ce sens-là. Au-delà de ce seuil, on
# journalise l'écart : une horloge qui dérive est un fait d'exploitation, pas
# un détail de mise au point.
BINANCE_CLOCK_SKEW_WARN_MS = 500

# Minimum d'un ordre MARKET documenté dans le change-log du 2026-06-16 :
# `amountIn` ≳ 1,5 USDT, « varies by market depth ». Les LIMIT n'y sont pas
# soumis. Valeur indicative, à revérifier en direct dès qu'une clé existe.
BINANCE_MARKET_ORDER_MIN_USDT = 1.5

# PIÈGE D'UNITÉ MESURÉ le 2026-08-18. `amountIn` et `feeAmount` sont exprimés
# en unités de base à 18 décimales, pas en USDT — devis réel : `amountIn`
# "2000000000000000000" pour 2 USDT. Envoyer `8.0` demande huit wei et rend
# `-9000 order amount is too small`, message qui désigne le solde alors que la
# faute est à l'unité.
BINANCE_COLLATERAL_DECIMALS = 18


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
