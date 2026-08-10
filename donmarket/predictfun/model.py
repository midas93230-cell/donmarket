"""Modèle de données Predict.fun, mesuré et non supposé.

Module PUR : aucun accès réseau, aucun accès disque.

Tout ce qui suit vient d'une mesure du 2026-08-09 sur `api-testnet.predict.fun`,
pas d'une transposition de Polymarket. Les écarts sont documentés à l'endroit
exact où ils mordent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

# Statut de négociabilité renvoyé par l'API. Mesuré : le paramètre de requête
# `?tradingStatus=` est IGNORÉ par le serveur — il faut filtrer ici (voir api.py).
TRADING_OPEN = "OPEN"

# Mesuré : `decimalPrecision` vaut 2 ou 3 selon le marché. C'est le nombre de
# décimales du prix, donc le pas de cotation est 10^-precision.
DEFAULT_DECIMAL_PRECISION = 2

# Un jeu complet (Yes + No) vaut 1 USDT à la résolution, comme sur Polymarket.
FULL_SET_USD = 1.0


class PredictSchemaError(ValueError):
    """La réponse de l'API n'a pas la forme mesurée.

    Cette exception existe pour une raison précise. Le parseur de carnet de
    Polymarket (`api/clob.py`) ignore silencieusement tout palier qui n'est pas
    un dictionnaire. Appliqué à un carnet Predict.fun — dont les paliers sont
    des paires `[prix, taille]` — il renverrait un carnet VIDE sans une ligne
    d'erreur, et tout l'aval conclurait « aucune liquidité » au lieu de « je ne
    sais pas lire ». On préfère échouer bruyamment.
    """


def _as_float(value: Any, *, where: str) -> float:
    """Convertit en flottant ou lève. Pas de valeur par défaut silencieuse."""
    if isinstance(value, bool) or value is None:
        raise PredictSchemaError(f"{where} : nombre attendu, reçu {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise PredictSchemaError(f"{where} : nombre attendu, reçu {value!r}") from exc
    raise PredictSchemaError(f"{where} : nombre attendu, reçu {type(value).__name__}")


def parse_iso(value: Any) -> datetime | None:
    """Lit un horodatage ISO 8601 de l'API (« 2025-12-31T08:54:08.117Z ») en UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PredictLevel:
    """Un palier du carnet : un prix unitaire et une taille en PARTS."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        """Valeur en USDT immobilisée par ce palier."""
        return self.price * self.size


def _level_price(level: PredictLevel) -> float:
    return level.price


@dataclass(frozen=True)
class PredictOutcome:
    """Une branche du marché (« Yes »/« No », « Up »/« Down »)."""

    name: str
    index_set: int
    on_chain_id: str
    best_bid: PredictLevel | None
    best_ask: PredictLevel | None
    status: str | None


@dataclass(frozen=True)
class PredictBook:
    """Le carnet d'un marché, exprimé du point de vue de la branche « Yes ».

    DIFFÉRENCE STRUCTURANTE AVEC POLYMARKET, mesurée et confirmée par la doc
    (« YES asks are equivalent to NO bids, and YES bids are equivalent to NO
    asks ») : il n'y a **qu'un carnet par marché**. Le côté « No » n'est pas un
    second carnet indépendant, c'est le miroir exact du premier — vérifié sur
    12 marchés, 0 violation : `no_ask = 1 − yes_bid` au flottant près, avec la
    MÊME taille.

    Conséquence économique, et elle est décisive : l'arbitrage du jeu complet
    est **impossible par construction**, pas seulement rare. Acheter les deux
    branches au marché coûte

        ask_yes + ask_no = ask_yes + (1 − bid_yes) = 1 + écart ≥ 1

    Sur Polymarket, l'impossibilité était une observation empirique (0 cas sur
    1 937 marchés) qui aurait pu changer ; ici c'est une identité algébrique.
    NE PAS porter la chasse à l'arbitrage sur ce marché : elle n'a pas de zéro.
    """

    market_id: int
    yes_bids: tuple[PredictLevel, ...]
    yes_asks: tuple[PredictLevel, ...]
    updated_ms: int | None = None

    def __post_init__(self) -> None:
        """Impose l'ordre meilleur-en-premier, quelle que soit la provenance.

        L'API livre DÉJÀ les bids décroissants et les asks croissants, donc
        meilleur en premier — l'inverse de Polymarket. On trie quand même :
        se fier à l'ordre de l'émetteur est exactement l'hypothèse qui a coûté
        cher côté Polymarket, et un carnet reconstruit à la main (test, cache
        relu) n'offre aucune garantie d'ordre.
        """
        object.__setattr__(
            self, "yes_bids", tuple(sorted(self.yes_bids, key=_level_price, reverse=True))
        )
        object.__setattr__(self, "yes_asks", tuple(sorted(self.yes_asks, key=_level_price)))

    # --- côté Yes, lu directement ------------------------------------------

    @property
    def best_yes_bid(self) -> PredictLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> PredictLevel | None:
        return self.yes_asks[0] if self.yes_asks else None

    # --- côté No, DÉRIVÉ (il n'existe pas de carnet No) ---------------------

    @property
    def no_bids(self) -> tuple[PredictLevel, ...]:
        """Bids No = asks Yes retournés : acheter No à 1−p ≡ vendre Yes à p."""
        return tuple(PredictLevel(1.0 - lv.price, lv.size) for lv in self.yes_asks)

    @property
    def no_asks(self) -> tuple[PredictLevel, ...]:
        return tuple(PredictLevel(1.0 - lv.price, lv.size) for lv in self.yes_bids)

    @property
    def best_no_bid(self) -> PredictLevel | None:
        return self.no_bids[0] if self.no_bids else None

    @property
    def best_no_ask(self) -> PredictLevel | None:
        return self.no_asks[0] if self.no_asks else None

    # --- grandeurs dérivées -------------------------------------------------

    @property
    def spread(self) -> float | None:
        """Écart Yes en points de prix. Se mesure sur UNE branche, jamais entre branches."""
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return self.best_yes_ask.price - self.best_yes_bid.price

    @property
    def midpoint(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid.price + self.best_yes_ask.price) / 2.0

    @property
    def full_set_ask_sum(self) -> float | None:
        """Coût d'achat des deux branches au marché. Vaut `1 + écart`, donc ≥ 1.

        Exposé pour que ce soit VÉRIFIABLE plutôt que promis : un test l'affirme
        et un scan peut le recontrôler en direct si Predict.fun changeait un jour
        de moteur d'appariement.
        """
        if self.best_yes_ask is None or self.best_no_ask is None:
            return None
        return self.best_yes_ask.price + self.best_no_ask.price

    def depth_usd(self, side: str, levels: int = 3) -> float:
        """USDT posés sur les `levels` meilleurs paliers du côté Yes demandé."""
        book_side = self.yes_bids if side == "bid" else self.yes_asks
        return sum(level.notional for level in book_side[:levels])

    def is_empty(self) -> bool:
        return not self.yes_bids and not self.yes_asks


@dataclass(frozen=True)
class PredictMarket:
    """Un marché Predict.fun normalisé. Immuable."""

    market_id: int
    condition_id: str
    question: str
    title: str
    category_slug: str
    trading_status: str
    status: str
    fee_rate_bps: int
    decimal_precision: int
    # Mesuré uniforme à 100 parts sur les marchés ouverts du testnet. C'est la
    # taille minimale que la plateforme considère comme une vraie cote ; la doc
    # des points l'appelle « minimum order size » du marché.
    share_threshold: float | None
    # Mesuré uniforme à 0,06 (FRACTION, pas un pourcentage — Polymarket, lui,
    # exprime `rewardsMaxSpread` en pourcent et impose de diviser par 100).
    spread_threshold: float | None
    market_variant: str | None
    is_neg_risk: bool
    is_boosted: bool
    # Publié par Predict.fun lui-même : le ou les marchés Polymarket équivalents.
    # Seul lien structurel entre les deux carnets, utile pour comparer un prix.
    polymarket_condition_ids: tuple[str, ...]
    outcomes: tuple[PredictOutcome, ...]
    created_at: datetime | None
    # `rewards` existe dans le schéma mais est resté VIDE sur tout l'échantillon
    # testnet accessible. On le conserve brut : le jour où il se remplit, on veut
    # le voir tel quel plutôt qu'à travers une structure devinée aujourd'hui.
    rewards_raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_open(self) -> bool:
        return self.trading_status == TRADING_OPEN

    @property
    def tick_size(self) -> float:
        """Pas de cotation, déduit de `decimalPrecision` (2 → 0,01 ; 3 → 0,001)."""
        return 10.0 ** (-self.decimal_precision)

    @property
    def fee_rate(self) -> float:
        """Taux de frais de base en fraction (200 bps → 0,02)."""
        return self.fee_rate_bps / 10_000.0

    def outcome_named(self, *names: str) -> PredictOutcome | None:
        wanted = {n.casefold() for n in names}
        for outcome in self.outcomes:
            if outcome.name.casefold() in wanted:
                return outcome
        return None

    @property
    def yes_outcome(self) -> PredictOutcome | None:
        """La branche « positive ». Mesuré : « Yes » ou « Up » selon le marché.

        On se rabat sur `indexSet == 1`, convention observée sur tous les marchés
        binaires de l'échantillon, plutôt que sur la position dans la liste — un
        ordre de liste n'est garanti nulle part.
        """
        named = self.outcome_named("yes", "up")
        if named is not None:
            return named
        for outcome in self.outcomes:
            if outcome.index_set == 1:
                return outcome
        return None


def _parse_level_pair(row: Any, *, where: str) -> PredictLevel:
    """Lit un palier `[prix, taille]`.

    MESURÉ : Predict.fun renvoie des paires, pas des objets. On accepte aussi la
    forme objet `{"price": …, "size": …}` — mais uniquement parce que l'API
    l'utilise DÉJÀ ailleurs, pour `bestBid`/`bestAsk` sur le marché. Toute autre
    forme lève : voir `PredictSchemaError`.
    """
    if isinstance(row, (list, tuple)):
        if len(row) < 2:
            raise PredictSchemaError(f"{where} : paire [prix, taille] attendue, reçu {row!r}")
        return PredictLevel(
            price=_as_float(row[0], where=f"{where}.prix"),
            size=_as_float(row[1], where=f"{where}.taille"),
        )
    if isinstance(row, dict):
        return PredictLevel(
            price=_as_float(row.get("price"), where=f"{where}.price"),
            size=_as_float(row.get("size"), where=f"{where}.size"),
        )
    raise PredictSchemaError(
        f"{where} : palier illisible de type {type(row).__name__} — "
        "le schéma du carnet a changé, ne pas deviner"
    )


def _parse_levels(rows: Any, *, where: str) -> tuple[PredictLevel, ...]:
    if rows is None:
        return ()
    if not isinstance(rows, (list, tuple)):
        raise PredictSchemaError(
            f"{where} : liste de paliers attendue, reçu {type(rows).__name__}"
        )
    levels = (_parse_level_pair(row, where=f"{where}[{i}]") for i, row in enumerate(rows))
    # Une taille nulle n'est pas un palier : c'est une suppression.
    return tuple(level for level in levels if level.size > 0)


def parse_book(payload: dict[str, Any]) -> PredictBook:
    """Normalise `GET /v1/markets/{id}/orderbook`.

    Forme mesurée :
        {"success": true,
         "data": {"marketId": 1049,
                  "bids": [[0.88, 688183.2794], [0.09, 653410.53]],
                  "asks": [[0.94, 35.2185], [0.99, 617.18]],
                  "lastOrderSettled": {…},
                  "updateTimestampMs": 1786240863644}}
    """
    if not isinstance(payload, dict):
        raise PredictSchemaError(f"carnet : objet attendu, reçu {type(payload).__name__}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    market_id = data.get("marketId")
    if not isinstance(market_id, int):
        raise PredictSchemaError(f"carnet : marketId entier attendu, reçu {market_id!r}")

    updated = data.get("updateTimestampMs")
    return PredictBook(
        market_id=market_id,
        yes_bids=_parse_levels(data.get("bids"), where=f"carnet[{market_id}].bids"),
        yes_asks=_parse_levels(data.get("asks"), where=f"carnet[{market_id}].asks"),
        updated_ms=updated if isinstance(updated, int) else None,
    )


def _parse_best(row: Any, *, where: str) -> PredictLevel | None:
    if row is None:
        return None
    if not isinstance(row, dict):
        raise PredictSchemaError(f"{where} : objet {{price, size}} ou null attendu")
    return PredictLevel(
        price=_as_float(row.get("price"), where=f"{where}.price"),
        size=_as_float(row.get("size"), where=f"{where}.size"),
    )


def _parse_outcome(row: Any, *, where: str) -> PredictOutcome:
    if not isinstance(row, dict):
        raise PredictSchemaError(f"{where} : objet attendu, reçu {type(row).__name__}")
    index_set = row.get("indexSet")
    return PredictOutcome(
        name=str(row.get("name") or ""),
        index_set=index_set if isinstance(index_set, int) else 0,
        on_chain_id=str(row.get("onChainId") or ""),
        best_bid=_parse_best(row.get("bestBid"), where=f"{where}.bestBid"),
        best_ask=_parse_best(row.get("bestAsk"), where=f"{where}.bestAsk"),
        status=row.get("status") if isinstance(row.get("status"), str) else None,
    )


def parse_market(raw: dict[str, Any]) -> PredictMarket:
    """Normalise une ligne de `GET /v1/markets`. Lève si l'identité manque."""
    if not isinstance(raw, dict):
        raise PredictSchemaError(f"marché : objet attendu, reçu {type(raw).__name__}")
    market_id = raw.get("id")
    if not isinstance(market_id, int):
        raise PredictSchemaError(f"marché : id entier attendu, reçu {market_id!r}")

    precision = raw.get("decimalPrecision")
    fee_bps = raw.get("feeRateBps")
    share_threshold = raw.get("shareThreshold")
    spread_threshold = raw.get("spreadThreshold")
    poly_ids = raw.get("polymarketConditionIds")
    rewards = raw.get("rewards")

    return PredictMarket(
        market_id=market_id,
        condition_id=str(raw.get("conditionId") or ""),
        question=str(raw.get("question") or ""),
        title=str(raw.get("title") or "").strip(),
        category_slug=str(raw.get("categorySlug") or ""),
        trading_status=str(raw.get("tradingStatus") or ""),
        status=str(raw.get("status") or ""),
        fee_rate_bps=fee_bps if isinstance(fee_bps, int) else 0,
        decimal_precision=(
            precision if isinstance(precision, int) else DEFAULT_DECIMAL_PRECISION
        ),
        share_threshold=(
            float(share_threshold) if isinstance(share_threshold, (int, float)) else None
        ),
        spread_threshold=(
            float(spread_threshold) if isinstance(spread_threshold, (int, float)) else None
        ),
        market_variant=(
            str(raw.get("marketVariant")) if raw.get("marketVariant") is not None else None
        ),
        is_neg_risk=bool(raw.get("isNegRisk")),
        is_boosted=bool(raw.get("isBoosted")),
        polymarket_condition_ids=(
            tuple(str(x) for x in poly_ids) if isinstance(poly_ids, list) else ()
        ),
        outcomes=tuple(
            _parse_outcome(row, where=f"marché[{market_id}].outcomes[{i}]")
            for i, row in enumerate(raw.get("outcomes") or [])
        ),
        created_at=parse_iso(raw.get("createdAt")),
        rewards_raw=rewards if isinstance(rewards, dict) else {},
        raw=raw,
    )


def parse_markets(rows: Sequence[Any]) -> list[PredictMarket]:
    """Normalise une page.

    Contrairement au parseur Gamma, une ligne illisible n'est PAS ignorée en
    silence : sur un univers de 20 marchés (plafond mesuré du testnet), perdre
    une ligne sans le dire fausse tout décompte.
    """
    return [parse_market(row) for row in rows]
