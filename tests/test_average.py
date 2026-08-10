"""Tests de la moyenne temporelle.

Le piège que ces tests verrouillent est subtil et coûteux : faire la moyenne
des MESSAGES au lieu de la moyenne du TEMPS. Les mises à jour du carnet
arrivent par rafales ; une moyenne des messages donnerait le plus de poids aux
moments d'agitation, c'est-à-dire à ceux où le carnet est le plus encombré.
Elle surestimerait donc la concurrence exactement dans le sens qui fait rater
les pools déserts — sans jamais produire d'erreur visible.
"""

from __future__ import annotations

import pytest

from donmarket.watch.average import TimeAverage


class TestTimeAverage:
    def test_has_no_mean_before_any_observation(self) -> None:
        assert TimeAverage().mean_at(10.0) is None

    def test_a_single_observation_is_its_own_mean(self) -> None:
        average = TimeAverage().observe(100.0, at=0.0)
        assert average.mean_at(10.0) == 100.0

    def test_weights_by_duration_not_by_message_count(self) -> None:
        # 100 pendant 9 s, puis 0 pendant 1 s → 90, et surtout pas 50.
        average = TimeAverage().observe(100.0, at=0.0).observe(0.0, at=9.0)
        assert average.mean_at(10.0) == pytest.approx(90.0)

    def test_a_burst_of_messages_does_not_outweigh_a_quiet_period(self) -> None:
        # Cinq messages en une seconde, puis le calme pendant dix secondes.
        average = TimeAverage()
        for index in range(5):
            average = average.observe(200.0, at=index * 0.2)
        average = average.observe(50.0, at=1.0)

        # Moyenne des messages : (200×5 + 50) / 6 ≈ 175. Moyenne du temps : ~63.
        assert average.mean_at(11.0) == pytest.approx(63.6, abs=0.5)

    def test_counts_the_time_since_the_last_message(self) -> None:
        # Le flux qui se tait ne veut pas dire que la valeur a disparu : elle
        # TIENT. Une moyenne figée au dernier message dirait le contraire.
        average = TimeAverage().observe(100.0, at=0.0).observe(0.0, at=1.0)
        assert average.mean_at(1.0) == pytest.approx(100.0)
        assert average.mean_at(101.0) == pytest.approx(1.0, abs=0.01)

    def test_reports_the_time_it_actually_covers(self) -> None:
        average = TimeAverage().observe(100.0, at=0.0).observe(50.0, at=30.0)
        assert average.covered_seconds(40.0) == pytest.approx(40.0)

    def test_covers_nothing_before_the_first_observation(self) -> None:
        assert TimeAverage().covered_seconds(99.0) == 0.0

    def test_the_first_observation_invents_no_past(self) -> None:
        # On ne sait pas depuis quand elle tenait : supposer une durée
        # reviendrait à fabriquer de l'historique.
        average = TimeAverage().observe(100.0, at=50.0)
        assert average.seconds == 0.0
        assert average.covered_seconds(50.0) == 0.0

    def test_is_immutable(self) -> None:
        original = TimeAverage().observe(100.0, at=0.0)
        original.observe(0.0, at=10.0)
        assert original.last_value == 100.0
        assert original.samples == 1

    def test_ignores_a_clock_that_goes_backwards(self) -> None:
        # `time.monotonic` ne recule pas, mais un test ou un rejeu peut le
        # faire : une durée négative empoisonnerait la moyenne durablement.
        average = TimeAverage().observe(100.0, at=10.0).observe(50.0, at=5.0)
        assert average.seconds == 0.0
        assert average.mean_at(5.0) == 50.0
