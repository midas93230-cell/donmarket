"""Le coût de suivre quelqu'un — les cinq centimes que personne ne publie.

## Pourquoi ce module existe

`SpookyDegenaro` fait tourner un « Whale Bot » qui repère les gros paris sur
Polymarket et recommande de les suivre. Bilan annoncé le 2026-09-02 :
289-139-1, **67,4 % de réussite**, ROI **+13,1 %** sur 429 paris réglés.

Ces chiffres sont cohérents entre eux — vérifié, pas supposé. +33,5895 u pour
+13,1 % implique 256,4 u misées sur 429 paris, donc une mise moyenne de 0,598,
donc un **prix d'entrée moyen de ~0,60**. L'espérance à ce prix est
0,674 × 0,402 − 0,326 × 0,598 = **+0,076 par pari**, soit +32,6 u sur 429
contre +33,59 annoncés : 3 % d'écart. Son avantage est réel.

Sauf que le seuil de rentabilité à 0,60 est de **60 %** de réussite. Son
avantage fait donc **7,4 points**, et c'est mince.

**Et son 0,60 est le prix À L'ALERTE.** Le pari de la baleine consomme le
carnet ; le suiveur remplit après, plus haut. À 0,62 le seuil passe à 62 % et
l'avantage tombe à 5,4 points. À 0,65, il reste 2,4 points. **Tout l'avantage
tient dans cinq centimes de carnet**, et aucun vendeur de signaux au monde ne
publie ces cinq centimes — parce que les mesurer demande le carnet à la
seconde de l'alerte, pas le prix affiché.

C'est le seul chiffre qui décide si suivre une baleine rapporte quelque chose.

## Ce que ce module refuse de faire

Quand le carnet ne peut pas absorber la taille demandée, il ne complète PAS les
parts manquantes au dernier prix connu. Cela produirait un prix moyen précis et
faux — exactement la faute que `verifier_portefeuille.py` existe pour dénoncer,
et qu'il a lui-même commise le 2026-09-03 en annonçant un plancher « garanti »
sur une fenêtre tronquée. Ici, un carnet trop mince rend ce qui est
remplissable et le signale par `exhausted`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..api.clob import Book


@dataclass(frozen=True)
class TakerFill:
    """Ce qu'un preneur obtient réellement, opposé à ce qu'on lui annonce."""

    side: str
    requested: float
    filled: float
    """Parts réellement obtenables. Inférieur à `requested` si le carnet cède."""

    quoted: float
    """Le meilleur prix affiché — celui que porte une alerte."""

    effective: float
    """Moyenne pondérée réellement payée sur les paliers consommés.

    Calculée sur `filled`, jamais sur `requested` : moyenner sur des parts
    qu'on n'a pas obtenues inventerait un prix.
    """

    slippage: float
    """`effective` − `quoted`, TOUJOURS POSITIF OU NUL, dans les deux sens.

    Un glissement est un coût. Le signer par le sens de l'ordre ferait
    apparaître les ventes comme profitables au glissement, ce qui inverserait
    la conclusion de la mesure.
    """

    exhausted: bool
    """Vrai si le carnet s'est épuisé avant la taille demandée."""

    @property
    def slippage_pct(self) -> float:
        """Le glissement rapporté au prix annoncé."""
        return self.slippage / self.quoted if self.quoted else 0.0


def taker_fill(book: Book, side: str, shares: float) -> TakerFill | None:
    """Parcourt le carnet palier par palier pour la taille demandée.

    `side` est le sens de l'ORDRE : « BUY » consomme les `asks` et glisse vers
    le haut, « SELL » consomme les `bids` et glisse vers le bas.

    Rend `None` — et non un objet vide — quand il n'y a rien à mesurer :
    carnet vide, ou taille nulle ou négative. Un `TakerFill` à zéro se
    confondrait avec un glissement nul, qui est un résultat, pas une absence.
    """
    if shares <= 0:
        return None

    achat = side.upper() == "BUY"
    niveaux = book.asks if achat else book.bids
    if not niveaux:
        return None

    quoted = niveaux[0].price
    restant = float(shares)
    cout = 0.0
    obtenu = 0.0
    for niveau in niveaux:
        pris = min(restant, niveau.size)
        cout += pris * niveau.price
        obtenu += pris
        restant -= pris
        if restant <= 0:
            break

    if obtenu <= 0:
        return None

    effective = cout / obtenu
    # `abs` et non une soustraction orientée : voir le docstring de `slippage`.
    # Un achat glisse vers le haut, une vente vers le bas, et les deux coûtent.
    return TakerFill(
        side="BUY" if achat else "SELL",
        requested=float(shares),
        filled=obtenu,
        quoted=quoted,
        effective=effective,
        slippage=abs(effective - quoted),
        exhausted=restant > 0,
    )


def breakeven_win_rate(price: float) -> float:
    """Le taux de réussite qu'un prix exige pour ne rien perdre.

    Payer `p` pour recevoir 1,00 rapporte (1 − p) en cas de gain et coûte `p`
    en cas de perte. L'espérance s'annule quand w(1 − p) = (1 − w)p, donc
    quand **w = p**. Le prix EST le seuil, ce qui est la raison pour laquelle
    un taux de réussite seul ne dit jamais si quelqu'un gagne de l'argent :
    80 % de réussite à 0,80 payé est une espérance nulle, négative avec frais.

    C'est la réfutation de la piste « miser sur les favoris », et c'est aussi
    ce qui rend la mesure du glissement décisive : le glissement déplace `p`,
    donc déplace le seuil, donc mange l'avantage directement.
    """
    if not 0.0 < price < 1.0:
        raise ValueError(
            f"prix hors de ]0, 1[ : {price!r}. Un prix Polymarket est une "
            "probabilité ; 0 et 1 sont des résolutions, pas des prix."
        )
    return price
