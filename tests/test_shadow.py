"""Le mode ombre, vérifié sur des parts et des durées calculées à la main.

Ce module produit le SEUL chiffre que la stratégie n'avait jamais mesuré : la
part de pool réellement obtenue. Un test qui relirait la sortie pour en faire
son attendu ne vérifierait rien — chaque valeur ci-dessous est donc calculée
dans la docstring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from donmarket.execute.shadow import (
    MIN_MINUTES_TO_EXTRAPOLATE,
    ShadowLedger,
    ShadowSample,
    compare_to_estimate,
)

START = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _sample(
    minute: int,
    *,
    own: float = 10.0,
    competing: float = 30.0,
    pool: float = 100.0,
    engaged: float = 50.0,
) -> ShadowSample:
    return ShadowSample(
        condition_id="0xabc",
        observed_at=START + timedelta(minutes=minute),
        own_q=own,
        competing_q=competing,
        daily_pool=pool,
        engaged_usd=engaged,
        midpoint=0.50,
    )


def _ledger(samples) -> ShadowLedger:
    return ShadowLedger(condition_id="0xabc", question="Will X?", samples=tuple(samples))


class TestShare:
    def test_share_is_the_ratio_of_scores(self):
        """10 contre 30 : notre part est 10/40 = 25 %, pas 33 %.

        Le dénominateur inclut NOTRE score. L'oublier annoncerait un tiers du
        pool là où on en touche un quart.
        """
        assert _sample(0).share == pytest.approx(0.25)

    def test_an_empty_book_gives_everything(self):
        """Personne d'autre dans la bande : tout le pool.

        C'est le cas que la stratégie cherche — et le plus fragile : un seul
        teneur qui arrive le divise.
        """
        assert _sample(0, competing=0.0).share == pytest.approx(1.0)

    def test_a_score_of_zero_takes_nothing(self):
        """Carnet trop large : notre ordre sort de la bande et ne marque rien.

        Sans ce garde-fou, un score nul face à une concurrence nulle donnerait
        0/0 — et la tentation serait de répondre 1,0, soit tout le pool pour un
        ordre qui ne qualifie pas.
        """
        assert _sample(0, own=0.0, competing=0.0).share == 0.0
        assert _sample(0, own=0.0, competing=10.0).share == 0.0

    def test_dollars_per_minute_divides_the_daily_pool(self):
        """Pool 100 $/jour, part 25 % → 25 $/jour → 25/1440 $/minute."""
        assert _sample(0).usd_per_minute == pytest.approx(25.0 / 1440.0)


class TestCoverage:
    def test_coverage_is_measured_on_timestamps_not_on_count(self):
        """Trois relevés à 0, 1 et 61 minutes couvrent 61 minutes, pas 3.

        Un fil qui décroche laisse un trou. Compter les relevés reviendrait à
        prétendre que le temps mort a été observé.
        """
        ledger = _ledger([_sample(0), _sample(1), _sample(61)])

        assert ledger.observations == 3
        assert ledger.minutes_covered == pytest.approx(61.0)

    def test_a_single_sample_covers_nothing(self):
        assert _ledger([_sample(0)]).minutes_covered == 0.0

    def test_too_short_a_run_refuses_to_extrapolate(self):
        """29 minutes ne disent rien d'une journée : le rendement reste None.

        Le refus est le comportement utile. Extrapoler trois relevés à 24 h
        produirait un « %/jour » crédible et faux.
        """
        short = _ledger([_sample(0), _sample(29)])

        assert short.minutes_covered < MIN_MINUTES_TO_EXTRAPOLATE
        assert short.measured_usd_per_day is None
        assert short.measured_yield is None

    def test_an_empty_ledger_is_not_a_crash(self):
        empty = _ledger([])

        assert empty.observations == 0
        assert empty.median_share is None
        assert empty.measured_yield is None
        assert empty.engaged_usd == 0.0


class TestMeasuredYield:
    def test_a_steady_run_measures_the_expected_yield(self):
        """Part 25 % constante, pool 100 $/j, capital engagé 50 $.

        Chaque relevé vaut 25/1440 $. La moyenne par relevé vaut donc aussi
        25/1440, et ramenée à 1440 relevés : 25 $/jour. Sur 50 $ engagés, cela
        fait 50 %/jour.
        """
        ledger = _ledger([_sample(minute) for minute in range(0, 61)])

        assert ledger.measured_usd_per_day == pytest.approx(25.0)
        assert ledger.measured_yield == pytest.approx(50.0)

    def test_sampling_twice_as_fast_does_not_double_the_result(self):
        """Le rendement mesuré ne doit pas dépendre de la cadence du fil.

        Deux registres couvrent la même heure, l'un avec 61 relevés, l'autre
        avec 31. Passer par la somme des minutes aurait donné le double au
        premier — un bug qui aurait rendu la mesure ininterprétable.
        """
        dense = _ledger([_sample(minute) for minute in range(0, 61)])
        sparse = _ledger([_sample(minute) for minute in range(0, 61, 2)])

        assert dense.measured_usd_per_day == pytest.approx(sparse.measured_usd_per_day)

    def test_the_median_share_resists_an_empty_minute(self):
        """Une minute à carnet vide ne doit pas porter tout le résultat.

        Soixante relevés à 25 %, un à 100 %. La médiane reste 25 % ; une
        moyenne serait tirée vers le haut par la seule minute exceptionnelle.
        """
        samples = [_sample(minute) for minute in range(0, 60)]
        samples.append(_sample(60, competing=0.0))

        assert _ledger(samples).median_share == pytest.approx(0.25)

    def test_no_capital_engaged_yields_nothing(self):
        ledger = _ledger([_sample(minute, engaged=0.0) for minute in range(0, 61)])

        assert ledger.measured_yield is None


class TestComparison:
    def test_the_gap_says_whether_the_scan_over_promises(self):
        """Mesuré 50 %/jour contre 80 %/jour estimés : le scan promet trop.

        C'est la raison d'être du mode ombre : l'estimation suppose que la
        concurrence de la seconde du balayage tient 24 h.
        """
        ledger = _ledger([_sample(minute) for minute in range(0, 61)])

        result = compare_to_estimate(ledger, estimated_yield=80.0)

        assert result is not None
        measured, gap = result
        assert measured == pytest.approx(50.0)
        assert gap == pytest.approx(-30.0)

    def test_no_comparison_before_the_measure_is_readable(self):
        assert compare_to_estimate(_ledger([_sample(0)]), estimated_yield=10.0) is None


class TestImmutability:
    def test_adding_a_sample_returns_a_new_ledger(self):
        original = _ledger([_sample(0)])

        grown = original.with_sample(_sample(1))

        assert original.observations == 1
        assert grown.observations == 2

    def test_a_sample_cannot_be_rewritten(self):
        with pytest.raises(Exception):
            _sample(0).own_q = 999.0  # type: ignore[misc]
