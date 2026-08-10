"""Tests de l'adaptateur Predict.fun.

Deux d'entre eux ne sont pas des tests unitaires ordinaires mais des ANCRAGES
DE MESURE : ils figent ce qui a été observé en direct le 2026-08-09, pour que
le jour où Predict.fun change, l'échec pointe le fait exact qui a bougé plutôt
qu'un chiffre lointain.

  - `test_bareme_de_frais_publie` rejoue les 21 lignes du barème de la doc.
  - `test_le_jeu_complet_ne_descend_jamais_sous_un` vérifie l'identité qui rend
    l'arbitrage impossible.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from donmarket.model import parse_gamma_market
from donmarket.predictfun.api import PredictClient
from donmarket.predictfun.crossvenue import (
    describe,
    fetch_polymarket_by_condition,
    pair_markets,
)
from donmarket.predictfun.model import (
    PredictBook,
    PredictLevel,
    PredictSchemaError,
    parse_book,
    parse_market,
)
from donmarket.predictfun.rebates import (
    breakeven_adverse_move,
    looks_rebate_eligible,
    maker_rebate_per_share,
    program_is_running,
    rebate_yield_on_filled_notional,
    taker_fee,
    taker_fee_per_share,
)
from donmarket.predictfun.scan import Rejection, evaluate_market

# --- Fixtures synthétiques, calquées sur la forme mesurée -----------------

BOOK_PAYLOAD = {
    "success": True,
    "data": {
        "marketId": 1049,
        # Ordre natif mesuré : bids DÉCROISSANTS, asks CROISSANTS.
        "bids": [[0.88, 688183.2794], [0.09, 653410.53], [0.02, 185.34]],
        "asks": [[0.94, 35.2185], [0.99, 617.18]],
        "updateTimestampMs": 1786240863644,
    },
}

MARKET_PAYLOAD = {
    "id": 1049,
    "conditionId": "0xabc",
    "question": "Question ?",
    "title": "Un titre ",
    "categorySlug": "btc-usd-up-down-2025-12-07-00-00",
    "tradingStatus": "OPEN",
    "status": "ACTIVE",
    "feeRateBps": 200,
    "decimalPrecision": 2,
    "shareThreshold": 100,
    "spreadThreshold": 0.06,
    "marketVariant": "DEFAULT",
    "isNegRisk": False,
    "isBoosted": False,
    "polymarketConditionIds": ["0xdef"],
    "createdAt": "2025-12-31T08:54:08.117Z",
    "rewards": {"current": None, "schedule": []},
    "outcomes": [
        {
            "name": "Yes",
            "indexSet": 1,
            "onChainId": "1",
            "status": None,
            "bestBid": {"price": 0.44, "size": 10.0},
            "bestAsk": {"price": 0.46, "size": 5.0},
        },
        {
            "name": "No",
            "indexSet": 2,
            "onChainId": "2",
            "status": None,
            "bestBid": {"price": 0.54, "size": 5.0},
            "bestAsk": {"price": 0.56, "size": 10.0},
        },
    ],
}


def make_book(bids: list[list[float]], asks: list[list[float]]) -> PredictBook:
    return parse_book({"data": {"marketId": 1, "bids": bids, "asks": asks}})


# --- Le carnet : forme et ordre -------------------------------------------


def test_les_paliers_sont_des_paires_pas_des_objets() -> None:
    """Predict.fun envoie `[prix, taille]`. Le parseur Polymarket rendrait un carnet vide."""
    book = parse_book(BOOK_PAYLOAD)

    assert book.market_id == 1049
    assert len(book.yes_bids) == 3
    assert book.yes_bids[0] == PredictLevel(0.88, 688183.2794)
    assert book.updated_ms == 1786240863644


def test_le_meilleur_prix_est_en_premier_apres_normalisation() -> None:
    """Contraire de Polymarket, où le meilleur bid est en DERNIER."""
    book = parse_book(BOOK_PAYLOAD)

    assert book.best_yes_bid is not None and book.best_yes_bid.price == 0.88
    assert book.best_yes_ask is not None and book.best_yes_ask.price == 0.94
    assert [lv.price for lv in book.yes_bids] == [0.88, 0.09, 0.02]
    assert [lv.price for lv in book.yes_asks] == [0.94, 0.99]


def test_un_carnet_construit_a_la_main_est_retrie() -> None:
    """L'invariant vit dans `__post_init__`, pas dans le parseur.

    Un carnet relu d'un cache ou fabriqué par un test n'offre aucune garantie
    d'ordre ; s'y fier ferait passer le PIRE prix pour le meilleur, en silence.
    """
    book = PredictBook(
        market_id=1,
        yes_bids=(PredictLevel(0.10, 1.0), PredictLevel(0.40, 1.0)),
        yes_asks=(PredictLevel(0.90, 1.0), PredictLevel(0.50, 1.0)),
    )

    assert book.best_yes_bid == PredictLevel(0.40, 1.0)
    assert book.best_yes_ask == PredictLevel(0.50, 1.0)


def test_un_palier_illisible_leve_au_lieu_de_disparaitre() -> None:
    """Le silence est le vrai danger : un carnet vide se lit « pas de liquidité »."""
    with pytest.raises(PredictSchemaError, match="palier illisible"):
        parse_book({"data": {"marketId": 1, "bids": ["0.5"], "asks": []}})


def test_une_taille_nulle_nest_pas_un_palier() -> None:
    book = make_book([[0.4, 0.0], [0.3, 5.0]], [])

    assert [lv.price for lv in book.yes_bids] == [0.3]


# --- Le côté No est DÉRIVÉ, pas servi -------------------------------------


def test_le_cote_no_est_le_miroir_exact_du_cote_yes() -> None:
    """Mesuré sur 12 marchés, 0 violation : no_ask = 1 − yes_bid, même taille."""
    book = make_book([[0.44, 10.0]], [[0.46, 5.0]])

    assert book.best_no_ask is not None
    assert book.best_no_ask.price == pytest.approx(0.56)
    assert book.best_no_ask.size == 10.0
    assert book.best_no_bid is not None
    assert book.best_no_bid.price == pytest.approx(0.54)
    assert book.best_no_bid.size == 5.0


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(0.44, 0.46), (0.01, 0.99), (0.50, 0.50), (0.88, 0.94)],
)
def test_le_jeu_complet_ne_descend_jamais_sous_un(bid: float, ask: float) -> None:
    """ANCRAGE : ask_yes + ask_no = 1 + écart ≥ 1, donc l'arbitrage n'a pas de zéro.

    Sur Polymarket c'était une observation empirique (0 cas sur 1 937 marchés) ;
    ici c'est une identité algébrique, parce qu'il n'existe qu'un seul carnet.
    """
    book = make_book([[bid, 10.0]], [[ask, 10.0]])

    total = book.full_set_ask_sum
    assert total is not None
    assert total >= 1.0
    assert total == pytest.approx(1.0 + (ask - bid))


# --- La formule de récompense ---------------------------------------------

# Barème publié : docs.predict.fun « Predict Fees and Limits », base 2 %,
# 100 parts par ligne. (prix, frais en USDT)
BAREME_PUBLIE = [
    (0.01, 0.02), (0.05, 0.10), (0.10, 0.20), (0.15, 0.30), (0.20, 0.40),
    (0.25, 0.50), (0.30, 0.60), (0.35, 0.70), (0.40, 0.80), (0.45, 0.90),
    (0.50, 1.00), (0.55, 0.90), (0.60, 0.80), (0.65, 0.70), (0.70, 0.60),
    (0.75, 0.50), (0.80, 0.40), (0.85, 0.30), (0.90, 0.20), (0.95, 0.10),
    (0.99, 0.02),
]


@pytest.mark.parametrize(("price", "expected"), BAREME_PUBLIE)
def test_bareme_de_frais_publie(price: float, expected: float) -> None:
    """ANCRAGE : la formule reproduit les 21 lignes du barème officiel.

    frais = taux × min(p, 1−p) × parts, avec taux = 2 % et parts = 100.
    """
    assert taker_fee(price, 100, fee_rate=0.02) == pytest.approx(expected, abs=1e-9)


def test_les_frais_culminent_au_milieu_et_seffondrent_aux_extremes() -> None:
    """Contre-intuitif : un pari improbable coûte presque rien en frais."""
    assert taker_fee_per_share(0.50) > taker_fee_per_share(0.20)
    assert taker_fee_per_share(0.20) == pytest.approx(taker_fee_per_share(0.80))
    assert taker_fee_per_share(0.99) == pytest.approx(0.0002)


def test_le_rebate_vaut_un_quart_des_frais_du_preneur() -> None:
    """« the maker receives 25% of the taker fee paid on that fill »."""
    for price in (0.05, 0.25, 0.5, 0.75, 0.95):
        assert maker_rebate_per_share(price) == pytest.approx(
            0.25 * taker_fee_per_share(price)
        )


def test_le_rendement_maximal_par_execution_est_un_demi_pourcent() -> None:
    """0,25 × 2 % × 0,5 / 0,5 = 0,5 % du notionnel exécuté, atteint à p = 0,5."""
    assert rebate_yield_on_filled_notional(0.5) == pytest.approx(0.005)
    assert rebate_yield_on_filled_notional(0.9) < rebate_yield_on_filled_notional(0.5)


def test_le_seuil_de_perte_est_un_quart_de_pas_a_cinquante_cents() -> None:
    """Le gain se compare au risque : 0,0025 $/part, soit 0,25 cent de dérive."""
    assert breakeven_adverse_move(0.5) == pytest.approx(0.0025)


def test_le_programme_expire_et_ne_se_reconduit_pas_tout_seul() -> None:
    assert program_is_running(date(2026, 9, 16)) is True
    assert program_is_running(date(2026, 9, 17)) is False


def test_leligibilite_est_une_heuristique_sur_le_slug() -> None:
    assert looks_rebate_eligible("btc-usd-up-down-2025-12-07-00-00") is True
    assert looks_rebate_eligible("whatever", "CRYPTO_UP_DOWN") is True
    assert looks_rebate_eligible("logan-pauls-psa-10-pokmon-illustrator") is False


def test_un_prix_hors_intervalle_leve() -> None:
    with pytest.raises(ValueError, match="hors"):
        taker_fee_per_share(1.5)


# --- Le marché --------------------------------------------------------------


def test_le_marche_est_normalise_avec_ses_seuils() -> None:
    market = parse_market(MARKET_PAYLOAD)

    assert market.market_id == 1049
    assert market.title == "Un titre"  # espace final retiré
    assert market.is_open is True
    assert market.fee_rate == pytest.approx(0.02)
    assert market.tick_size == pytest.approx(0.01)
    assert market.share_threshold == 100
    # Fraction, PAS un pourcentage : Polymarket exige une division par 100.
    assert market.spread_threshold == pytest.approx(0.06)
    assert market.polymarket_condition_ids == ("0xdef",)
    assert market.yes_outcome is not None and market.yes_outcome.name == "Yes"


def test_un_marche_sans_identite_leve() -> None:
    with pytest.raises(PredictSchemaError, match="id entier"):
        parse_market({"conditionId": "0x1"})


# --- Le scanner -------------------------------------------------------------


def test_un_marche_cotable_donne_un_candidat_chiffre() -> None:
    market = parse_market(MARKET_PAYLOAD)
    book = make_book([[0.44, 200.0]], [[0.46, 200.0]])

    candidate = evaluate_market(market, book)

    assert not isinstance(candidate, Rejection)
    assert candidate.reference_price == pytest.approx(0.45)
    assert candidate.rebate_per_share == pytest.approx(0.25 * 0.02 * 0.45)
    # Ticket = max(1 USDT, shareThreshold × prix) = max(1, 100 × 0,45).
    assert candidate.entry_ticket_usd == pytest.approx(45.0)
    assert candidate.within_spread_threshold is True
    assert candidate.breakeven_ticks == pytest.approx(0.225)


def test_un_carnet_unilateral_est_rejete_avec_un_motif() -> None:
    market = parse_market(MARKET_PAYLOAD)
    book = make_book([[0.44, 10.0]], [])

    verdict = evaluate_market(market, book)

    assert isinstance(verdict, Rejection)
    assert "unilatéral" in verdict.reason


def test_un_carnet_vide_est_rejete() -> None:
    market = parse_market(MARKET_PAYLOAD)

    verdict = evaluate_market(market, make_book([], []))

    assert isinstance(verdict, Rejection)
    assert verdict.reason == "carnet vide"


# --- Le client : les défauts d'API mesurés --------------------------------


def _page(cursor: str, rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"cursor": cursor, "data": rows})


def test_un_curseur_qui_pietine_arrete_la_collecte() -> None:
    """MESURÉ : le curseur ne bouge jamais. Sans garde, la boucle ne finit pas.

    Le test rend le serveur infiniment répétitif ; seule la détection de
    piétinement peut terminer.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _page("MjkwODQ=", [MARKET_PAYLOAD])

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with PredictClient(network="testnet", transport=transport) as client:
            page = await client.fetch_markets()

        assert len(page.markets) == 1
        assert page.rows_received == 2
        assert page.duplicate_rows == 1
        assert page.pagination_stalled is True
        assert calls["n"] == 2
        assert any("pagination bloquée" in note for note in page.complaints())

    asyncio.run(run())


def test_un_filtre_ignore_par_le_serveur_est_signale() -> None:
    """MESURÉ : `?tradingStatus=OPEN` renvoie quand même des marchés CLOSED."""
    closed = {**MARKET_PAYLOAD, "id": 7, "tradingStatus": "CLOSED"}

    def handler(request: httpx.Request) -> httpx.Response:
        return _page("", [MARKET_PAYLOAD, closed])

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with PredictClient(network="testnet", transport=transport) as client:
            page = await client.fetch_markets(trading_status="OPEN")

        assert page.filter_ignored is True
        assert {m.market_id for m in page.markets} == {1049, 7}
        assert any("ignoré le filtre" in note for note in page.complaints())

    asyncio.run(run())


def test_un_carnet_absent_rend_none_au_lieu_de_lever() -> None:
    """MESURÉ : 404 sur un marché clos. Ce n'est pas une panne."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with PredictClient(network="testnet", transport=transport) as client:
            assert await client.fetch_book(487) is None

    asyncio.run(run())


def test_mainnet_sans_cle_nest_pas_lisible() -> None:
    """MESURÉ : api.predict.fun répond 401 sans en-tête x-api-key."""
    assert PredictClient(network="mainnet", api_key=None).is_readable is False
    assert PredictClient(network="testnet", api_key=None).is_readable is True


def test_un_reseau_inconnu_est_refuse() -> None:
    with pytest.raises(ValueError, match="réseau inconnu"):
        PredictClient(network="polymarket")


# --- Le pont vers Polymarket ----------------------------------------------


def _gamma_row(condition_id: str, *, bid: float = 0.40, ask: float = 0.42) -> dict:
    return {
        "conditionId": condition_id,
        "question": "La même question ?",
        "slug": "la-meme-question",
        "bestBid": bid,
        "bestAsk": ask,
        "closed": False,
        "active": True,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["1", "2"]',
        "outcomePrices": '["0.41", "0.59"]',
    }


def test_une_reponse_non_filtree_nest_jamais_prise_pour_un_jumeau() -> None:
    """MESURÉ : un paramètre mal nommé rend HTTP 200 et 20 marchés quelconques.

    C'est le piège central du pont : sans revérifier le `conditionId` rendu, un
    marché sans aucun rapport serait présenté comme le jumeau, avec un écart de
    prix parfaitement crédible et entièrement faux.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Le serveur ignore le filtre et renvoie autre chose que ce qu'on demande.
        return httpx.Response(200, json=[_gamma_row("0xAUTRE_MARCHE")])

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://gamma-api.polymarket.com", transport=transport
        ) as http:
            found = await fetch_polymarket_by_condition(["0xdef"], client=http)

        assert found == {}

    asyncio.run(run())


def test_un_jumeau_authentique_est_accepte_et_compare() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_gamma_row("0xdef", bid=0.40, ask=0.42)])

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            base_url="https://gamma-api.polymarket.com", transport=transport
        ) as http:
            found = await fetch_polymarket_by_condition(["0xdef"], client=http)

        market = parse_market(MARKET_PAYLOAD)
        quotes = pair_markets([market], found, {1049: 0.45})

        assert len(quotes) == 1
        quote = quotes[0]
        assert quote.polarity_checked is True
        assert quote.polymarket_mid == pytest.approx(0.41)
        # Predict.fun cote 4 points au-dessus de Polymarket.
        assert quote.divergence == pytest.approx(0.04)

    asyncio.run(run())


def test_une_polarite_non_confirmee_refuse_de_chiffrer_un_ecart() -> None:
    """Un écart calculé sur des branches inversées est un écart inventé."""
    row = _gamma_row("0xdef")
    row["outcomes"] = '["Up", "Down"]'  # l'autre place ne nomme pas pareil
    twin = parse_gamma_market(row)
    assert twin is not None

    quotes = pair_markets([parse_market(MARKET_PAYLOAD)], {"0xdef": twin}, {1049: 0.45})

    assert quotes[0].polarity_checked is False
    assert quotes[0].divergence is None
    assert any("polarité" in reason for reason in quotes[0].blockers)


def test_lecart_nest_jamais_presente_comme_captable() -> None:
    """L'exécution Predict.fun n'est pas branchée : ça doit se lire à chaque ligne."""
    twin = parse_gamma_market(_gamma_row("0xdef"))
    assert twin is not None

    quotes = pair_markets([parse_market(MARKET_PAYLOAD)], {"0xdef": twin}, {1049: 0.45})

    assert any("BNB Chain" in reason for reason in quotes[0].blockers)
    assert any("capital requis des deux côtés" in r for r in quotes[0].blockers)


def test_aucun_jumeau_produit_une_explication_pas_un_silence() -> None:
    lines = describe(())

    assert len(lines) == 1
    assert "fictifs" in lines[0]
