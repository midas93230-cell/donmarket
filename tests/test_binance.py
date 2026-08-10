"""Tests de l'adaptateur Binance Prediction Trading.

Trois d'entre eux ne sont pas des tests unitaires ordinaires mais des ANCRAGES
DE MESURE : ils figent ce qui a été vérifié en direct contre `api.binance.com`
le 2026-08-09, pour que le jour où Binance change, l'échec pointe le fait exact
qui a bougé.

  - `test_les_crochets_ne_sont_jamais_encodes` garde la seule propriété qui
    sépare une signature valide d'un `-1022` sur `batch-cancel`.
  - `test_le_carnet_sort_meilleur_prix_en_premier` fige l'ordre inverse de
    celui de Polymarket.
  - `test_une_ecriture_ne_reessaie_jamais` garde le fait qu'un ordre coupé en
    chemin n'est pas rejoué.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx
import pytest

from donmarket.binance.api import BinancePredictionClient, Credentials
from donmarket.binance.model import (
    BinanceApiError,
    BinanceSchemaError,
    PredictionBook,
    PredictionLevel,
    extract_rows,
    parse_book,
    parse_market,
)
from donmarket.binance.signing import (
    SigningError,
    canonical_query,
    redact,
    sign,
    signed_query,
)
from donmarket.binance.trade import (
    LIMIT,
    MARKET,
    PredictionOrder,
    PredictionTrader,
    parse_quote,
)
from donmarket.execute.limits import ExecutionLimits

FAKE = Credentials(api_key="cle_publique_de_test", api_secret="secret_de_test")

# Forme EXACTE documentée par Binance pour le flux carnet : paliers en paires
# de chaînes, asks croissants, bids décroissants.
BOOK_PAYLOAD = {
    "msgType": "orderbook",
    "marketId": 8859231,
    "updateTimestampMs": 1717420800123,
    "asks": [["0.32", "500"], ["0.33", "1200"], ["0.34", "300"]],
    "bids": [["0.31", "800"], ["0.30", "2000"], ["0.28", "1500"]],
}


# --- Signature -------------------------------------------------------------


def test_la_signature_est_un_hmac_sha256_de_la_chaine_exacte() -> None:
    query = canonical_query({"marketId": 42, "side": "BUY"})
    assert query == "marketId=42&side=BUY"
    attendu = hmac.new(b"secret_de_test", query.encode(), hashlib.sha256).hexdigest()
    assert sign(query, "secret_de_test") == attendu


def test_les_crochets_ne_sont_jamais_encodes() -> None:
    """ANCRAGE. Le piège n°1 documenté par Binance pour `batch-cancel`.

    `urlencode` produirait `cancelInfoList%5B0%5D.orderId`, que le serveur ne
    signe pas de la même façon → `-1022` sur une signature pourtant juste.
    """
    query = canonical_query({"cancelInfoList[0].orderId": "abc123"})
    assert query == "cancelInfoList[0].orderId=abc123"
    assert "%5B" not in query and "%5D" not in query


def test_httpx_transmet_les_crochets_intacts() -> None:
    """ANCRAGE. Binance conseille `http.client` ; on vérifie qu'httpx suffit.

    Si ce test tombe après une montée de version d'httpx, la signature de
    `batch-cancel` est cassée en production — pas seulement en test.
    """
    url = httpx.URL("https://api.binance.com").copy_with(
        raw_path=b"/sapi/v1/x?cancelInfoList[0].orderId=7&signature=ab"
    )
    assert b"cancelInfoList[0].orderId=7" in httpx.Request("POST", url).url.raw_path


def test_une_valeur_est_encodee_mais_pas_la_cle() -> None:
    """L'asymétrie est le cœur du module : une valeur peut casser la chaîne."""
    query = canonical_query({"keyword": "trump & biden"})
    assert query == "keyword=trump%20%26%20biden"


def test_un_booleen_part_en_minuscules_json() -> None:
    """`str(True)` enverrait « True », que Binance ne reconnaît pas."""
    assert canonical_query({"flag": True}) == "flag=true"


def test_un_flottant_ne_part_jamais_en_notation_exponentielle() -> None:
    assert canonical_query({"price": 0.00001}) == "price=0.00001"


def test_un_parametre_absent_est_omis_pas_envoye_vide() -> None:
    assert canonical_query({"a": 1, "b": None}) == "a=1"


def test_un_nom_de_parametre_exotique_leve() -> None:
    with pytest.raises(SigningError):
        canonical_query({"mauvaise clé": 1})


def test_la_signature_est_ajoutee_en_dernier_et_ne_se_signe_pas_elle_meme() -> None:
    query = signed_query({"a": 1}, secret="s3cr3t_long", timestamp_ms=1700000000000)
    corps, _, signature = query.rpartition("&signature=")
    assert corps == "a=1&timestamp=1700000000000"
    assert sign(corps, "s3cr3t_long") == signature


def test_le_tri_alphabetique_est_optionnel_et_sert_au_websocket() -> None:
    params = {"topic": "t", "recvWindow": 30000, "random": "r"}
    assert canonical_query(params, sort=True) == "random=r&recvWindow=30000&topic=t"


def test_une_cle_ne_survit_jamais_a_la_journalisation() -> None:
    texte = "échec avec X-MBX-APIKEY: " + "A" * 40 + " et secret_de_test"
    nettoye = redact(texte, "secret_de_test")
    assert "secret_de_test" not in nettoye
    assert "A" * 40 not in nettoye


# --- Carnet ----------------------------------------------------------------


def test_le_carnet_sort_meilleur_prix_en_premier() -> None:
    """ANCRAGE. Inverse de Polymarket (meilleur en DERNIER)."""
    book = parse_book(BOOK_PAYLOAD)
    assert book.best_bid == PredictionLevel(0.31, 800.0)
    assert book.best_ask == PredictionLevel(0.32, 500.0)
    assert book.spread == pytest.approx(0.01)
    assert book.mid == pytest.approx(0.315)


def test_un_carnet_construit_a_la_main_est_retrie() -> None:
    """L'invariant vit dans `__post_init__`, pas dans le parseur.

    Sans ça, un carnet relu d'un cache ou bâti par un test présenterait le
    PIRE prix comme le meilleur, sans lever la moindre erreur.
    """
    book = PredictionBook(
        market_id=1,
        bids=(PredictionLevel(0.10, 5.0), PredictionLevel(0.40, 5.0)),
        asks=(PredictionLevel(0.90, 5.0), PredictionLevel(0.50, 5.0)),
    )
    assert book.best_bid.price == 0.40
    assert book.best_ask.price == 0.50


def test_un_palier_illisible_leve_au_lieu_de_disparaitre() -> None:
    """Le parseur Polymarket rendrait ici un carnet VIDE, sans une erreur."""
    with pytest.raises(BinanceSchemaError):
        parse_book({"marketId": 1, "bids": [{"prix": "0.3"}], "asks": []})


def test_une_taille_nulle_est_une_anomalie_pas_une_suppression() -> None:
    """Sémantique OPPOSÉE au WebSocket Polymarket, où size=0 supprime un palier.

    La doc Binance garantit `size > 0` ; en accepter un vaudrait importer une
    convention étrangère et fausser toute mesure de profondeur.
    """
    with pytest.raises(BinanceSchemaError):
        parse_book({"marketId": 1, "bids": [["0.31", "0"]], "asks": []})


def test_un_prix_hors_intervalle_leve() -> None:
    with pytest.raises(BinanceSchemaError):
        parse_book({"marketId": 1, "bids": [["1.4", "10"]], "asks": []})


def test_un_msgtype_inattendu_leve() -> None:
    with pytest.raises(BinanceSchemaError):
        parse_book({"msgType": "trade", "marketId": 1, "bids": [], "asks": []})


def test_lenveloppe_sapi_est_deballee() -> None:
    book = parse_book({"code": "000000", "data": BOOK_PAYLOAD})
    assert book.market_id == 8859231
    assert book.updated_ms == 1717420800123


def test_la_convention_de_branche_reste_declaree_inconnue() -> None:
    """La doc REST (un carnet par jeton) et le flux (un par marché) divergent.

    Tant que ce n'est pas tranché en direct, rien en aval n'a le droit de
    sommer deux branches en croyant lire un jeu complet.
    """
    assert parse_book(BOOK_PAYLOAD).side_convention == "inconnue"


# --- Marché et enveloppes --------------------------------------------------


def test_un_marche_sans_identifiant_leve() -> None:
    with pytest.raises(BinanceSchemaError):
        parse_market({"title": "Un titre sans identifiant"})


def test_un_champ_absent_vaut_none_jamais_zero() -> None:
    """Un 0 inventé se propage en rendement et devient un chiffre faux."""
    marche = parse_market({"marketId": 7})
    assert marche.volume_usdt is None
    assert marche.fee_rate_bps is None
    assert "titre" in marche.unread_fields


def test_une_reponse_illisible_leve_au_lieu_de_rendre_une_liste_vide() -> None:
    """« Aucun marché » et « je n'ai pas su lire » ne doivent pas se confondre."""
    with pytest.raises(BinanceSchemaError):
        extract_rows({"choucroute": 1}, where="market/list")


# --- Client ----------------------------------------------------------------


def test_sans_cle_le_client_se_declare_illisible(monkeypatch) -> None:
    """MESURÉ : même `category/list` rend -2014 sans en-tête."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    client = BinancePredictionClient()
    assert client.is_readable is False
    assert "BINANCE_API_KEY" in client.missing_credentials


def test_un_code_derreur_binance_devient_un_message_actionnable() -> None:
    """Un HTTP 200 porteur de `code: -2015` est un ÉCHEC, pas un succès."""

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": -2015, "msg": "Invalid API-key"})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            with pytest.raises(BinanceApiError) as exc:
                await client.list_categories()
        assert exc.value.code == -2015
        assert "Prediction Trading" in str(exc.value)

    asyncio.run(scenario())


def test_la_requete_porte_la_cle_et_une_signature() -> None:
    vues: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            vues.append(request)
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.list_categories()

    asyncio.run(scenario())
    requete = vues[0]
    assert requete.headers["X-MBX-APIKEY"] == FAKE.api_key
    assert b"signature=" in requete.url.raw_path
    assert b"recvWindow=" in requete.url.raw_path
    assert b"/sapi/v1/w3w/wallet/prediction/category/list" in requete.url.raw_path


def test_un_secret_ne_part_jamais_dans_lurl() -> None:
    vues: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            vues.append(request)
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.list_categories()

    asyncio.run(scenario())
    assert FAKE.api_secret.encode() not in vues[0].url.raw_path


def test_une_ecriture_ne_reessaie_jamais() -> None:
    """ANCRAGE DE SÉCURITÉ. Un ordre rejoué peut être passé deux fois.

    Une requête perdue à l'aller est indiscernable d'une requête reçue dont la
    réponse s'est perdue : le seul choix sûr est de ne pas rejouer.
    """
    appels: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            appels.append(request.method)
            return httpx.Response(503, json={"msg": "indisponible"})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            with pytest.raises(BinanceApiError):
                await client.post("/trade/place-order-bundle", {"quoteId": "q1"})

    asyncio.run(scenario())
    assert appels == ["POST"], f"écriture rejouée {len(appels)} fois"


# --- Exécution -------------------------------------------------------------


LIMITES = ExecutionLimits(max_total_usd=50.0, max_per_market_usd=25.0, max_orders=3)


def test_un_prix_hors_probabilite_est_refuse_a_la_construction() -> None:
    with pytest.raises(ValueError):
        PredictionOrder(market_id=1, token_id="t", price=1.5, size=10)


def test_un_devis_sans_quoteid_leve() -> None:
    """Rendre un devis vide ferait échouer l'ordre loin d'ici, sans indice."""
    with pytest.raises(BinanceSchemaError):
        parse_quote({"data": {"price": "0.32"}})


def test_un_devis_est_lu_sous_son_enveloppe() -> None:
    devis = parse_quote({"data": {"quoteId": "Q-1", "price": "0.32", "fee": "0.01"}})
    assert devis.quote_id == "Q-1"
    assert devis.price == pytest.approx(0.32)


def test_desarme_le_devis_est_demande_mais_aucun_ordre_ne_part() -> None:
    """Le mode désarmé parcourt le MÊME chemin et s'arrête avant l'engagement."""
    chemins: list[bytes] = []

    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            chemins.append(request.url.raw_path)
            return httpx.Response(200, json={"data": {"quoteId": "Q-1"}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=False)
            return await trader.run(
                [PredictionOrder(market_id=1, token_id="t", price=0.3, size=10)]
            )

    resultat = asyncio.run(scenario())
    assert resultat.armed is False
    assert len(resultat.quotes) == 1
    assert resultat.placed == ()
    assert all(b"place-order-bundle" not in chemin for chemin in chemins)


def test_place_leve_si_le_trader_est_desarme() -> None:
    """Défaut de programmation, pas situation à rattraper silencieusement."""

    async def scenario():
        async with BinancePredictionClient(credentials=FAKE) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=False)
            with pytest.raises(RuntimeError):
                await trader.place(
                    PredictionOrder(market_id=1, token_id="t", price=0.3, size=10),
                    parse_quote({"quoteId": "Q-1"}),
                )

    asyncio.run(scenario())


def test_les_plafonds_refusent_avec_un_motif_avant_tout_appel_reseau() -> None:
    appels: list[bytes] = []

    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            appels.append(request.url.raw_path)
            return httpx.Response(200, json={"data": {"quoteId": "Q-1"}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=False)
            # 0,9 × 100 = 90 USDT, très au-dessus des 25 USDT par marché.
            return await trader.run(
                [PredictionOrder(market_id=1, token_id="t", price=0.9, size=100)]
            )

    resultat = asyncio.run(scenario())
    assert len(resultat.refused) == 1
    assert "plafond" in resultat.refused[0][1]
    assert appels == [], "un ordre refusé ne doit générer aucun appel réseau"


def test_un_ordre_marche_trop_petit_est_signale_pas_envoye() -> None:
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("aucun appel ne devrait partir")

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=False)
            return await trader.run(
                [
                    PredictionOrder(
                        market_id=1,
                        token_id="t",
                        order_type=MARKET,
                        price=0.1,
                        size=1,
                    )
                ]
            )

    resultat = asyncio.run(scenario())
    assert len(resultat.failures) == 1
    assert "minimum documenté" in resultat.failures[0][1]


def test_lannulation_construit_des_cles_indexees_non_encodees() -> None:
    """Le piège des crochets, vu de bout en bout plutôt qu'en unité."""
    chemins: list[bytes] = []

    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            chemins.append(request.url.raw_path)
            return httpx.Response(200, json={"data": {}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=True)
            await trader.batch_cancel(["A1", "B2"])

    asyncio.run(scenario())
    assert b"cancelInfoList[0].orderId=A1" in chemins[0]
    assert b"cancelInfoList[1].orderId=B2" in chemins[0]
    assert b"%5B" not in chemins[0]


def test_le_compte_rendu_dit_toujours_sil_etait_arme() -> None:
    """Un rapport muet sur ce point finit par être mal lu."""

    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"quoteId": "Q-1"}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client, limits=LIMITES, armed=False)
            return await trader.run(
                [PredictionOrder(market_id=1, token_id="t", price=0.3, size=10)]
            )

    assert "DÉSARMÉ" in asyncio.run(scenario()).summary


def test_le_type_dordre_par_defaut_est_limit() -> None:
    """Un MARKET par défaut paierait l'écart à chaque ordre, en silence."""
    ordre = PredictionOrder(market_id=1, token_id="t", price=0.3, size=10)
    assert ordre.order_type == LIMIT
