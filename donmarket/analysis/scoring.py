"""La formule de récompense de Polymarket, telle qu'elle est publiée.

Source : `docs.polymarket.com/market-makers/liquidity-rewards`.

    S(v, s) = ((v − s)/v)² · b

`v` est la bande qualifiante (`rewardsMaxSpread`), `s` la distance de l'ordre
au milieu du carnet, `b` sa taille EN PARTS. Les scores sont relevés chaque
minute ; à la fin de l'époque chacun touche `Q_à_nous / Σ Q` du pool.

Les deux seaux :

    Qone = bids(m) + asks(m')        Qtwo = asks(m) + bids(m')

Acheter YES et vendre NO fournissent la même liquidité économique, donc le
même seau. Nos deux ordres d'achat (YES et NO) tombent chacun dans un seau
différent : c'est exactement la cotation équilibrée que la formule récompense.

    Qmin = max(min(Qone, Qtwo), max(Qone, Qtwo)/c)   si milieu ∈ [0,10 ; 0,90]
    Qmin = min(Qone, Qtwo)                           sinon

## Ce que ce module corrige

Le calcul précédent (`rewards.competing_liquidity`) sommait les DOLLARS postés
dans la bande, sans pondération de distance. Trois écarts, tous dans le sens
d'un chiffre flatteur :

- **Pas de pondération.** Un ordre au bord de la bande y comptait autant qu'un
  ordre collé au milieu, alors qu'il vaut littéralement zéro.
- **Des dollars au lieu de parts.** `prix × taille` fait qu'un marché à 0,90
  paraissait porter 9 fois plus de concurrence qu'un marché à 0,10 à liquidité
  identique.
- **Une somme au lieu d'un `min`.** Additionner les deux branches laissait
  croire qu'une branche bien placée rattrape une branche mal placée. C'est
  l'inverse : `min` retient la pire.

## Une honnêteté à tenir sur la concurrence

Le carnet est anonyme : on voit des paliers agrégés, pas le `Qone`/`Qtwo` de
chaque participant. Or la somme des `Qmin` individuels n'est pas le `Qmin` des
sommes. On applique donc la formule aux agrégats, ce qui donne :

- hors de [0,10 ; 0,90], un vrai MAJORANT de la concurrence
  (Σ min(aᵢ,bᵢ) ≤ min(Σaᵢ, Σbᵢ)), donc une part sous-estimée — prudent ;
- dans la plage, une approximation dont le sens n'est pas garanti, à cause du
  terme `max/c`.

Aucune de ces deux valeurs n'est le chiffre exact, et il n'existe pas de moyen
de l'obtenir depuis un carnet anonyme. La direction prudente est documentée
plutôt que masquée.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..api.clob import Book, Level

# Au-delà de ces bornes de milieu, la liquidité doit être des DEUX côtés pour
# marquer : Polymarket refuse de payer un côté seul sur un marché déjà tranché.
IN_GAME_LOW = 0.10
IN_GAME_HIGH = 0.90

# Le diviseur appliqué à la liquidité d'un seul côté dans la plage centrale.
# « currently 3.0 on all markets » — c'est un paramètre de Polymarket, pas un
# choix de notre part : s'il change, c'est ici et nulle part ailleurs.
SINGLE_SIDED_PENALTY = 3.0


def order_score(size: float, spread: float, max_spread: float) -> float:
    """S(v, s) = ((v − s)/v)² · b — le score d'un ordre, en parts pondérées.

    Rend exactement 0 au bord de la bande et au-delà. Ce n'est pas un cas
    limite décoratif : c'est le prix auquel le planificateur postait avant
    correction, et il rapportait donc zéro pour un risque d'inventaire entier.
    """
    if max_spread <= 0 or size <= 0:
        return 0.0
    if spread >= max_spread:
        return 0.0
    distance = max(spread, 0.0)
    factor = (max_spread - distance) / max_spread
    return factor * factor * size


def _side_score(levels: Sequence[Level], midpoint: float, max_spread: float) -> float:
    """Score cumulé d'un côté du carnet, chaque palier à sa propre distance."""
    return sum(
        order_score(level.size, abs(level.price - midpoint), max_spread)
        for level in levels
    )


def qmin(q_one: float, q_two: float, *, midpoint: float) -> float:
    """Le score retenu par Polymarket à partir des deux seaux.

    Bornes INCLUSIVES : à 0,10 pile, la pénalité s'applique encore plutôt que
    l'annulation. La doc écrit [0.10, 0.90], on la suit au caractère près.
    """
    low, high = min(q_one, q_two), max(q_one, q_two)
    if IN_GAME_LOW <= midpoint <= IN_GAME_HIGH:
        return max(low, high / SINGLE_SIDED_PENALTY)
    return low


def market_scores(
    book_yes: Book, book_no: Book, max_spread: float
) -> tuple[float, float] | None:
    """Les deux seaux (Qone, Qtwo) tels que le carnet visible les remplit.

    Renvoie None si l'un des deux carnets n'a pas de milieu calculable : sans
    milieu, la distance n'a pas de sens et un score de 0 se confondrait avec
    « personne n'est posté ».
    """
    if max_spread <= 0:
        return None
    mid_yes, mid_no = book_yes.midpoint, book_no.midpoint
    if mid_yes is None or mid_no is None:
        return None

    q_one = _side_score(book_yes.bids, mid_yes, max_spread) + _side_score(
        book_no.asks, mid_no, max_spread
    )
    q_two = _side_score(book_yes.asks, mid_yes, max_spread) + _side_score(
        book_no.bids, mid_no, max_spread
    )
    return q_one, q_two


def competing_score(
    books: Mapping[str, Book], token_ids: Sequence[str], max_spread: float
) -> float | None:
    """Le score déjà posté sur ce marché — ce contre quoi notre part se dilue.

    Renvoie None dès qu'un carnet manque : une déconnexion partielle ne doit
    pas ressembler à un pool à moitié vide, sur lequel on se précipiterait.
    """
    if len(token_ids) < 2:
        return None
    book_yes, book_no = books.get(token_ids[0]), books.get(token_ids[1])
    if book_yes is None or book_no is None:
        return None
    scores = market_scores(book_yes, book_no, max_spread)
    if scores is None:
        return None
    mid_yes = book_yes.midpoint
    if mid_yes is None:  # déjà garanti par market_scores, gardé pour le typage
        return None
    return qmin(*scores, midpoint=mid_yes)


# ---------------------------------------------------------------------------
# À partir d'ici : NOTRE placement, qui est un choix, pas la formule ci-dessus.
# ---------------------------------------------------------------------------

# Où l'on se poste dans la bande, en fraction de celle-ci. Le score varie en
# (1 − fraction)² : au bord (1,0) il est NUL, au milieu (0,5) il vaut 25 % du
# maximum, collé au milieu du carnet (0,0) il est plein mais on se fait remplir
# en permanence. 0,5 est un compromis assumé entre score et risque
# d'inventaire — c'est le paramètre à bouger si la mesure dit qu'il a tort,
# et le seul endroit où il est écrit.
QUOTE_FRACTION = 0.5


def quote_spread(max_spread: float, fraction: float = QUOTE_FRACTION) -> float:
    """La distance au milieu à laquelle on se poste.

    Volontairement à l'écart du bord : viser `max_spread` exactement, c'est
    viser un score de zéro, et un arrondi au tick suffit alors à sortir de la
    bande. Viser le milieu de la bande laisse au contraire l'arrondi vers le
    bas sans conséquence (un tick sur 15 coûte 4 % du score, pas 100 %).
    """
    return max_spread * fraction


def distance_after_posting(
    best_bid: float | None, best_ask: float | None, price: float
) -> float | None:
    """Notre distance au milieu UNE FOIS notre ordre posé.

    Le milieu n'est pas une donnée extérieure : c'est la moyenne du meilleur
    bid et du meilleur ask, et un ordre qui améliore le carnet DEVIENT le
    meilleur bid. Il déplace donc le milieu vers lui-même, et s'éloigne de la
    moitié de ce déplacement.

    Mesuré le 2026-07-31, et c'est le défaut qui a produit « +238 %/jour » :
    sur un carnet à bid 0,452 / ask 0,698 (0,246 d'écart) avec une bande de
    0,045, viser `milieu − bande/2` = 0,5525 semble confortable. Une fois posé,
    le milieu passe à 0,62525 et notre distance à 0,07275 — HORS bande, nul.

    En postant à `m − v/2` la distance finale vaut `(A−B)/2 + v/2` : on ne
    marque donc que si l'écart du carnet ne dépasse pas **trois fois la bande**.
    Un carnet béant est inqualifiable, et c'est exactement là que la
    concurrence paraît nulle — les deux se produisent ensemble, ce qui rendait
    le piège systématique plutôt qu'occasionnel.
    """
    if best_ask is None:
        return None
    resting = best_bid if best_bid is not None else price
    new_bid = max(resting, price)
    new_mid = (new_bid + best_ask) / 2.0
    return abs(new_mid - price)


def score_on_book(
    book: Book, price: float, size: float, max_spread: float
) -> float | None:
    """Le score que NOTRE ordre obtiendrait réellement sur ce carnet.

    Renvoie None si le carnet n'a pas d'ask : sans lui il n'y a pas de milieu,
    donc pas de distance, et un score de 0 se confondrait avec « posté trop
    loin » alors qu'on ne sait simplement pas lire.
    """
    distance = distance_after_posting(book.best_bid, book.best_ask, price)
    if distance is None:
        return None
    return order_score(size, distance, max_spread)


def planned_price(book: Book, max_spread: float) -> float | None:
    """Le prix d'achat que le planificateur retiendrait sur ce carnet.

    Une seule définition de la règle de placement, partagée par le rendement
    (`analysis/rewards`) et par les ordres (`execute/orders`). Si les deux
    divergeaient, le classement décrirait une stratégie que le bot ne joue pas
    — le rendement porterait sur un prix, les ordres sur un autre.

    Le tick n'intervient pas ici : arrondir est le métier du planificateur, qui
    connaît le pas du jeton. Ce prix est la cible avant arrondi.
    """
    mid = book.midpoint
    if mid is None or book.best_bid is None:
        return None
    target = quote_spread(max_spread)
    if mid - book.best_bid <= target:
        return book.best_bid
    return mid - target


def own_score(
    price_yes: float,
    price_no: float,
    size: float,
    *,
    mid_yes: float,
    max_spread: float,
) -> float:
    """Notre score si on posait ces deux achats — l'un dans Qone, l'autre dans Qtwo.

    `mid_no` se déduit de `mid_yes` : les deux branches sont liées par
    prix_yes + prix_no = 1, donc leurs milieux aussi.
    """
    mid_no = 1.0 - mid_yes
    q_one = order_score(size, mid_yes - price_yes, max_spread)
    q_two = order_score(size, mid_no - price_no, max_spread)
    return qmin(q_one, q_two, midpoint=mid_yes)
