"""La persistance des balayages de récompenses, et ce qu'elle doit permettre.

Une base qui n'enregistre que les candidats RETENUS ne peut pas répondre à la
question qui compte — « ce rendement tenait-il ? » — parce qu'un candidat qui
passe sous le seuil disparaît alors des relevés au lieu d'y être noté comme
retombé. Ces tests fixent d'abord ce point.
"""

from __future__ import annotations

import pytest

from donmarket.analysis.opportunities import Mode
from donmarket.analysis.rewards import RewardCandidate
from donmarket.scan.rewards_scan import RewardScanResult
from donmarket.store import db


@pytest.fixture()
def connection(tmp_path):
    with db.connect(tmp_path / "test.db") as conn:
        yield conn


def _candidate(
    condition_id: str = "0xaa",
    *,
    gross: float = 10.0,
    drift: float = 2.0,
    rejected: tuple[str, ...] = (),
) -> RewardCandidate:
    return RewardCandidate(
        condition_id=condition_id,
        question="Le prix dépassera-t-il X ?",
        slug="prix-x",
        daily_pool=100.0,
        competing_q=2000.0,
        own_q=500.0,
        engaged_usd=50.0,
        gross_yield=gross,
        drift=drift,
        oscillation=4.0,
        hours_left=72.0,
        rejected_by=rejected,
        token_ids=("t1", "t2"),
        max_spread=0.03,
    )


def _result(*candidates: RewardCandidate, near: tuple[RewardCandidate, ...] = ()) -> RewardScanResult:
    return RewardScanResult(
        mode=Mode.SERIEUX,
        bankroll=100.0,
        markets_seen=2099,
        rewarded=639,
        alive=571,
        affordable=474,
        books_fetched=948,
        histories_fetched=60,
        duration_seconds=51.2,
        candidates=candidates,
        near_misses=near,
    )


class TestRecordRewardScan:
    def test_writes_scan_and_candidates(self, connection):
        scan_id = db.record_reward_scan(connection, _result(_candidate()))

        assert scan_id > 0
        totals = db.counts(connection)
        assert totals["reward_scans"] == 1
        assert totals["reward_candidates"] == 1

    def test_records_the_funnel_counters(self, connection):
        db.record_reward_scan(connection, _result(_candidate()))

        row = connection.execute("SELECT * FROM reward_scans").fetchone()
        assert row["markets_seen"] == 2099
        assert row["rewarded"] == 639
        assert row["affordable"] == 474
        assert row["found"] == 1
        assert row["mode"] == "serieux"

    def test_keeps_rejected_candidates_with_their_reason(self, connection):
        """Un candidat retombé doit rester lisible, sinon la série ment.

        S'il disparaît de la base au scan où il échoue, l'historique n'affiche
        que ses bons jours et toute lecture de persistance est faussée.
        """
        db.record_reward_scan(
            connection,
            _result(near=(_candidate(rejected=("net < seuil", "carnet trop mince")),)),
        )

        row = connection.execute("SELECT * FROM reward_candidates").fetchone()
        assert row["actionable"] == 0
        assert "net < seuil" in row["rejected_by"]
        assert "carnet trop mince" in row["rejected_by"]

    def test_net_yield_matches_the_dataclass(self, connection):
        db.record_reward_scan(connection, _result(_candidate(gross=10.0, drift=2.0)))

        row = connection.execute("SELECT net_yield FROM reward_candidates").fetchone()
        assert row["net_yield"] == pytest.approx(8.0)

    def test_token_ids_survive_the_round_trip(self, connection):
        """Sans les jetons, un relevé passé ne peut pas être rejoué."""
        db.record_reward_scan(connection, _result(_candidate()))

        row = connection.execute("SELECT token_ids FROM reward_candidates").fetchone()
        assert db.decode_token_ids(row["token_ids"]) == ("t1", "t2")

    def test_empty_scan_is_still_recorded(self, connection):
        """Un balayage qui ne retient rien est une mesure, pas un non-événement."""
        scan_id = db.record_reward_scan(connection, _result())

        assert scan_id > 0
        assert db.counts(connection)["reward_scans"] == 1


class TestCandidateTracks:
    def test_groups_observations_by_market(self, connection):
        db.record_reward_scan(connection, _result(_candidate("0xaa", gross=10.0)))
        db.record_reward_scan(connection, _result(_candidate("0xaa", gross=20.0)))
        db.record_reward_scan(connection, _result(_candidate("0xbb", gross=30.0)))

        tracks = db.candidate_tracks(connection, min_observations=1)

        by_market = {track.condition_id: track for track in tracks}
        assert by_market["0xaa"].observations == 2
        assert by_market["0xbb"].observations == 1

    def test_reports_the_spread_of_the_net_over_time(self, connection):
        """C'est le chiffre de la sixième mesure : l'amplitude, pas la moyenne."""
        for gross in (10.0, 50.0, 30.0):
            db.record_reward_scan(connection, _result(_candidate("0xaa", gross=gross)))

        track = db.candidate_tracks(connection, min_observations=1)[0]

        assert track.net_min == pytest.approx(8.0)
        assert track.net_max == pytest.approx(48.0)
        assert track.net_median == pytest.approx(28.0)
        assert track.net_first == pytest.approx(8.0)
        assert track.net_last == pytest.approx(28.0)

    def test_counts_how_often_it_was_actually_actionable(self, connection):
        db.record_reward_scan(connection, _result(_candidate("0xaa")))
        db.record_reward_scan(
            connection, _result(near=(_candidate("0xaa", rejected=("net < seuil",)),))
        )

        track = db.candidate_tracks(connection, min_observations=1)[0]

        assert track.observations == 2
        assert track.actionable_count == 1

    def test_hides_markets_seen_only_once(self, connection):
        """Un seul relevé ne dit rien sur la persistance : c'est un instantané."""
        db.record_reward_scan(connection, _result(_candidate("0xaa")))

        assert db.candidate_tracks(connection, min_observations=2) == []

    def test_orders_by_observation_count(self, connection):
        db.record_reward_scan(
            connection, _result(_candidate("0xaa"), _candidate("0xbb"))
        )
        db.record_reward_scan(connection, _result(_candidate("0xbb")))

        tracks = db.candidate_tracks(connection, min_observations=1)

        assert [track.condition_id for track in tracks] == ["0xbb", "0xaa"]

    def test_no_scan_no_track(self, connection):
        assert db.candidate_tracks(connection) == []


class TestExistingTablesStillWork:
    def test_counts_covers_every_table(self, connection):
        totals = db.counts(connection)

        assert set(totals) == {
            "markets",
            "outcomes",
            "scans",
            "opportunities",
            "reward_scans",
            "reward_candidates",
        }
