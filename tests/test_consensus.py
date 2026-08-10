"""Tests du vote d'ensemble et de son diagnostic.

Le danger de cette méthode n'est pas de planter : c'est de convaincre. Un vote
« 28 sur 31 » a l'air d'une preuve accablante. Il ne l'est que si les 31 avis
sont indépendants — sinon c'est un seul avis répété 31 fois, et le seuil ne
filtre rien tout en donnant l'impression d'être exigeant.

Ces tests verrouillent donc surtout le diagnostic : qu'un ensemble de clones
soit reconnu comme tel, et qu'un ensemble réellement varié le soit aussi.
"""

from __future__ import annotations

import pytest

from donmarket.consensus.diagnostics import (
    analyse,
    effective_members,
    mean_correlation,
    vote_history,
)
from donmarket.consensus.ensemble import (
    Member,
    Vote,
    breakout,
    build_ensemble,
    decide,
    mean_reversion,
    momentum,
    volatility_filter,
    vote_all,
)

RISING = [0.40 + 0.002 * i for i in range(120)]
FALLING = [0.60 - 0.002 * i for i in range(120)]
FLAT = [0.50] * 120
# Dents de scie : monte et descend sans aller nulle part.
SAWTOOTH = [0.50 + (0.01 if i % 2 else -0.01) for i in range(120)]


class TestMembers:
    def test_momentum_follows_a_rise(self) -> None:
        assert momentum(30, 0.005)(RISING) is Vote.UP

    def test_momentum_follows_a_fall(self) -> None:
        assert momentum(30, 0.005)(FALLING) is Vote.DOWN

    def test_mean_reversion_bets_against_a_rise(self) -> None:
        # La famille opposée à l'élan : c'est ce qui crée de la diversité.
        assert mean_reversion(30, 0.005)(RISING) is Vote.DOWN

    def test_breakout_stays_silent_without_a_new_extreme(self) -> None:
        assert breakout(30)(FLAT) is Vote.ABSTAIN

    def test_volatility_filter_stays_silent_when_the_series_shakes(self) -> None:
        assert volatility_filter(30, 0.004)(SAWTOOTH) is Vote.ABSTAIN

    def test_a_member_abstains_rather_than_voting_on_a_truncated_window(self) -> None:
        # Voter sur trois points quand on en demande soixante, c'est voter sur
        # autre chose que ce qu'on croit — et ça se mélange aux autres avis.
        assert momentum(60, 0.005)([0.4, 0.41, 0.42]) is Vote.ABSTAIN


class TestEnsembleConstruction:
    def test_builds_the_requested_number_of_members(self) -> None:
        assert len(build_ensemble(31)) == 31

    def test_members_have_distinct_names(self) -> None:
        names = [member.name for member in build_ensemble(31)]
        assert len(set(names)) == 31

    def test_a_truncated_ensemble_still_mixes_families(self) -> None:
        # Sans entrelacement, les 31 premiers seraient 31 variantes d'élan.
        families = {member.name.split("-")[0] for member in build_ensemble(31)}
        assert len(families) >= 3


class TestVote:
    def _votes(self, up: int, down: int, abstain: int) -> list[Vote]:
        return [Vote.UP] * up + [Vote.DOWN] * down + [Vote.ABSTAIN] * abstain

    def test_supermajority_decides(self) -> None:
        assert decide(self._votes(28, 1, 2), threshold=28).decision is Vote.UP

    def test_below_the_threshold_it_abstains(self) -> None:
        assert decide(self._votes(27, 1, 3), threshold=28).decision is Vote.ABSTAIN

    def test_abstentions_count_against_the_threshold(self) -> None:
        # Trente membres muets et un seul qui parle ne font pas l'unanimité :
        # ils font un membre isolé.
        assert decide(self._votes(1, 0, 30), threshold=28).decision is Vote.ABSTAIN

    def test_reports_the_tally_even_when_it_abstains(self) -> None:
        consensus = decide(self._votes(10, 9, 12), threshold=28)
        assert (consensus.up, consensus.down, consensus.abstain) == (10, 9, 12)
        assert consensus.voters == 19
        assert consensus.total == 31


class TestDiagnostics:
    def _clones(self, count: int) -> list[Member]:
        # Le cas que la méthode ne doit PAS laisser passer : N copies du même
        # modèle, qui produiront toujours un vote unanime.
        return [Member(f"clone-{i}", momentum(30, 0.005)) for i in range(count)]

    def test_clones_are_worth_about_one_independent_vote(self) -> None:
        history = vote_history(self._clones(31), SAWTOOTH, step=3)
        assert effective_members(history) == pytest.approx(1.0, abs=0.2)

    def test_clones_reach_the_supermajority_every_single_time(self) -> None:
        # Et voilà le piège complet : 31/31 en permanence, sur un seul avis.
        votes = vote_all(self._clones(31), RISING)
        assert decide(votes, threshold=28).decision is Vote.UP

    def test_opposed_families_are_negatively_correlated(self) -> None:
        members = [
            Member("elan", momentum(30, 0.002)),
            Member("retour", mean_reversion(30, 0.002)),
        ]
        history = vote_history(members, SAWTOOTH, step=1)
        correlation = mean_correlation(history)
        assert correlation is not None and correlation < 0

    def test_a_varied_ensemble_is_worth_more_than_one_vote(self) -> None:
        history = vote_history(build_ensemble(31), SAWTOOTH, step=3)
        effective = effective_members(history)
        assert effective is not None and effective > 1.5

    def test_never_claims_more_independent_votes_than_members(self) -> None:
        history = vote_history(build_ensemble(8), SAWTOOTH, step=3)
        effective = effective_members(history)
        assert effective is not None and effective <= 8.0


class TestReport:
    def test_names_a_clone_ensemble_for_what_it_is(self) -> None:
        clones = [Member(f"c{i}", momentum(30, 0.005)) for i in range(31)]
        report = analyse(clones, SAWTOOTH, threshold=28, step=3)
        assert "un seul modèle répété" in report.verdict

    def test_counts_how_often_the_threshold_is_actually_reached(self) -> None:
        report = analyse(build_ensemble(31), RISING, threshold=28, step=5)
        assert report.observations > 0
        assert 0.0 <= report.decision_rate <= 1.0
        assert report.decisions + report.abstentions == report.observations

    def test_an_unmeasurable_ensemble_says_so_rather_than_inventing(self) -> None:
        # Tous les membres muets : aucune corrélation n'est définie.
        silent = [Member(f"muet-{i}", momentum(500, 0.005)) for i in range(5)]
        report = analyse(silent, FLAT, threshold=4, step=10)
        assert report.effective_members is None
        assert "non mesurable" in report.verdict
