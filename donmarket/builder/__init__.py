"""Programme Builders de Polymarket : attribution, frais, et revenus mesurés.

Paquet volontairement autonome, comme `predictfun/` et `binance/` : il n'importe
ni `model.py`, ni `analysis/scoring.py`, ni `api/clob.py`. La logique de
récompenses de liquidité (score `S(v,s)`, seaux `Qone/Qtwo`, piège du milieu qui
se déplace) n'a AUCUN sens ici — un frais builder se prélève sur le notionnel
exécuté, sans carnet, sans concurrence et sans distance au milieu.

Le mécanisme change aussi de nature : on n'est plus payé pour avoir raison sur
un prix, mais pour router du volume. C'est le seul revenu du dépôt qui ne
suppose aucun edge.
"""

from .api import (
    BuilderApiError,
    BuilderTrade,
    DailyVolume,
    LeaderboardEntry,
    TradeSample,
    build_clob_client,
    build_data_client,
    fetch_builder_trades,
    fetch_daily_volume,
    fetch_leaderboard,
)
from .attribution import (
    AttributionNotConfigured,
    BuilderAttribution,
    attribution_status,
    build_builder_config,
    load_attribution,
)
from .codes import (
    BuilderCode,
    InvalidBuilderCode,
    is_valid_builder_code,
    normalise_builder_code,
)
from .fees import (
    FeeModelError,
    FeeSchedule,
    ImpliedRate,
    builder_fee_usd,
    infer_rate,
    infer_schedule,
    platform_fee_usd,
    published_max_bps,
)
from .revenue import (
    Projection,
    RevenueEstimate,
    build_estimate,
    rank_by_revenue,
    volume_needed_for,
)

__all__ = [
    "AttributionNotConfigured",
    "BuilderApiError",
    "BuilderAttribution",
    "BuilderCode",
    "BuilderTrade",
    "DailyVolume",
    "FeeModelError",
    "FeeSchedule",
    "ImpliedRate",
    "InvalidBuilderCode",
    "LeaderboardEntry",
    "Projection",
    "RevenueEstimate",
    "TradeSample",
    "attribution_status",
    "build_builder_config",
    "build_clob_client",
    "build_data_client",
    "build_estimate",
    "builder_fee_usd",
    "fetch_builder_trades",
    "fetch_daily_volume",
    "fetch_leaderboard",
    "infer_rate",
    "infer_schedule",
    "is_valid_builder_code",
    "load_attribution",
    "normalise_builder_code",
    "platform_fee_usd",
    "published_max_bps",
    "rank_by_revenue",
    "volume_needed_for",
]
