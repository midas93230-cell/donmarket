"""Le compte de démonstration : un solde qui bouge, sans risquer un dollar.

Ce paquet existe parce qu'il manquait la seule chose que le tableau de bord ne
savait pas faire : montrer de l'argent qui BOUGE. Le balayage classe des
marchés, le backtest mesure un coût passé, le mode ombre mesure une part de
pool — aucun ne tient de position ni de solde.

Ici on tient un compte : des ordres qui dorment sur le carnet, des remplissages
provoqués par les VRAIES exécutions du marché, un inventaire réévalué au prix
courant, et des récompenses accumulées. Le solde monte et descend pour les
mêmes raisons qu'il monterait et descendrait en réel.

## La limite à connaître avant de lire un chiffre d'ici

Nos ordres ne sont pas dans le carnet. Personne ne les voit, personne ne se
place derrière eux, et ils ne déplacent pas les prix. Le remplissage est donc
MODÉLISÉ, jamais observé — voir `fills.py`, qui prend systématiquement
l'hypothèse la plus défavorable. Les récompenses, elles, sont mesurées : le
score ne dépend que du prix et de la taille, tous deux connus exactement.
"""

from .fills import MarketTrade, RestingOrder, fill_against_trade
from .ledger import PaperAccount, PaperFill, Position

__all__ = [
    "MarketTrade",
    "PaperAccount",
    "PaperFill",
    "Position",
    "RestingOrder",
    "fill_against_trade",
]
