"""Rejeu de la stratégie de tenue de marché sur les prix passés.

Ce paquet ne backteste PAS le rendement. Il ne le peut pas : `/prices-history`
rend les prix passés, jamais les carnets passés, donc ni la concurrence, ni les
scores, ni la part du pool ne sont reconstituables. Prétendre le contraire
produirait un chiffre inventé.

Ce qui est mesurable sur les prix seuls, c'est le **coût** de la stratégie —
et c'est précisément le terme que `analysis/rewards` n'estime aujourd'hui que
par un majorant.
"""

from .replay import ReplayResult, replay_quotes

__all__ = ["ReplayResult", "replay_quotes"]
