"""Tenue de marché sur Polymarket — capturer l'écart, pas les récompenses.

## Pourquoi un module de plus

`execute/orders.py` planifie pour les RÉCOMPENSES de liquidité : il cote les
deux branches au bid pour marquer des points dans le barème de Polymarket, et
son ticket d'entrée est `rewardsMinSize` — une centaine de parts, donc une
centaine de dollars. C'est ce chiffre qui avait fait écarter Polymarket le
2026-08-18 avec un « 0 marché finançable à 8 $ » parfaitement exact, et
parfaitement hors sujet.

Capturer l'ÉCART est une autre stratégie : acheter au meilleur bid, revendre au
meilleur ask, et encaisser la différence. Son ticket est `orderMinSize`, soit
cinq parts — entre 0,50 $ et 2 $ selon le prix. MESURÉ le 2026-08-20 sur
2 100 marchés : **351 branches cotables à 8,73 $**, gain brut médian d'un
aller-retour **4,5 %**. Là où la même logique n'en trouvait qu'UNE sur Binance.

## Le piège que ces filtres existent pour éviter

Sans eux, la même mesure rendait 1 608 branches, menées par des écarts à
28 000 % : bid 0,002 contre ask 0,565. Ce n'est pas une aubaine, c'est un
carnet VIDE — personne n'y servira jamais un ordre. C'est la leçon du
2026-07-28, où les +2 % lus dans les carnets Polymarket ne se sont jamais
retrouvés dans les exécutions.

D'où trois refus explicites, et chacun a son motif rendu à l'appelant :
un écart plafonné, un prix hors des extrêmes, et de la profondeur DES DEUX
CÔTÉS — sans contrepartie en face on ne peut ni être rempli à l'achat, ni
ressortir à la vente.

## Ce que ce module ne prétend pas savoir

Le taux de remplissage. « Brut » veut dire les deux côtés remplis, ce que rien
ne garantit : être servi à l'achat signifie que quelqu'un a voulu vendre à
notre prix, et sur un marché de prédiction ce quelqu'un en sait parfois plus
que nous. Aucun rendement n'est annoncé ici.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

DEFAULT_TICK = 0.01

# Sous deux pas, l'aller-retour rapporte moins qu'un pas de cotation : le
# moindre décalage du carnet transforme le gain en perte.
MIN_SPREAD_TICKS = 2

# Au-delà, ce n'est plus un écart mais un carnet béant. Voir le piège ci-dessus.
MAX_SPREAD_TICKS = 10

# Hors de cette bande, un pas de cotation pèse trop lourd par rapport au prix,
# et la branche est de toute façon presque résolue.
MIN_PRICE = 0.10
MAX_PRICE = 0.90

# Parts présentes de chaque côté pour qu'on parle de contrepartie.
MIN_DEPTH_SHARES = 20.0

# Volume échangé sur 24 h, en dollars. MESURÉ le 2026-08-21 : un ordre posé au
# meilleur bid sur « Somaliland join the Abraham Accords » a passé QUATORZE
# HEURES au carnet sans le moindre remplissage. Le carnet n'était pas vide — il
# avait de la profondeur des deux côtés et un écart de 8 pas — il était LENT.
#
# C'est le piège suivant celui du carnet béant, et il est plus sournois : un
# écart large signale souvent qu'il ne se passe rien, puisque personne ne vient
# le resserrer. Un écart de 2 pas rempli dix fois par jour vaut mieux qu'un
# écart de 8 pas jamais servi.
#
# Le seuil est bas volontairement : à ce capital on ne cherche pas les marchés
# les plus actifs, seulement à éliminer ceux où il ne se passe RIEN.
MIN_VOLUME_24H_USD = 500.0


@dataclass(frozen=True)
class Rung:
    """Une branche cotable : ce qu'on veut acheter, et à quoi le revendre."""

    condition_id: str
    token_id: str
    question: str
    buy_price: float
    sell_price: float
    ticket_usd: float
    spread_ticks: int

    @property
    def gross_edge(self) -> float:
        """Gain brut d'un aller-retour, en fraction du capital engagé.

        BRUT : il suppose les DEUX côtés remplis. C'est l'inconnue centrale de
        toute la stratégie, et ce module ne la mesure pas.
        """
        if self.buy_price <= 0:
            return 0.0
        return (self.sell_price - self.buy_price) / self.buy_price


@dataclass
class Inventory:
    """Les parts détenues, par jeton."""

    shares: dict[str, float] = field(default_factory=dict)

    def add(self, token_id: str, shares: float) -> None:
        self.shares[token_id] = self.shares.get(token_id, 0.0) + shares

    def held(self, token_id: str) -> float:
        return self.shares.get(token_id, 0.0)


def _depth(levels: Sequence[object]) -> float:
    """Taille au MEILLEUR prix.

    Les carnets Polymarket arrivent PIRE PRIX EN PREMIER (mesuré le
    2026-07-26) : le meilleur est en dernière position. Lire `levels[0]`
    donnerait la profondeur du pire palier et laisserait passer des carnets
    sans contrepartie réelle.
    """
    if not levels:
        return 0.0
    return float(getattr(levels[-1], "size", 0.0) or 0.0)


def eligible(
    markets: Sequence[object],
    books: Mapping[str, object],
    *,
    capital_usd: float,
) -> tuple[list[Rung], list[tuple[str, str]]]:
    """Les branches cotables, et le motif de chaque écartée.

    Les motifs sont RENDUS et non journalisés : « aucune branche cotable » sans
    eux envoie chercher une panne là où c'est un filtre qui a tout pris.
    """
    retenues: list[Rung] = []
    rejets: list[tuple[str, str]] = []

    for market in markets:
        condition_id = str(getattr(market, "condition_id", "") or "")
        question = str(getattr(market, "question", "") or "")
        tick = float(getattr(market, "min_tick_size", 0.0) or DEFAULT_TICK)
        min_size = max(float(getattr(market, "min_order_size", 0.0) or 0.0), 1.0)

        # Un marché sans activité récente ne servira personne, quel que soit
        # son écart. Filtré AVANT les carnets : inutile de lire deux carnets
        # pour un marché où rien ne se passe.
        volume_24h = float(getattr(market, "volume_24h", 0.0) or 0.0)
        if volume_24h < MIN_VOLUME_24H_USD:
            rejets.append(
                (condition_id, f"{volume_24h:.0f} $ sur 24 h — marché endormi")
            )
            continue

        for token_id in getattr(market, "token_ids", ()):
            book = books.get(token_id)
            bid = getattr(book, "best_bid", None) if book is not None else None
            ask = getattr(book, "best_ask", None) if book is not None else None
            if bid is None or ask is None:
                rejets.append((condition_id, "carnet incomplet"))
                continue
            bid, ask = float(bid), float(ask)

            spread_ticks = round((ask - bid) / tick) if tick else 0
            if spread_ticks < MIN_SPREAD_TICKS:
                rejets.append(
                    (condition_id, f"écart {spread_ticks} pas — rien à capturer")
                )
                continue
            if spread_ticks > MAX_SPREAD_TICKS:
                rejets.append(
                    (
                        condition_id,
                        f"écart {spread_ticks} pas — carnet béant, personne ne cote",
                    )
                )
                continue
            if not (MIN_PRICE <= bid <= MAX_PRICE):
                rejets.append((condition_id, f"prix {bid:.3f} hors bande utile"))
                continue

            profondeur = min(
                _depth(getattr(book, "bids", ())), _depth(getattr(book, "asks", ()))
            )
            if profondeur < MIN_DEPTH_SHARES:
                rejets.append(
                    (
                        condition_id,
                        f"{profondeur:.0f} parts au mieux — pas de contrepartie",
                    )
                )
                continue

            ticket = min_size * bid
            if ticket > capital_usd:
                rejets.append(
                    (condition_id, f"ticket {ticket:.2f} $ au-dessus du capital")
                )
                continue

            retenues.append(
                Rung(
                    condition_id=condition_id,
                    token_id=token_id,
                    question=question,
                    # On REJOINT la file au meilleur prix. L'améliorer d'un pas
                    # resserrerait l'écart qu'on cherche justement à encaisser.
                    buy_price=round(bid, 4),
                    sell_price=round(ask, 4),
                    ticket_usd=ticket,
                    spread_ticks=spread_ticks,
                )
            )

    retenues.sort(key=lambda r: r.gross_edge, reverse=True)
    return retenues, rejets


@dataclass(frozen=True)
class DesiredOrder:
    """Un ordre voulu, indépendant du client qui l'enverra."""

    condition_id: str
    token_id: str
    side: str
    price: float
    size: float

    @property
    def cost_usd(self) -> float:
        return self.price * self.size


def plan(
    rungs: Sequence[Rung],
    inventory: Inventory,
    *,
    notional_per_market: float,
    max_markets: int,
    min_order_size: float = 5.0,
) -> list[DesiredOrder]:
    """Les ordres voulus, à partir de l'état. Fonction PURE : aucun réseau.

    Une branche sans inventaire se fait ACHETER au bid ; une branche avec
    inventaire se fait REVENDRE à l'ask. Jamais les deux à la fois sur la même
    branche — ce serait se croiser soi-même, et le carnet nous apparierait
    contre nous : on paierait l'écart au lieu de l'encaisser. C'est le seul cas
    où la stratégie perd de façon garantie.
    """
    ordres: list[DesiredOrder] = []
    for rung in rungs[:max_markets]:
        detenu = inventory.held(rung.token_id)
        if detenu > 0:
            if detenu < min_order_size:
                # Un reliquat sous le minimum n'est pas revendable tel quel :
                # le dire vaut mieux que d'émettre un ordre qui sera refusé.
                continue
            ordres.append(
                DesiredOrder(
                    condition_id=rung.condition_id,
                    token_id=rung.token_id,
                    side="SELL",
                    price=rung.sell_price,
                    size=detenu,
                )
            )
            continue

        parts = notional_per_market / rung.buy_price if rung.buy_price > 0 else 0.0
        parts = float(int(parts))  # jamais de fraction de part
        if parts < min_order_size:
            continue
        ordres.append(
            DesiredOrder(
                condition_id=rung.condition_id,
                token_id=rung.token_id,
                side="BUY",
                price=rung.buy_price,
                size=parts,
            )
        )
    return ordres
