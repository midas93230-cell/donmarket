"""Modèle de données et normalisation des réponses Polymarket.

Ce module est volontairement PUR : aucun accès réseau, aucun accès disque.
Toute la logique fragile (l'API renvoie des nombres sous forme de chaînes, et
certaines listes sont des chaînes JSON imbriquées) est isolée ici pour être
testable sans dépendre d'internet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence


def to_float(value: Any, default: float | None = None) -> float | None:
    """Convertit une valeur d'API en flottant, sans jamais lever d'exception.

    L'API mélange nombres réels, chaînes ("0.0195"), None et chaînes vides.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def to_list(value: Any) -> list[Any]:
    """Décode les champs qui sont parfois une liste, parfois une chaîne JSON.

    `outcomes`, `outcomePrices` et `clobTokenIds` arrivent en `'["Yes", "No"]'`.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return []
        try:
            decoded = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        return list(decoded) if isinstance(decoded, list) else []
    return []


def parse_iso(value: Any) -> datetime | None:
    """Lit une date ISO 8601 de l'API (ex. "2026-12-31T00:00:00Z") en UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Outcome:
    """Une branche d'un marché (« Yes » / « No », ou un candidat nommé)."""

    name: str
    token_id: str
    price: float | None


@dataclass(frozen=True)
class Market:
    """Un marché Polymarket normalisé.

    Immuable : toute mise à jour produit un nouvel objet (voir coding-style).
    """

    condition_id: str
    question: str
    slug: str
    outcomes: tuple[Outcome, ...]
    volume: float
    volume_24h: float
    liquidity: float
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    min_order_size: float
    min_tick_size: float
    end_date: datetime | None
    active: bool
    closed: bool
    accepting_orders: bool
    order_book_enabled: bool
    neg_risk: bool
    rewards_min_size: float | None
    rewards_max_spread: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def token_ids(self) -> tuple[str, ...]:
        return tuple(o.token_id for o in self.outcomes if o.token_id)

    @property
    def is_tradable(self) -> bool:
        """Un marché sur lequel un ordre pourrait réellement être posé."""
        return (
            self.active
            and not self.closed
            and self.accepting_orders
            and self.order_book_enabled
            and len(self.token_ids) >= 2
        )

    @property
    def has_rewards(self) -> bool:
        """Vrai si le marché paie des récompenses de liquidité (stratégie LP)."""
        return bool(self.rewards_min_size) and bool(self.rewards_max_spread)

    def notional_min_order(self) -> float | None:
        """Coût en dollars du plus petit ordre autorisé, au meilleur ask.

        Répond à une question très concrète : « avec X dollars, ai-je seulement
        le droit d'entrer sur ce marché ? »
        """
        if self.best_ask is None:
            return None
        return self.min_order_size * self.best_ask


def parse_gamma_market(raw: dict[str, Any]) -> Market | None:
    """Normalise un marché de l'API Gamma. Renvoie None si inexploitable."""
    condition_id = raw.get("conditionId") or ""
    if not isinstance(condition_id, str) or not condition_id:
        return None

    names = [str(n) for n in to_list(raw.get("outcomes"))]
    token_ids = [str(t) for t in to_list(raw.get("clobTokenIds"))]
    prices = [to_float(p) for p in to_list(raw.get("outcomePrices"))]

    outcomes = tuple(
        Outcome(
            name=names[i] if i < len(names) else f"outcome_{i}",
            token_id=token_ids[i] if i < len(token_ids) else "",
            price=prices[i] if i < len(prices) else None,
        )
        for i in range(max(len(names), len(token_ids)))
    )

    return Market(
        condition_id=condition_id,
        question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""),
        outcomes=outcomes,
        volume=to_float(raw.get("volumeNum"), 0.0) or 0.0,
        volume_24h=to_float(raw.get("volume24hr"), 0.0) or 0.0,
        liquidity=to_float(raw.get("liquidityNum"), 0.0) or 0.0,
        best_bid=to_float(raw.get("bestBid")),
        best_ask=to_float(raw.get("bestAsk")),
        spread=to_float(raw.get("spread")),
        min_order_size=to_float(raw.get("orderMinSize"), 0.0) or 0.0,
        min_tick_size=to_float(raw.get("orderPriceMinTickSize"), 0.0) or 0.0,
        end_date=parse_iso(raw.get("endDateIso") or raw.get("endDate")),
        active=bool(raw.get("active")),
        closed=bool(raw.get("closed")),
        accepting_orders=bool(raw.get("acceptingOrders")),
        order_book_enabled=bool(raw.get("enableOrderBook")),
        neg_risk=bool(raw.get("negRisk")),
        rewards_min_size=to_float(raw.get("rewardsMinSize")),
        rewards_max_spread=to_float(raw.get("rewardsMaxSpread")),
        raw=raw,
    )


def parse_gamma_markets(rows: Sequence[dict[str, Any]]) -> list[Market]:
    """Normalise une page entière, en ignorant silencieusement l'inexploitable."""
    parsed = (parse_gamma_market(row) for row in rows if isinstance(row, dict))
    return [market for market in parsed if market is not None]
