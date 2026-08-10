"""Récompenses de liquidité — la seule stratégie qui ait survécu aux mesures.

Trois pistes ont été fermées par des chiffres (voir README) : l'arbitrage de jeu
complet côté preneur (0 / 1 937 marchés), le même côté teneur (+0,57 % médian,
perdant là où il y a du volume), et le rendement brut des récompenses.

Ce dernier point mérite d'être explicite, parce qu'il est contre-intuitif et
qu'il a failli coûter cher. Les récompenses paient pour POSTER des ordres, pas
pour être rempli : ni la sélection adverse ni le taux de remplissage ne les
atteignent. On en conclut trop vite que c'est de l'argent gratuit.

Mesure du 2026-07-28 : 2 099 marchés lus, 623 récompensés, 552 à plus de 24 h
de l'échéance, 453 finançables à 100 $, 441 aux deux carnets lisibles. Sur les
60 meilleurs AU RENDEMENT BRUT, avec 24 h d'historique à la minute :

    rendement brut médian            : +0,75 % / jour
    dérive médiane sur 24 h          :  3,00 % du capital engagé
    NET médian                       : -1,60 % / jour
    marchés où la récompense couvre  : 16 / 60

Autrement dit : sélectionner au rendement brut donne une médiane PERDANTE. Ce
module classe donc par NET = rendement − dérive. Beaucoup moins vendeur, et
seule version qui ne perd pas.

La queue haute, elle, existe et se voit : le meilleur candidat de la mesure
paie 156 $/jour face à 236 $ de liquidité concurrente, soit +49,75 %/jour NET
sur un ticket de 50 $. C'est la thèse du module — chasser les pools
SOUS-PEUPLÉS, pas les gros pools — et c'est aussi pourquoi le tri se fait en
boucle sur tout l'univers plutôt que sur une liste figée.

AVERTISSEMENT D'UNITÉ (2026-07-31) : tous les rendements bruts cités ci-dessus
ont été mesurés avec le modèle LINÉAIRE de concurrence (somme des dollars
postés dans la bande), remplacé depuis par la formule publiée de Polymarket
(`analysis/scoring`). Les deux ne diffèrent pas d'un facteur constant : la
pondération quadratique efface les ordres postés loin du milieu, ce qui monte
le brut sur les carnets étalés, et le passage des dollars aux parts le change
dans un sens qui dépend du niveau de prix. Ces chiffres restent valables comme
ORDRE DE GRANDEUR et comme conclusion (sélectionner au brut donne une médiane
perdante — la dérive, elle, n'a pas changé de calcul), mais le brut lui-même
est à re-mesurer avant d'être cité comme le rendement du modèle actuel.

Biais à garder en tête : ces médianes portent sur la tête de classement au
brut, pas sur l'univers entier. C'est délibéré (l'historique coûte un appel par
jeton, non groupable), mais cela veut dire qu'elles décrivent les marchés
qu'on serait tenté de prendre, pas le marché moyen.

Distinction qui porte tout le module :

- l'OSCILLATION (aller-retour du prix) est FAVORABLE au teneur : il achète au
  bid et revend à l'ask, c'est la capture de fourchette.
- la DÉRIVE (mouvement net d'un bout à l'autre) est DÉFAVORABLE : on est rempli
  d'un seul côté et on porte la position pendant qu'elle empire.

Limite assumée : la dérive telle qu'elle est mesurée ici est un MAJORANT. Elle
suppose d'être rempli à fond du mauvais côté et de porter 24 h. Un teneur coté
des deux côtés n'est que partiellement exposé. Le net réel est donc entre le
chiffre calculé et zéro — mais l'ordre de grandeur suffit à trancher.

Ce module est PUR : aucun appel réseau, aucune écriture. L'appelant fournit les
marchés, les carnets et les séries de prix ; la logique de décision reste
testable sans dépendre de l'état du marché.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping, Sequence

from ..api.clob import Book
from ..backtest.replay import replay_quotes
from ..model import Market
from .opportunities import Mode
from .scoring import (
    competing_score,
    order_score,
    planned_price,
    qmin,
    quote_spread,
    score_on_book,
)

# Un marché déjà résolu mais encore `closed=false` garde un pool intact et n'a
# plus aucune liquidité concurrente : le rendement calculé y explose (118 %/jour
# mesuré) alors qu'il n'est pas versable. C'est le piège le plus coûteux de
# cette stratégie — d'où un plancher d'heures restantes, non négociable.
MIN_HOURS_LEFT = 24.0

# Nombre minimal de points de prix pour qu'une dérive veuille dire quelque chose.
MIN_HISTORY_POINTS = 30

# Un jeu complet (une part de chaque branche) coûte 1 $ : c'est l'identité qui
# définit le marché, et donc l'unité de capital de toute cette stratégie.
FULL_SET_USD = 1.0


@dataclass(frozen=True)
class PathStats:
    """Ce que le chemin de prix dit du risque, en pourcent du CAPITAL ENGAGÉ.

    L'unité n'est pas un détail de présentation : `net_yield` soustrait la
    dérive du rendement, et le rendement est en pourcent du capital engagé.
    Exprimer la dérive en pourcent du prix moyen (≈ 0,45 $) revenait à
    soustraire des pommes à des poires, et gonflait le risque d'un facteur ~2.
    """

    drift: float  # mouvement NET d'un bout à l'autre — la perte subie
    oscillation: float  # somme des variations — la fourchette capturable


@dataclass(frozen=True)
class RewardThresholds:
    """Conditions qu'un marché récompensé doit franchir."""

    min_net_yield: float  # rendement net minimal, en % / jour
    min_hours_left: float
    max_engaged_usd: float  # ticket d'entrée maximal, borné par le capital
    require_history: bool  # sans historique, la dérive est inconnue

    @staticmethod
    def for_mode(mode: Mode, *, bankroll: float) -> "RewardThresholds":
        if mode is Mode.SERIEUX:
            # Haute conviction : le net doit être franchement positif, pas
            # marginal — une dérive sous-estimée suffirait à l'effacer.
            return RewardThresholds(
                min_net_yield=1.0,
                min_hours_left=MIN_HOURS_LEFT,
                max_engaged_usd=bankroll,
                require_history=True,
            )
        return RewardThresholds(
            min_net_yield=0.0,
            min_hours_left=MIN_HOURS_LEFT,
            max_engaged_usd=bankroll,
            require_history=False,
        )


@dataclass(frozen=True)
class RewardCandidate:
    """Un marché récompensé, mesuré, avec la raison de sa retenue ou de son rejet."""

    condition_id: str
    question: str
    slug: str
    daily_pool: float  # dollars par jour versés par le marché
    # Concurrence et part se comptent en SCORES (`analysis/scoring`), pas en
    # dollars : le pool se partage au prorata des `Qmin`, où un ordre au bord
    # de la bande vaut zéro quel que soit le capital qu'il immobilise. Les deux
    # champs voyagent ensemble parce que le rendement est leur RAPPORT — garder
    # l'un sans l'autre laisserait recalculer une part avec un dénominateur
    # dont on ignore l'échelle.
    competing_q: float  # score déjà posté dans la bande qualifiante
    own_q: float  # score que nos deux ordres obtiendraient
    engaged_usd: float  # capital qu'il faut immobiliser pour qualifier
    gross_yield: float  # % / jour, avant risque d'inventaire
    drift: float  # % sur 24 h — le risque d'inventaire
    oscillation: float  # % sur 24 h — favorable, informatif
    hours_left: float | None
    rejected_by: tuple[str, ...] = ()
    # De quoi refaire le calcul plus tard sans repasser par Gamma : le flux
    # temps réel livre des carnets par jeton, et la bande qualifiante est le
    # seul paramètre du marché dont dépend la concurrence.
    token_ids: tuple[str, ...] = ()
    max_spread: float = 0.0
    # Coût d'inventaire MESURÉ par rejeu de la cotation sur l'historique 24 h,
    # en % du capital engagé, signé (négatif = perte). `None` quand aucun
    # historique n'était disponible : on ne le confond pas avec un coût nul.
    replay_cost: float | None = None

    @property
    def inventory_cost(self) -> float:
        """Ce que la tenue de marché coûte, en % / jour, négatif = perte.

        Le PIRE des deux mesures, parce qu'aucune ne majore l'autre :

        - `−drift` rate les allers-retours. Sur 0,50 → 0,53 → 0,50 la dérive
          est nulle et le rejeu perd 1 % : c'est le trou qui a fait céder le
          majorant sur 6 des 17 marchés cotés du 01/08/2026, jusqu'à 31 points
          d'écart par jour.
        - le rejeu rate les tendances lentes. Il recote chaque minute autour du
          nouveau prix, donc une dérive de 0,4 cent/minute ne touche jamais des
          cotes posées à 1,5 cent : coût mesuré nul sur un marché qui a pourtant
          parcouru 23 cents. Un teneur réel, lui, se fait passer dessus.

        Prendre le pire n'est pas de la prudence décorative : chacune des deux
        décrit un chemin de perte que l'autre ne voit pas.
        """
        if self.replay_cost is None:
            return -self.drift
        return min(self.replay_cost, -self.drift)

    @property
    def net_yield(self) -> float:
        """Le seul chiffre qui compte : rendement moins coût d'inventaire, en % / jour."""
        return self.gross_yield + self.inventory_cost

    @property
    def is_actionable(self) -> bool:
        return not self.rejected_by

    @property
    def daily_usd(self) -> float:
        """Gain net attendu en dollars par jour sur le capital engagé."""
        return self.engaged_usd * self.net_yield / 100.0


def daily_pool(market: Market) -> float:
    """Dollars par jour versés par le marché, somme des programmes actifs.

    Le champ vit dans `raw["clobRewards"]`, une liste d'entrées dont seule
    `rewardsDailyRate` nous intéresse. Toute entrée illisible est ignorée
    plutôt que de faire échouer le marché entier.
    """
    total = 0.0
    for entry in market.raw.get("clobRewards") or []:
        if not isinstance(entry, dict):
            continue
        try:
            total += float(entry.get("rewardsDailyRate") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def hours_until(end_date: datetime | None, *, now: datetime | None = None) -> float | None:
    if end_date is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (end_date - reference).total_seconds() / 3600.0


def path_stats(prices: Sequence[float]) -> PathStats | None:
    """Dérive et oscillation d'une série de prix, en pourcent du capital engagé.

    Le dénominateur est le prix du JEU COMPLET, soit 1 $ par part, parce que
    c'est ce que coûte la position : qualifier demande de poster `min_size`
    parts de chaque branche, et bid_yes + bid_no ≈ 1 $. Un prix qui bouge de
    4 cents fait donc perdre 4 % du capital à qui est rempli du mauvais côté,
    quel que soit le niveau du prix.

    Avantage secondaire : diviser par une constante égale à 1, c'est ne pas
    diviser — une série qui traîne près de zéro ne produit plus de pourcentages
    absurdes, sans avoir à choisir un prix de référence.
    """
    clean = [p for p in prices if p is not None]
    if len(clean) < MIN_HISTORY_POINTS:
        return None
    drift = abs(clean[-1] - clean[0]) * 100.0 / FULL_SET_USD
    oscillation = sum(abs(b - a) for a, b in zip(clean, clean[1:])) * 100.0 / FULL_SET_USD
    return PathStats(drift=drift, oscillation=oscillation)


def planned_q(min_size: float, max_spread: float) -> float:
    """Le score que NOS deux ordres obtiendraient sur ce marché.

    Les deux branches sont cotées à la même distance cible du milieu, donc les
    deux seaux sont égaux et `Qmin` vaut cette valeur commune, quelle que soit
    la règle de plage — d'où l'absence de milieu dans le calcul.

    C'est une HYPOTHÈSE BASSE : si le meilleur bid se trouve plus près du
    milieu que la cible, le planificateur s'y range et score davantage. Le
    rendement affiché est donc un plancher de placement, pas une promesse.
    """
    return order_score(min_size, quote_spread(max_spread), max_spread)


def planned_q_on_books(
    books: Mapping[str, Book],
    token_ids: Sequence[str],
    min_size: float,
    max_spread: float,
) -> float | None:
    """Notre score RÉEL sur ce marché, effet de notre propre ordre compris.

    `planned_q` répond « ce que vaudrait un ordre posté à mi-bande » ; cette
    fonction répond « ce que vaudrait NOTRE ordre sur CE carnet », ce qui n'est
    pas la même chose dès que le carnet est large : en améliorant le meilleur
    bid, l'ordre déplace le milieu et s'éloigne de lui-même
    (`distance_after_posting`).

    L'écart n'est pas marginal. Sur les dix marchés à concurrence nulle du
    balayage du 2026-07-31, `planned_q` annonçait 5 à 8 points et le score réel
    valait ZÉRO : des carnets béants où qualifier est impossible, et que le
    classement plaçait en tête précisément parce qu'ils paraissaient déserts.

    Nos deux achats tombent dans des seaux différents (Qone et Qtwo), d'où le
    `qmin` : c'est la branche la PIRE placée qui commande, pas la moyenne.
    """
    if len(token_ids) < 2 or max_spread <= 0 or min_size <= 0:
        return None
    book_yes, book_no = books.get(token_ids[0]), books.get(token_ids[1])
    if book_yes is None or book_no is None:
        return None
    mid_yes = book_yes.midpoint
    if mid_yes is None:
        return None

    scores: list[float] = []
    for book in (book_yes, book_no):
        price = planned_price(book, max_spread)
        if price is None:
            return None
        score = score_on_book(book, price, min_size, max_spread)
        if score is None:
            return None
        scores.append(score)
    return qmin(scores[0], scores[1], midpoint=mid_yes)


def judged_on_average(
    candidate: RewardCandidate, competing_q: float
) -> RewardCandidate:
    """Le même candidat, jugé sur une concurrence MOYENNÉE dans le temps.

    Notre propre score ne change pas : il ne dépend que de notre prix et de
    notre taille, tous deux à nous. Seule la concurrence est un tirage — et
    c'est elle qu'il faut moyenner.
    """
    return replace(
        candidate,
        competing_q=competing_q,
        gross_yield=gross_yield_from_scores(
            daily_pool=candidate.daily_pool,
            own=candidate.own_q,
            competing=competing_q,
            engaged_usd=candidate.engaged_usd,
        ),
    )


def reranked_on_average(
    candidates: Sequence[RewardCandidate],
    averages: Mapping[str, float],
) -> list[RewardCandidate]:
    """Reclasse au net après avoir remplacé l'instantané par la moyenne.

    Pourquoi ce reclassement existe (mesuré le 2026-07-29, quatrième mesure) :
    la tête du classement affichait +140,47 %/jour au balayage, et son net a
    oscillé entre +29,61 et +83,10 %/jour en quarante-cinq secondes de flux. Un
    chiffre de balayage est **périmé quand il s'imprime**, et surestime d'un
    facteur 2 à 5. Le bot choisissait pourtant quoi coter avec ce chiffre-là.

    Un candidat sans moyenne fiable garde son instantané plutôt que d'être
    écarté : l'absence de mesure n'est pas une mauvaise mesure. Mais il est
    alors classé sur un nombre dont on sait qu'il flatte — d'où le tri
    secondaire, qui départage à égalité en faveur de ceux qui ONT été observés.
    """
    judged = [
        judged_on_average(c, averages[c.condition_id])
        if c.condition_id in averages
        else c
        for c in candidates
    ]
    return sorted(
        judged,
        key=lambda c: (c.net_yield, c.condition_id in averages),
        reverse=True,
    )


def gross_yield_from_scores(
    *, daily_pool: float, own: float, competing: float, engaged_usd: float
) -> float:
    """Rendement brut en % / jour : notre part du pool, sur le capital engagé.

    Une seule définition, partagée par le balayage et par les recalculs du flux.
    C'est ce qui rend comparables les trois colonnes de la page (balayage,
    instantané, moyenne) : si chacune diluait à sa façon, leur écart ne dirait
    plus rien du marché, seulement de nos formules.

    Noter la différence d'unités, qui est le fond de la correction : la PART se
    joue en scores (`own / (competing + own)`), le RENDEMENT se rapporte à des
    dollars. Mélanger les deux — l'ancien `engaged / (competing_usd + engaged)`
    — revenait à supposer qu'un dollar posté vaut un point de score, ce qui est
    faux dès qu'un ordre n'est pas collé au milieu.
    """
    if own <= 0 or engaged_usd <= 0 or competing < 0:
        return 0.0
    return 100.0 * daily_pool * own / ((competing + own) * engaged_usd)


def evaluate_reward_market(
    market: Market,
    books: Mapping[str, Book],
    *,
    prices: Sequence[float] | None = None,
    thresholds: RewardThresholds,
    now: datetime | None = None,
) -> RewardCandidate | None:
    """Mesure un marché récompensé. Renvoie None s'il n'y a rien à mesurer.

    Comme pour les opportunités d'arbitrage, un candidat rejeté est renvoyé
    avec ses motifs plutôt que filtré en silence : c'est ce qui permet de
    savoir si le marché n'offre rien, ou si nos seuils sont mal réglés.
    """
    pool = daily_pool(market)
    if pool <= 0:
        return None

    max_spread = market.rewards_max_spread
    min_size = market.rewards_min_size
    if not max_spread or not min_size or min_size <= 0:
        return None

    # `rewardsMaxSpread` est exprimé en POURCENT (3.5 = 3,5 cents), les prix du
    # carnet en dollars. Sans cette division, tout le carnet qualifie.
    band = max_spread / 100.0

    # Un carnet manquant rend None ; un score nul est en revanche une VRAIE
    # information (personne n'est posté assez près du milieu pour marquer), et
    # c'est exactement le pool sous-peuplé que la stratégie cherche.
    competing = competing_score(books, market.token_ids, band)
    if competing is None:
        return None

    # Coter les deux branches au bid coûte environ `rewardsMinSize` dollars,
    # puisque bid_yes + bid_no ≈ 1 $ par jeu complet — même unité que la dérive.
    # Le capital reste en DOLLARS ; la part du pool, elle, se joue en SCORES.
    engaged = float(min_size) * FULL_SET_USD
    # Le score de NOS ordres se mesure sur le carnet, pas sur une bande
    # théorique : sur un carnet large, notre propre ordre déplace le milieu et
    # se disqualifie tout seul. `planned_q` seul annoncerait 5 points là où la
    # réalité en vaut 0 — et ces marchés-là sont ceux qui montent en tête.
    ours = planned_q_on_books(books, market.token_ids, float(min_size), band)
    if ours is None:
        return None
    gross_yield = gross_yield_from_scores(
        daily_pool=pool, own=ours, competing=competing, engaged_usd=engaged
    )

    stats = path_stats(prices or ())
    remaining = hours_until(market.end_date, now=now)

    # Le coût d'inventaire se MESURE sur le même historique que la dérive, en
    # rejouant exactement la cotation que `execute/orders` poserait. C'est le
    # même calcul que la commande `backtest`, appliqué ici marché par marché.
    replay_cost: float | None = None
    if prices and len(prices) >= 2:
        half_spread = quote_spread(band)
        if half_spread > 0:
            replay_cost = replay_quotes(
                prices, half_spread=half_spread, size=float(min_size)
            ).pnl_pct

    failures: list[str] = []
    # Dit AVANT les autres motifs, parce que c'est le seul qui soit rédhibitoire
    # par construction : un carnet trop large ne se corrige pas en baissant un
    # seuil. Sans ce motif, ces marchés ressortaient « rejetés pour net
    # insuffisant », ce qui laissait croire qu'ils reviendraient un jour.
    if ours <= 0:
        failures.append("carnet trop large — notre ordre sortirait de la bande")
    if remaining is None or remaining < thresholds.min_hours_left:
        shown = "inconnue" if remaining is None else f"{remaining:.0f}h"
        failures.append(f"échéance {shown} < {thresholds.min_hours_left:.0f}h")
    if engaged > thresholds.max_engaged_usd:
        failures.append(f"ticket {engaged:.0f}$ > {thresholds.max_engaged_usd:.0f}$")
    if stats is None:
        if thresholds.require_history:
            failures.append("risque inconnu (historique absent)")
    else:
        cost = -stats.drift if replay_cost is None else min(replay_cost, -stats.drift)
        net = gross_yield + cost
        if net < thresholds.min_net_yield:
            failures.append(f"net {net:+.2f}%/j < {thresholds.min_net_yield:.2f}%/j")

    return RewardCandidate(
        condition_id=market.condition_id,
        question=market.question,
        slug=market.slug,
        daily_pool=pool,
        competing_q=competing,
        own_q=ours,
        engaged_usd=engaged,
        gross_yield=gross_yield,
        drift=stats.drift if stats else 0.0,
        oscillation=stats.oscillation if stats else 0.0,
        hours_left=remaining,
        rejected_by=tuple(failures),
        token_ids=market.token_ids,
        max_spread=band,
        replay_cost=replay_cost,
    )


def with_competing(
    candidate: RewardCandidate, competing: float
) -> RewardCandidate:
    """Refait le rendement brut sur une autre mesure de concurrence, EN SCORE.

    Une seule formule, quelle que soit la provenance du chiffre — carnet du
    balayage, carnet du flux, ou moyenne temporelle. C'est ce qui garantit que
    les trois colonnes de la page sont comparables entre elles.

    Un score concurrent NUL est accepté et n'est pas un cas dégénéré : il veut
    dire que personne n'est posté assez près du milieu pour marquer, donc que
    le pool entier nous revient. C'est précisément le pool sous-peuplé que la
    stratégie cherche, et l'écarter comme une valeur suspecte reviendrait à
    jeter les seules occasions qui justifient de balayer.
    """
    if candidate.own_q <= 0 or candidate.engaged_usd <= 0 or competing < 0:
        return candidate
    return replace(
        candidate,
        competing_q=competing,
        gross_yield=gross_yield_from_scores(
            daily_pool=candidate.daily_pool,
            own=candidate.own_q,
            competing=competing,
            engaged_usd=candidate.engaged_usd,
        ),
    )


def competing_from_books(
    candidate: RewardCandidate, books: Mapping[str, Book]
) -> float | None:
    """Score concurrent d'un candidat sur des carnets donnés.

    Renvoie None dès qu'un carnet manque : une déconnexion partielle ne doit
    pas ressembler à un pool à moitié vide. Le calcul prend les DEUX branches
    ensemble et non branche par branche, parce que `Qone`/`Qtwo` croisent les
    bids d'une branche avec les asks de l'autre : sommer deux mesures
    séparées ne reconstitue pas le `Qmin` du marché.
    """
    if not candidate.token_ids or candidate.max_spread <= 0:
        return None
    return competing_score(books, candidate.token_ids, candidate.max_spread)


def recompute_with_books(
    candidate: RewardCandidate, books: Mapping[str, Book]
) -> RewardCandidate:
    """Refait le rendement d'un candidat sur des carnets plus frais.

    C'est le cœur de la surveillance temps réel, et ça repose sur une
    asymétrie mesurée : la concurrence bouge en MINUTES (un candidat observé
    est passé de 461 $ à 256 $ de liquidité concurrente en quelques minutes,
    son net de 22 à 42 %/jour), alors que la dérive est une statistique sur
    24 h qui ne peut pas changer à cette échelle. On recalcule donc le brut,
    et on garde la dérive du balayage.

    Un carnet manquant laisse le candidat inchangé plutôt que de produire un
    zéro : une déconnexion ne doit pas ressembler à un pool qui se vide.
    """
    competing = competing_from_books(candidate, books)
    if competing is None:
        return candidate
    return with_competing(candidate, competing)


def rank_reward_markets(
    markets: Sequence[Market],
    books: Mapping[str, Book],
    histories: Mapping[str, Sequence[float]],
    *,
    mode: Mode = Mode.SERIEUX,
    bankroll: float,
    now: datetime | None = None,
) -> list[RewardCandidate]:
    """Classe l'univers récompensé par rendement NET décroissant.

    `histories` est indexé par identifiant de jeton : c'est la première branche
    du marché qui sert de référence de prix, les deux branches étant liées par
    prix_yes + prix_no = 1.
    """
    thresholds = RewardThresholds.for_mode(mode, bankroll=bankroll)
    candidates: list[RewardCandidate] = []
    for market in markets:
        if not market.outcomes:
            continue
        prices = histories.get(market.outcomes[0].token_id)
        candidate = evaluate_reward_market(
            market, books, prices=prices, thresholds=thresholds, now=now
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.net_yield, reverse=True)
    return candidates


def actionable(candidates: Sequence[RewardCandidate]) -> list[RewardCandidate]:
    return [c for c in candidates if c.is_actionable]


def allocate(
    candidates: Sequence[RewardCandidate], *, bankroll: float
) -> list[RewardCandidate]:
    """Le sous-ensemble réellement tenable EN MÊME TEMPS avec ce capital.

    `evaluate_reward_market` vérifie le ticket marché par marché : chacun tient
    dans le capital, pris isolément. Additionner leurs gains laisse croire qu'on
    peut tous les tenir, ce qui est faux dès que la somme des tickets dépasse le
    capital — six candidats à 20-50 $ engagent 200 $, pas 100 $. C'est le genre
    de total qui fait prendre une position qu'on ne peut pas financer.

    Chaque ticket est imposé par `rewardsMinSize` : on ne peut pas en prendre la
    moitié. C'est donc un problème de sac à dos. Le glouton par rendement net
    (le capital va d'abord là où chaque dollar rapporte le plus) n'est pas
    exactement optimal, mais sur une poignée de candidats l'écart est de
    quelques centimes — et il reste explicable, ce qui vaut mieux ici.
    """
    remaining = bankroll
    kept: list[RewardCandidate] = []
    for candidate in sorted(candidates, key=lambda c: c.net_yield, reverse=True):
        if candidate.engaged_usd <= remaining:
            kept.append(candidate)
            remaining -= candidate.engaged_usd
    return kept
