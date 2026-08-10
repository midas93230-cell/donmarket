"""Les plafonds durs — ce qui empêche une erreur de coûter tout le compte.

Ces tests sont les seuls du dépôt qui gardent une dépense d'argent réel. Chaque
attendu est calculé à la main : un plafond dont on relit la sortie pour en faire
l'assertion ne garde rien, il entérine ce que le code fait déjà.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from donmarket.execute.limits import ExecutionLimits, gate, order_cost_usd


@dataclass(frozen=True)
class _Order:
    """Le minimum que le portier regarde d'un ordre."""

    condition_id: str
    price: float
    size: float
    token_id: str = "t"
    side: str = "BUY"


def _limits(total=1000.0, per_market=200.0, orders=10) -> ExecutionLimits:
    return ExecutionLimits(
        max_total_usd=total, max_per_market_usd=per_market, max_orders=orders
    )


class TestLimitsRefuseToBeMeaningless:
    def test_a_zero_cap_is_a_refusal_not_an_absence_of_limit(self):
        """0 $ ne veut pas dire « pas de limite ». Le confondre viderait le compte."""
        with pytest.raises(ValueError):
            ExecutionLimits(max_total_usd=0.0, max_per_market_usd=10.0, max_orders=1)

    def test_a_negative_cap_is_refused(self):
        with pytest.raises(ValueError):
            ExecutionLimits(max_total_usd=-100.0, max_per_market_usd=10.0, max_orders=1)

    def test_zero_orders_is_refused(self):
        with pytest.raises(ValueError):
            ExecutionLimits(max_total_usd=100.0, max_per_market_usd=10.0, max_orders=0)

    def test_a_per_market_cap_above_the_total_is_incoherent(self):
        """Un plafond par marché plus permissif que le global ne borne rien.

        Il passerait pour une protection tout en autorisant un marché unique à
        consommer plus que l'exposition totale autorisée.
        """
        with pytest.raises(ValueError):
            ExecutionLimits(
                max_total_usd=100.0, max_per_market_usd=500.0, max_orders=5
            )

    def test_there_is_no_default_capital_cap(self):
        """`max_total_usd` doit être fourni : personne ne le décide à la place."""
        with pytest.raises(TypeError):
            ExecutionLimits()  # type: ignore[call-arg]


class TestCost:
    def test_cost_is_size_times_price(self):
        """100 parts à 0,45 $ immobilisent 45 $, pas 100 $."""
        assert order_cost_usd(_Order("0x1", price=0.45, size=100.0)) == pytest.approx(45.0)

    def test_a_negative_size_costs_nothing_rather_than_crediting(self):
        """Une taille négative ne doit pas RENDRE du plafond.

        Sans borne à zéro, un ordre malformé augmenterait le budget restant.
        """
        assert order_cost_usd(_Order("0x1", price=0.5, size=-100.0)) == 0.0


class TestTotalCap:
    def test_orders_stop_at_the_total_cap(self):
        """Trois ordres à 40 $, plafond global 100 $ : les deux premiers passent.

        40 + 40 = 80 ≤ 100 ; le troisième porterait à 120, il est refusé.
        """
        orders = [_Order(f"0x{i}", price=0.40, size=100.0) for i in range(3)]

        decision = gate(orders, limits=_limits(total=100.0, per_market=100.0))

        assert decision.allowed_count == 2
        assert decision.refused_count == 1
        assert "120.00" in decision.refused[0][1]

    def test_capital_already_engaged_counts_against_the_cap(self):
        """Relancer le moteur ne doit pas réengager le plafond une seconde fois.

        90 $ déjà immobilisés, plafond 100 $, un ordre à 40 $ : refusé. Sans ce
        paramètre, deux exécutions successives engageraient 200 $ sous un
        plafond de 100 $, chacune se croyant seule.
        """
        decision = gate(
            [_Order("0x1", price=0.40, size=100.0)],
            limits=_limits(total=100.0, per_market=100.0),
            already_engaged_usd=90.0,
        )

        assert decision.allowed_count == 0

    def test_exactly_at_the_cap_is_allowed(self):
        """100 $ pile sous un plafond de 100 $ passe : la borne est inclusive."""
        decision = gate(
            [_Order("0x1", price=1.0, size=100.0)],
            limits=_limits(total=100.0, per_market=100.0),
        )

        assert decision.allowed_count == 1


class TestPerMarketCap:
    def test_one_market_cannot_absorb_everything(self):
        """Le plafond qui compte le plus.

        Le classement remonte en tête les marchés à gros pool, et le rejeu du
        01/08/2026 a montré que c'est là que le modèle de risque se trompe le
        plus. Sans plafond par marché, tout le capital irait précisément là.
        """
        orders = [_Order("0xmeme", price=0.50, size=100.0) for _ in range(5)]

        decision = gate(orders, limits=_limits(total=1000.0, per_market=120.0))

        assert decision.allowed_count == 2  # 50 + 50 = 100 ≤ 120, le 3e ferait 150
        assert all("sur ce marché" in reason for _, reason in decision.refused)

    def test_other_markets_are_not_penalised_by_a_saturated_one(self):
        """Un marché saturé ne doit pas bloquer les suivants.

        Deux ordres sur A saturent son plafond ; l'ordre sur B doit passer.
        """
        orders = [
            _Order("0xA", price=0.50, size=100.0),
            _Order("0xA", price=0.50, size=100.0),
            _Order("0xB", price=0.50, size=100.0),
        ]

        decision = gate(orders, limits=_limits(total=1000.0, per_market=60.0))

        assert decision.allowed_count == 2
        assert [getattr(o, "condition_id") for o in decision.allowed] == ["0xA", "0xB"]


class TestOrderCountCap:
    def test_the_order_count_is_capped_independently_of_amounts(self):
        """Une erreur de boucle coûte autant qu'une erreur de montant.

        Dix ordres à 1 $ ne violent aucun plafond en dollars, mais dix ordres
        non voulus restent dix ordres.
        """
        orders = [_Order(f"0x{i}", price=0.01, size=100.0) for i in range(10)]

        decision = gate(orders, limits=_limits(total=1000.0, per_market=500.0, orders=3))

        assert decision.allowed_count == 3
        assert all("3 ordres" in reason for _, reason in decision.refused)


class TestGateBehaviour:
    def test_refused_orders_come_back_with_their_reason(self):
        """Un ordre qui disparaît en silence se lit comme un bug de stratégie."""
        decision = gate(
            [_Order("0x1", price=0.50, size=1000.0)],
            limits=_limits(total=100.0, per_market=100.0),
        )

        order, reason = decision.refused[0]
        assert order.condition_id == "0x1"
        assert reason

    def test_the_gate_does_not_reorder_to_fill_the_cap(self):
        """Le portier n'optimise pas : il garde l'ordre reçu.

        Un gros ordre en tête bloque le plafond même si deux petits derrière
        auraient mieux « rempli ». C'est voulu — un portier qui choisit quoi
        garder est une stratégie, et il faudrait alors la tester comme telle.
        """
        orders = [
            _Order("0xA", price=1.0, size=90.0),  # 90 $
            _Order("0xB", price=1.0, size=5.0),  # 5 $
            _Order("0xC", price=1.0, size=5.0),  # 5 $
        ]

        decision = gate(orders, limits=_limits(total=95.0, per_market=95.0))

        assert [getattr(o, "condition_id") for o in decision.allowed] == ["0xA", "0xB"]

    def test_an_empty_plan_is_not_a_crash(self):
        decision = gate([], limits=_limits())

        assert decision.allowed_count == 0
        assert decision.refused_count == 0
