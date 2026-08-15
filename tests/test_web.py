"""Tests du serveur local — la couche qui AFFICHE, sans jamais rien décider.

Trois choses doivent être vraies, et chacune correspond à un dégât possible :

1. Le total affiché ne doit compter que les positions tenables EN MÊME TEMPS.
   C'est le bug réparé le 2026-07-28 côté CLI (6 candidats, 200 $ engagés pour
   100 $ de capital, +16,84 $/jour annoncés) ; le remettre dans une page web
   serait le remettre sous une forme plus convaincante encore.
2. Deux scans ne doivent jamais tourner en parallèle. Un scan lit 2 100 marchés
   et ~950 carnets ; un utilisateur qui clique trois fois lancerait trois
   balayages complets sur des API publiques qui ne nous doivent rien.
3. Le serveur n'écoute que sur la boucle locale. Ce dépôt lit des marchés
   d'argent réel : rien ici n'a à être joignable depuis le réseau.
"""

from __future__ import annotations

from donmarket.web.server import is_local_write_allowed as _write_allowed

PORT = 8787


def test_un_site_tiers_ne_peut_pas_declencher_une_ecriture():
    """Le scénario réel : un onglet quelconque poste vers le loopback.

    La réponse lui serait refusée par la politique d'origine, mais l'action,
    elle, partirait — c'est le propre du CSRF.
    """
    assert not _write_allowed(
        {"Sec-Fetch-Site": "cross-site", "Origin": "https://site-quelconque.example"},
        port=PORT,
    )


def test_la_page_locale_est_acceptee():
    assert _write_allowed(
        {"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{PORT}"},
        port=PORT,
    )


def test_un_autre_service_local_est_refuse():
    """La boucle locale n'est PAS une origine unique : un autre port est un tiers."""
    assert not _write_allowed({"Origin": f"http://127.0.0.1:{PORT + 1}"}, port=PORT)


def test_curl_sans_en_tete_reste_accepte():
    """Refuser ici casserait tout script local sans rien protéger de plus."""
    assert _write_allowed({}, port=PORT)


def test_un_origin_falsifie_vers_un_autre_hote_est_refuse():
    assert not _write_allowed({"Origin": "http://evil.example"}, port=PORT)


def test_sec_fetch_site_prime_meme_sans_origin():
    assert not _write_allowed({"Sec-Fetch-Site": "same-site"}, port=PORT)

import json

from donmarket.analysis.opportunities import Mode
from donmarket.analysis.rewards import RewardCandidate
from donmarket.scan.rewards_scan import RewardScanResult
from donmarket.web import server
from donmarket.web.payload import scan_payload
from donmarket.web.state import ScanState, Status


def _candidate(
    *,
    condition_id: str = "0xabc",
    engaged: float = 50.0,
    gross: float = 10.0,
    drift: float = 2.0,
) -> RewardCandidate:
    return RewardCandidate(
        condition_id=condition_id,
        question="Will X happen?",
        slug="will-x-happen",
        daily_pool=150.0,
        competing_q=250.0,
        own_q=25.0,
        engaged_usd=engaged,
        gross_yield=gross,
        drift=drift,
        oscillation=12.0,
        hours_left=96.0,
    )


def _result(*candidates: RewardCandidate, bankroll: float = 100.0) -> RewardScanResult:
    return RewardScanResult(
        mode=Mode.SERIEUX,
        bankroll=bankroll,
        markets_seen=2099,
        rewarded=623,
        alive=552,
        affordable=453,
        books_fetched=948,
        histories_fetched=60,
        duration_seconds=61.0,
        candidates=candidates,
    )


class TestPayload:
    def test_totals_only_what_fits_in_the_bankroll_at_once(self) -> None:
        # Quatre tickets de 50 $ tiennent chacun dans 100 $, mais pas ensemble.
        four = [_candidate(condition_id=f"0x{i}", engaged=50.0) for i in range(4)]
        payload = scan_payload(_result(*four, bankroll=100.0))

        assert payload["portfolio"]["held_count"] == 2
        assert payload["portfolio"]["engaged_usd"] == 100.0
        assert payload["portfolio"]["engaged_usd"] <= payload["bankroll"]

    def test_marks_which_candidates_are_actually_held(self) -> None:
        rich = _candidate(condition_id="0xrich", engaged=80.0, gross=40.0)
        poor = _candidate(condition_id="0xpoor", engaged=80.0, gross=5.0)
        payload = scan_payload(_result(rich, poor, bankroll=100.0))

        by_id = {row["condition_id"]: row for row in payload["candidates"]}
        assert by_id["0xrich"]["held"] is True
        assert by_id["0xpoor"]["held"] is False

    def test_exposes_the_funnel_not_just_the_winners(self) -> None:
        # Quand rien ne sort, l'entonnoir dit OÙ l'univers s'est vidé.
        payload = scan_payload(_result(bankroll=100.0))

        assert payload["funnel"] == {
            "markets_seen": 2099,
            "rewarded": 623,
            "alive": 552,
            "affordable": 453,
        }
        assert payload["portfolio"]["daily_usd"] == 0.0

    def test_is_json_serialisable(self) -> None:
        payload = scan_payload(_result(_candidate()))
        assert json.loads(json.dumps(payload))["found"] == 1


class TestScanState:
    def test_starts_idle(self) -> None:
        assert ScanState().snapshot().status is Status.IDLE

    def test_refuses_a_second_scan_while_one_runs(self) -> None:
        state = ScanState()
        assert state.begin() is True
        assert state.begin() is False

    def test_accepts_a_new_scan_once_the_previous_finished(self) -> None:
        state = ScanState()
        state.begin()
        state.succeed(_result(_candidate()))

        snapshot = state.snapshot()
        assert snapshot.status is Status.DONE
        assert snapshot.result is not None
        assert state.begin() is True

    def test_keeps_the_last_result_visible_after_a_failure(self) -> None:
        # Une panne réseau ne doit pas effacer la dernière mesure réussie :
        # c'est encore la meilleure information disponible.
        state = ScanState()
        state.begin()
        state.succeed(_result(_candidate()))
        state.begin()
        state.fail("connexion refusée")

        snapshot = state.snapshot()
        assert snapshot.status is Status.ERROR
        assert snapshot.error == "connexion refusée"
        assert snapshot.result is not None


class TestRouting:
    def test_serves_the_page_at_the_root(self) -> None:
        response = server.handle_request("GET", "/", state=ScanState(), launch=lambda: True)
        assert response.status == 200
        assert "text/html" in response.content_type

    def test_state_endpoint_reports_idle_before_any_scan(self) -> None:
        response = server.handle_request(
            "GET", "/api/state", state=ScanState(), launch=lambda: True
        )
        body = json.loads(response.body)
        assert body["status"] == "idle"
        assert body["scan"] is None

    def test_state_endpoint_returns_the_last_scan(self) -> None:
        state = ScanState()
        state.begin()
        state.succeed(_result(_candidate()))

        response = server.handle_request(
            "GET", "/api/state", state=state, launch=lambda: True
        )
        body = json.loads(response.body)
        assert body["scan"]["found"] == 1

    def test_scan_endpoint_says_when_it_declined_to_start(self) -> None:
        response = server.handle_request(
            "POST", "/api/scan", state=ScanState(), launch=lambda: False
        )
        assert json.loads(response.body)["started"] is False

    def test_unknown_path_is_a_clean_404(self) -> None:
        response = server.handle_request(
            "GET", "/admin", state=ScanState(), launch=lambda: True
        )
        assert response.status == 404

    def test_scan_cannot_be_triggered_by_a_get(self) -> None:
        # Un GET est déclenchable par une simple balise image ; lancer un
        # balayage complet ne doit pas être aussi facile.
        response = server.handle_request(
            "GET", "/api/scan", state=ScanState(), launch=lambda: True
        )
        assert response.status == 405


class TestBinding:
    def test_listens_on_loopback_only(self) -> None:
        assert server.HOST == "127.0.0.1"
