"""Persistance SQLite — la mémoire de DONmarket.

Un scan ponctuel dit ce que le marché offre maintenant ; une base dit comment il
évolue. C'est cette histoire, et elle seule, qui permettra plus tard un backtest
honnête plutôt qu'une intuition.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Iterator, Sequence

from ..analysis.opportunities import Opportunity
from ..analysis.rewards import RewardCandidate
from ..config import SETTINGS
from ..model import Market

if TYPE_CHECKING:  # importé pour le typage seul : `scan` importe ce module.
    from ..scan.rewards_scan import RewardScanResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id       TEXT PRIMARY KEY,
    question           TEXT NOT NULL,
    slug               TEXT,
    volume             REAL,
    volume_24h         REAL,
    liquidity          REAL,
    best_bid           REAL,
    best_ask           REAL,
    spread             REAL,
    min_order_size     REAL,
    min_tick_size      REAL,
    end_date           TEXT,
    active             INTEGER,
    closed             INTEGER,
    accepting_orders   INTEGER,
    order_book_enabled INTEGER,
    neg_risk           INTEGER,
    rewards_min_size   REAL,
    rewards_max_spread REAL,
    raw_json           TEXT,
    scanned_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    condition_id TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    name         TEXT,
    token_id     TEXT,
    price        REAL,
    PRIMARY KEY (condition_id, idx)
);

CREATE TABLE IF NOT EXISTS scans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    mode           TEXT,
    markets_seen   INTEGER,
    markets_traded INTEGER,
    books_fetched  INTEGER,
    found          INTEGER
);

CREATE TABLE IF NOT EXISTS opportunities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id             INTEGER NOT NULL,
    condition_id        TEXT NOT NULL,
    kind                TEXT NOT NULL,
    edge                REAL,
    gross_edge          REAL,
    sum_price           REAL,
    depth_usd           REAL,
    spread              REAL,
    volume_24h          REAL,
    days_to_resolution  REAL,
    max_shares          REAL,
    observed_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reward_scans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at       TEXT NOT NULL,
    mode              TEXT,
    bankroll          REAL,
    markets_seen      INTEGER,
    rewarded          INTEGER,
    alive             INTEGER,
    affordable        INTEGER,
    books_fetched     INTEGER,
    histories_fetched INTEGER,
    duration_seconds  REAL,
    found             INTEGER
);

CREATE TABLE IF NOT EXISTS reward_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    question     TEXT,
    slug         TEXT,
    daily_pool   REAL,
    competing_q  REAL,
    own_q        REAL,
    engaged_usd  REAL,
    gross_yield  REAL,
    drift        REAL,
    replay_cost  REAL,
    oscillation  REAL,
    net_yield    REAL,
    hours_left   REAL,
    actionable   INTEGER NOT NULL,
    rejected_by  TEXT,
    token_ids    TEXT,
    max_spread   REAL,
    observed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_markets_volume  ON markets(volume_24h DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_token  ON outcomes(token_id);
CREATE INDEX IF NOT EXISTS idx_opps_scan       ON opportunities(scan_id);
CREATE INDEX IF NOT EXISTS idx_rc_scan         ON reward_candidates(scan_id);
CREATE INDEX IF NOT EXISTS idx_rc_market       ON reward_candidates(condition_id, observed_at);
"""

# Les tables comptées par `stats`. Une liste explicite plutôt qu'une lecture de
# `sqlite_master` : ajouter une table sans décider si elle compte est une
# occasion de la voir apparaître dans un rapport sans que personne l'ait voulu.
COUNTED_TABLES = (
    "markets",
    "outcomes",
    "scans",
    "opportunities",
    "reward_scans",
    "reward_candidates",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Colonnes ajoutées après la création initiale du schéma. `CREATE TABLE IF NOT
# EXISTS` ne touche pas une table déjà présente : sans ce rattrapage, les bases
# d'avant la colonne resteraient dans l'ancien format et l'insertion échouerait.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("reward_candidates", "replay_cost", "REAL"),
)


def _migrate(connection: sqlite3.Connection) -> None:
    """Ajoute les colonnes manquantes aux bases déjà créées. Idempotent."""
    for table, column, kind in MIGRATIONS:
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Ouvre la base, crée le schéma si besoin, et valide en sortie."""
    path = db_path or SETTINGS.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA)
        _migrate(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_markets(connection: sqlite3.Connection, markets: Sequence[Market]) -> int:
    """Écrit ou met à jour des marchés et leurs branches. Renvoie le compte."""
    scanned_at = _now_iso()

    market_rows = [
        (
            market.condition_id,
            market.question,
            market.slug,
            market.volume,
            market.volume_24h,
            market.liquidity,
            market.best_bid,
            market.best_ask,
            market.spread,
            market.min_order_size,
            market.min_tick_size,
            market.end_date.isoformat() if market.end_date else None,
            int(market.active),
            int(market.closed),
            int(market.accepting_orders),
            int(market.order_book_enabled),
            int(market.neg_risk),
            market.rewards_min_size,
            market.rewards_max_spread,
            json.dumps(market.raw, separators=(",", ":")),
            scanned_at,
        )
        for market in markets
    ]

    connection.executemany(
        """
        INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(condition_id) DO UPDATE SET
            question=excluded.question,
            slug=excluded.slug,
            volume=excluded.volume,
            volume_24h=excluded.volume_24h,
            liquidity=excluded.liquidity,
            best_bid=excluded.best_bid,
            best_ask=excluded.best_ask,
            spread=excluded.spread,
            min_order_size=excluded.min_order_size,
            min_tick_size=excluded.min_tick_size,
            end_date=excluded.end_date,
            active=excluded.active,
            closed=excluded.closed,
            accepting_orders=excluded.accepting_orders,
            order_book_enabled=excluded.order_book_enabled,
            neg_risk=excluded.neg_risk,
            rewards_min_size=excluded.rewards_min_size,
            rewards_max_spread=excluded.rewards_max_spread,
            raw_json=excluded.raw_json,
            scanned_at=excluded.scanned_at
        """,
        market_rows,
    )

    outcome_rows = [
        (market.condition_id, idx, outcome.name, outcome.token_id, outcome.price)
        for market in markets
        for idx, outcome in enumerate(market.outcomes)
    ]
    connection.executemany(
        "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)", outcome_rows
    )

    return len(market_rows)


def start_scan(connection: sqlite3.Connection, mode: str) -> int:
    cursor = connection.execute(
        "INSERT INTO scans (started_at, mode) VALUES (?,?)", (_now_iso(), mode)
    )
    return int(cursor.lastrowid or 0)


def finish_scan(
    connection: sqlite3.Connection,
    scan_id: int,
    *,
    markets_seen: int,
    markets_traded: int,
    books_fetched: int,
    found: int,
) -> None:
    connection.execute(
        """
        UPDATE scans SET finished_at=?, markets_seen=?, markets_traded=?,
                         books_fetched=?, found=?
        WHERE id=?
        """,
        (_now_iso(), markets_seen, markets_traded, books_fetched, found, scan_id),
    )


def record_opportunities(
    connection: sqlite3.Connection, scan_id: int, opportunities: Sequence[Opportunity]
) -> int:
    observed_at = _now_iso()
    connection.executemany(
        """
        INSERT INTO opportunities
            (scan_id, condition_id, kind, edge, gross_edge, sum_price, depth_usd,
             spread, volume_24h, days_to_resolution, max_shares, observed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_id,
                opp.condition_id,
                opp.kind,
                opp.edge,
                opp.gross_edge,
                opp.sum_price,
                opp.depth_usd,
                opp.spread,
                opp.volume_24h,
                opp.days_to_resolution,
                opp.max_shares,
                observed_at,
            )
            for opp in opportunities
        ],
    )
    return len(opportunities)


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Compteurs pour la commande `stats`."""
    result: dict[str, int] = {}
    for table in COUNTED_TABLES:
        row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        result[table] = int(row["n"])
    return result


# --- Récompenses : le relevé, et ce qu'il faut pour lire une série ----------


def encode_token_ids(token_ids: Sequence[str]) -> str:
    return json.dumps(list(token_ids), separators=(",", ":"))


def decode_token_ids(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(json.loads(raw))


def record_reward_scan(
    connection: sqlite3.Connection, result: "RewardScanResult"
) -> int:
    """Enregistre un balayage de récompenses et TOUS ses candidats mesurés.

    Les rejetés sont écrits au même titre que les retenus, avec leur motif. Ne
    garder que les retenus paraîtrait économe et rendrait la base inutilisable
    pour la seule question qu'elle doit trancher : un candidat qui affichait
    +42 %/jour vaut-il encore quelque chose au balayage suivant ? S'il n'est
    inscrit que les jours où il passe le seuil, sa série ne contient que ses
    bons jours et toute lecture de persistance est flatteuse par construction.
    """
    observed_at = _now_iso()
    cursor = connection.execute(
        """
        INSERT INTO reward_scans
            (observed_at, mode, bankroll, markets_seen, rewarded, alive,
             affordable, books_fetched, histories_fetched, duration_seconds, found)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            observed_at,
            result.mode.value,
            result.bankroll,
            result.markets_seen,
            result.rewarded,
            result.alive,
            result.affordable,
            result.books_fetched,
            result.histories_fetched,
            result.duration_seconds,
            result.found,
        ),
    )
    scan_id = int(cursor.lastrowid or 0)

    candidates = (*result.candidates, *result.near_misses)
    connection.executemany(
        """
        INSERT INTO reward_candidates
            (scan_id, condition_id, question, slug, daily_pool, competing_q,
             own_q, engaged_usd, gross_yield, drift, oscillation, net_yield,
             hours_left, actionable, rejected_by, token_ids, max_spread,
             replay_cost, observed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                scan_id,
                candidate.condition_id,
                candidate.question,
                candidate.slug,
                candidate.daily_pool,
                candidate.competing_q,
                candidate.own_q,
                candidate.engaged_usd,
                candidate.gross_yield,
                candidate.drift,
                candidate.oscillation,
                candidate.net_yield,
                candidate.hours_left,
                int(candidate.is_actionable),
                " | ".join(candidate.rejected_by),
                encode_token_ids(candidate.token_ids),
                candidate.max_spread,
                candidate.replay_cost,
                observed_at,
            )
            for candidate in candidates
        ],
    )
    return scan_id


@dataclass(frozen=True)
class CandidateTrack:
    """Le même marché, revu balayage après balayage.

    `net_median` n'est pas là pour faire joli : la quatrième mesure a montré
    qu'un instantané peut surestimer d'un facteur 2 à 5. L'écart entre
    `net_min` et `net_max` dit combien de ce chiffre est du bruit.
    """

    condition_id: str
    question: str
    observations: int
    actionable_count: int
    first_seen: str
    last_seen: str
    net_first: float
    net_last: float
    net_median: float
    net_min: float
    net_max: float

    @property
    def actionable_rate(self) -> float:
        """Part des relevés où ce marché franchissait réellement les seuils."""
        if not self.observations:
            return 0.0
        return self.actionable_count / self.observations


def candidate_tracks(
    connection: sqlite3.Connection,
    *,
    min_observations: int = 2,
    limit: int = 50,
) -> list[CandidateTrack]:
    """Les séries de candidats, du plus souvent revu au moins souvent.

    Un marché vu une seule fois est écarté par défaut : sa « série » est un
    instantané, et c'est précisément ce dont on cherche à s'affranchir.
    """
    rows = connection.execute(
        """
        SELECT condition_id, question, net_yield, actionable, observed_at
        FROM reward_candidates
        ORDER BY condition_id, observed_at, id
        """
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["condition_id"], []).append(row)

    tracks = [
        CandidateTrack(
            condition_id=condition_id,
            question=group[-1]["question"] or "",
            observations=len(group),
            actionable_count=sum(int(row["actionable"]) for row in group),
            first_seen=group[0]["observed_at"],
            last_seen=group[-1]["observed_at"],
            net_first=float(group[0]["net_yield"]),
            net_last=float(group[-1]["net_yield"]),
            net_median=float(median(float(row["net_yield"]) for row in group)),
            net_min=min(float(row["net_yield"]) for row in group),
            net_max=max(float(row["net_yield"]) for row in group),
        )
        for condition_id, group in grouped.items()
        if len(group) >= min_observations
    ]
    tracks.sort(key=lambda track: (track.observations, track.net_median), reverse=True)
    return tracks[:limit]
