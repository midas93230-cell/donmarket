"""Chemin d'ÉCRITURE sur les marchés de prédiction Binance.

DÉSARMÉ PAR DÉFAUT (`armed=False`), comme le moteur Polymarket. Le mode
désarmé parcourt exactement le MÊME chemin — plafonds compris, devis compris —
et s'arrête juste avant l'appel qui engage de l'argent. C'est ce qui permet de
vérifier un plan sans le jouer ; un chemin de répétition distinct du chemin
réel ne prouverait rien sur le chemin réel.

Le geste d'armer appartient à l'utilisateur, pas à ce fichier.

TROIS PARTICULARITÉS BINANCE, toutes documentées, aucune devinée :

1. **PASSER UN ORDRE SE FAIT EN DEUX TEMPS.** `POST /trade/get-quote` rend un
   `quoteId` que `POST /trade/place-order-bundle` exige. Ce n'est pas une
   commodité : sans devis valide, il n'y a pas d'ordre. Un devis a une durée de
   vie que la doc ne chiffre pas — on ne le met donc pas en cache, et on ne
   rejoue jamais un devis après une erreur réseau, parce qu'un devis rejoué
   pourrait être un second ordre.

2. **L'AUTORISATION SAS EST EXIGÉE** sur ordre, annulation, transfert et
   rachat (`-31003` sinon). Elle s'active dans l'application Binance : aucune
   ligne de code ne la remplace. On la présente comme un prérequis, pas comme
   une panne.

3. **L'ANNULATION PORTE LE PIÈGE DES CROCHETS.** `cancelInfoList[0].orderId`
   est signé sur les octets bruts ; toute bibliothèque qui percent-encode `[`
   casse la signature (`-1022`). Voir `signing.py` : c'est traité une fois,
   pour tous les appels.

Ce que ce module NE fait pas, et l'omission est délibérée : il ne choisit
aucun marché et ne calcule aucune taille. Il exécute un plan qu'on lui donne.
Un exécutant qui choisit devient une stratégie, et une stratégie cachée dans
un exécutant n'est mesurable nulle part.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from ..config import BINANCE_MARKET_ORDER_MIN_USDT, BINANCE_COLLATERAL_DECIMALS
from .api import DEFAULT_VENDOR

# Au-delà, c'est forcément une conversion appliquée deux fois : personne ne
# passe un ordre d'un milliard de dollars sur un marché de prédiction, et une
# double conversion est l'erreur naturelle une fois le piège d'unité connu.
MAX_PLAUSIBLE_USDT = 1_000_000_000.0


def to_base_units(amount_usdt: float) -> str:
    """Convertit un montant en USDT vers l'unité que l'API attend réellement.

    MESURÉ le 2026-08-18 : `amountIn` est en UNITÉS DE BASE à 18 décimales.
    Envoyer `8.0` demande huit wei, et le serveur répond `-9000 order amount is
    too small` — message qui accuse le solde alors que la faute est à l'unité.
    Le diagnostic n'a été possible que par recoupement : le compte avait
    8,73 USDT et des ordres déjà passés à 1 et 5 USDT, donc un minimum
    supérieur à 8 était impossible.

    Le passage par `Decimal` n'est pas une coquetterie : `int(0.07 * 10**18)`
    rend 69999999999999992, parce que 0,07 n'est pas représentable en binaire.

    Le plafond de vraisemblance garde l'erreur SYMÉTRIQUE, la seule vraiment
    coûteuse : convertir deux fois demanderait 10^18 fois trop.
    """
    if amount_usdt <= 0:
        raise ValueError(f"montant {amount_usdt} : un ordre se passe en positif")
    if amount_usdt > MAX_PLAUSIBLE_USDT:
        raise ValueError(
            f"montant {amount_usdt} USDT invraisemblable — c'est la signature "
            "d'une conversion en unités de base appliquée deux fois"
        )
    quantum = Decimal(10) ** BINANCE_COLLATERAL_DECIMALS
    return str(int((Decimal(str(amount_usdt)) * quantum).to_integral_value()))
from ..execute.limits import ExecutionLimits, gate
from .api import BinancePredictionClient
from .model import BinanceApiError, BinanceSchemaError

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"
LIMIT = "LIMIT"
MARKET = "MARKET"


@dataclass(frozen=True)
class PredictionOrder:
    """Un ordre à passer.

    Porte `market_id`, `price` et `size` : c'est exactement ce que
    `execute/limits.gate()` inspecte, donc le portier écrit pour Polymarket
    s'applique ici sans être dupliqué.
    """

    market_id: int
    token_id: str
    side: str = BUY
    order_type: str = LIMIT
    price: float = 0.0
    size: float = 0.0

    def __post_init__(self) -> None:
        if self.side not in (BUY, SELL):
            raise ValueError(f"side {self.side!r} : attendu {BUY} ou {SELL}")
        if self.order_type not in (LIMIT, MARKET):
            raise ValueError(
                f"order_type {self.order_type!r} : attendu {LIMIT} ou {MARKET}"
            )
        if self.size <= 0:
            raise ValueError("size doit être strictement positive")
        if self.order_type == LIMIT and not 0.0 < self.price < 1.0:
            raise ValueError(
                f"price {self.price} hors de (0, 1) — un prix de marché de "
                "prédiction est une probabilité"
            )

    @property
    def notional_usdt(self) -> float:
        return self.price * self.size

    def market_order_too_small(self) -> bool:
        """Change-log du 2026-06-16 : un MARKET exige `amountIn` ≳ 1,5 USDT.

        Le seuil « varies by market depth » : on le traite comme un signal, pas
        comme une frontière exacte. Les LIMIT n'y sont pas soumis.
        """
        return (
            self.order_type == MARKET
            and self.notional_usdt < BINANCE_MARKET_ORDER_MIN_USDT
        )


@dataclass(frozen=True)
class Quote:
    """Le devis rendu par `get-quote`. `quote_id` est la seule pièce obligatoire."""

    quote_id: str
    price: float | None = None
    size: float | None = None
    fee_usdt: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


def parse_quote(payload: Any) -> Quote:
    """Extrait le devis, ou lève.

    Un devis sans `quoteId` n'est pas un devis dégradé : c'est une réponse
    qu'on ne peut pas transformer en ordre. Rendre un `Quote` vide ferait
    échouer l'ordre plus loin, avec un message qui ne pointerait plus ici.
    """
    source: Any = payload
    if isinstance(payload, Mapping) and "quoteId" not in payload:
        inner = payload.get("data")
        if isinstance(inner, Mapping):
            source = inner
    if not isinstance(source, Mapping):
        raise BinanceSchemaError("get-quote : objet attendu")

    quote_id = source.get("quoteId") or source.get("quote_id") or source.get("id")
    if not isinstance(quote_id, str) or not quote_id.strip():
        raise BinanceSchemaError(
            f"get-quote : `quoteId` absent — clés reçues : {sorted(source)[:8]}"
        )

    def num(*names: str) -> float | None:
        for name in names:
            value = source.get(name)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    return Quote(
        quote_id=quote_id.strip(),
        price=num("price", "avgPrice", "executionPrice"),
        size=num("size", "quantity", "shares"),
        fee_usdt=num("fee", "feeAmount", "takerFee"),
        raw=dict(source),
    )


@dataclass(frozen=True)
class ExecutionOutcome:
    """Compte-rendu d'un passage, armé ou non.

    `armed` est conservé dans le résultat exprès : un rapport qui ne dit pas
    s'il décrit une répétition ou de vrais ordres est un rapport qu'on finit
    par mal lire.
    """

    armed: bool
    placed: tuple[Mapping[str, Any], ...] = ()
    quotes: tuple[Quote, ...] = ()
    refused: tuple[tuple[Any, str], ...] = ()
    failures: tuple[tuple[Any, str], ...] = ()

    @property
    def summary(self) -> str:
        head = (
            "ARMÉ — ordres réellement passés"
            if self.armed
            else "DÉSARMÉ — aucun ordre envoyé"
        )
        return (
            f"{head} : {len(self.placed)} passé(s), {len(self.quotes)} devis "
            f"obtenu(s), {len(self.refused)} refusé(s) par les plafonds, "
            f"{len(self.failures)} en échec"
        )


class PredictionTrader:
    """Exécute un plan d'ordres. Rien de plus."""

    def __init__(
        self,
        client: BinancePredictionClient,
        *,
        limits: ExecutionLimits,
        armed: bool = False,
    ) -> None:
        self.client = client
        self.limits = limits
        self.armed = armed

    async def get_quote(
        self,
        order: PredictionOrder,
        *,
        vendor: str | None = None,
        slippage_bps: int = 1000,
    ) -> Quote:
        """`POST /trade/get-quote`. Appelé même désarmé — c'est une lecture.

        Obtenir le devis en mode désarmé est délibéré : c'est la seule façon
        de savoir ce que l'ordre coûterait vraiment, frais compris, sans le
        passer. Le devis n'engage rien tant qu'il n'est pas présenté à
        `place-order-bundle`.
        """
        # TROIS paramètres découverts en escalier le 2026-08-18 — le serveur
        # n'en nomme qu'un par refus, donc leur absence se découvre une à une :
        # `walletAddress`, `vendor`, puis `amountIn`.
        #
        # `amountIn` est un MONTANT EN USDT, pas un nombre de parts. C'est le
        # même champ que le change-log Binance associe au minimum de ~1,5 USDT
        # sur les ordres MARKET — cohérence qui confirme l'unité.
        params: dict[str, Any] = {
            "walletAddress": await self.client.wallet_address(),  # type: ignore[attr-defined]
            "vendor": vendor or DEFAULT_VENDOR,
            "marketId": order.market_id,
            "tokenId": order.token_id,
            "side": order.side,
            "orderType": order.order_type,
            "amountIn": to_base_units(order.notional_usdt),
            "quantity": order.size,
            # Obligatoire aussi (mesuré) : sans lui, `-3026`. La valeur vient du
            # topic ; 1000 bps est ce que Binance publie par défaut.
            "slippageBps": slippage_bps,
        }
        if order.order_type == LIMIT:
            # `priceLimit`, PAS `price`. Relevé le 2026-08-19 dans le payload
            # du site web de Binance. `price` et `limitPrice` rendent tous deux
            # `-3026 Your input param is invalid` — un refus qui ressemble à un
            # refus de type d'ordre, et qui a fait conclure à tort que le LIMIT
            # n'existait pas sur cette place. C'était un nom de champ.
            params["priceLimit"] = f"{order.price:.2f}"
        payload = await self.client.post(  # type: ignore[attr-defined]
            "/trade/get-quote", params
        )
        return parse_quote(payload)

    async def place_limit_direct(
        self,
        order: PredictionOrder,
        *,
        vendor: str | None = None,
        slippage_bps: int = 1000,
    ) -> Mapping[str, Any]:
        """LIMIT posté SANS devis. Chemin non validé — lire avant d'armer.

        MESURÉ le 2026-08-19 : `/trade/get-quote` refuse `orderType: LIMIT`, y
        compris sans prix, donc ce n'est pas une question d'encodage mais de
        type d'ordre. Et la technique du 404 sur treize noms de routes montre
        qu'il n'existe aucune route LIMIT dédiée : seuls `get-quote` et
        `place-order-bundle` répondent.

        Reste que `place-order-bundle` exige `walletAddress` et pas seulement
        `quoteId` — il sait donc peut-être construire un ordre de lui-même.
        Cette méthode teste exactement cette hypothèse, et c'est le seul
        chemin qui puisse ouvrir la porte TENEUR : sans elle, on ne peut que
        payer 1,8 % en preneur au lieu d'encaisser 25 % du frais d'en face.

        CE QU'ON PERD par rapport au chemin normal, et il faut le savoir avant
        d'armer : le devis chiffrait le coût AVANT l'engagement. Ici il n'y a
        rien à lire avant. Le plafond de `limits` reste la seule protection,
        d'où le passage obligé par `gate()` en amont.

        UN SEUL ESSAI, comme toute écriture : une requête perdue à l'aller est
        indiscernable d'une réponse perdue au retour, et rejouer passerait
        l'ordre deux fois. En cas de doute, `active_orders()` tranche.
        """
        if order.order_type != LIMIT:
            raise ValueError(
                f"place_limit_direct refuse un ordre {order.order_type} : le "
                "MARKET a un devis qui chiffre son coût avant l'engagement, et "
                "s'en priver ne gagnerait rien"
            )
        if not self.armed:
            raise RuntimeError(
                "place_limit_direct() appelé sur un trader désarmé — c'est un "
                "défaut de programmation, pas une situation à rattraper"
            )

        params: dict[str, Any] = {
            "walletAddress": await self.client.wallet_address(),  # type: ignore[attr-defined]
            # DEUX champs distincts, mesuré le 2026-08-19 : l'ordre armé a
            # révélé que `place-order-bundle` accepte le LIMIT et réclamait
            # `walletId` en plus de l'adresse.
            "walletId": await self.client.wallet_id(),  # type: ignore[attr-defined]
            "vendor": vendor or DEFAULT_VENDOR,
            "marketId": order.market_id,
            "tokenId": order.token_id,
            "side": order.side,
            "orderType": LIMIT,
            "price": order.price,
            "amountIn": to_base_units(order.notional_usdt),
            "slippageBps": slippage_bps,
        }
        payload = await self.client.post(  # type: ignore[attr-defined]
            "/trade/place-order-bundle", params
        )
        return payload if isinstance(payload, Mapping) else {"raw": payload}

    async def place(
        self,
        order: PredictionOrder,
        quote: Quote,
        *,
        time_in_force: str = "GTC",
    ) -> Mapping[str, Any]:
        """`POST /trade/place-order-bundle`. LE point où l'argent bouge.

        Ne jamais réessayer : une erreur réseau après émission ne dit pas si
        l'ordre est passé, et un second envoi du même devis risque un doublon.
        En cas de doute, `active_orders()` tranche — c'est une lecture, elle
        est sans danger, contrairement à un réessai.
        """
        if not self.armed:
            raise RuntimeError(
                "place() appelé sur un trader désarmé — c'est un défaut de "
                "programmation, pas une situation à rattraper"
            )
        # Les TROIS champs sont exigés, découverts en escalier le 2026-08-19 :
        # `walletAddress`, puis `walletId`, puis `quoteId`. Le payload du site
        # web les porte tous les trois, ce qui a confirmé la liste.
        payload = await self.client.post(  # type: ignore[attr-defined]
            "/trade/place-order-bundle",
            {
                "walletAddress": await self.client.wallet_address(),  # type: ignore[attr-defined]
                "walletId": await self.client.wallet_id(),  # type: ignore[attr-defined]
                "quoteId": quote.quote_id,
                # QUATRIEME marche de l'escalier, decouverte au premier tour
                # arme du 2026-08-19 : sans `timeInForce`, -3026. GTC est le
                # seul choix coherent avec la strategie -- un ordre teneur doit
                # RESTER au carnet pour esperer etre rempli ; un IOC serait
                # annule aussitot et ne serait jamais teneur.
                "timeInForce": time_in_force,
            },
        )
        return payload if isinstance(payload, Mapping) else {"raw": payload}

    async def batch_cancel(self, order_ids: Sequence[str]) -> Mapping[str, Any]:
        """`POST /trade/batch-cancel` — les fameuses clés à crochets.

        Le champ `vendor` par élément a été RETIRÉ de la doc le 2026-06-16 :
        le serveur le remplit lui-même (`predict_fun`). L'envoyer quand même
        ajouterait un paramètre inattendu à la chaîne signée.
        """
        if not self.armed:
            raise RuntimeError("batch_cancel() appelé sur un trader désarmé")
        if not order_ids:
            return {}
        # MESURE du 2026-08-19 : sans `walletAddress` ET `walletId`, le
        # serveur rend -1102 « Mandatory parameter was not sent ». L'annulation
        # echouait donc a chaque tour, et un ordre non annulable est un ordre
        # qu'on laisse au carnet sans le vouloir.
        params: dict[str, Any] = {
            "walletAddress": await self.client.wallet_address(),  # type: ignore[attr-defined]
            "walletId": await self.client.wallet_id(),  # type: ignore[attr-defined]
        }
        for index, order_id in enumerate(order_ids):
            params[f"cancelInfoList[{index}].orderId"] = order_id
        payload = await self.client.post(  # type: ignore[attr-defined]
            "/trade/batch-cancel", params
        )
        return payload if isinstance(payload, Mapping) else {"raw": payload}

    async def run(self, orders: Sequence[PredictionOrder]) -> ExecutionOutcome:
        """Chemin complet : plafonds, devis, puis ordre si et seulement si armé."""
        decision = gate(orders, limits=self.limits)
        allowed = tuple(decision.allowed)
        refused = tuple(decision.refused)

        # Le minimum de MARKET est vérifié APRÈS les plafonds : un ordre déjà
        # refusé n'a pas besoin d'un second motif, et empiler les motifs rend
        # le rapport illisible.
        quotes: list[Quote] = []
        placed: list[Mapping[str, Any]] = []
        failures: list[tuple[Any, str]] = []

        for order in allowed:
            if order.market_order_too_small():
                failures.append(
                    (
                        order,
                        f"ordre MARKET de {order.notional_usdt:.2f} USDT sous le "
                        f"minimum documenté de ~{BINANCE_MARKET_ORDER_MIN_USDT} USDT",
                    )
                )
                continue
            try:
                quote = await self.get_quote(order)
            except (BinanceApiError, BinanceSchemaError) as exc:
                failures.append((order, f"devis refusé : {exc}"))
                continue
            quotes.append(quote)

            if not self.armed:
                continue
            try:
                placed.append(await self.place(order, quote))
            except (BinanceApiError, BinanceSchemaError) as exc:
                failures.append((order, f"ordre refusé : {exc}"))

        return ExecutionOutcome(
            armed=self.armed,
            placed=tuple(placed),
            quotes=tuple(quotes),
            refused=refused,
            failures=tuple(failures),
        )
