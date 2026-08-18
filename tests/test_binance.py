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
    flatten_market_topics,
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
    to_base_units,
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
            # Le devis exige `walletAddress` depuis la mesure du 2026-08-18 :
            # le chemin désarmé lit donc d'abord le portefeuille.
            if request.url.path.endswith("/wallet/list"):
                return httpx.Response(200, json=WALLET_LIST_REEL)
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


# --- Horloge ---------------------------------------------------------------
#
# ANCRAGE DE MESURE 2026-08-18 : l'horloge de cette machine était en avance de
# ~6 000 ms sur Binance (`w32time` arrêté, dérive libre). Binance refuse toute
# requête signée dépassant 1 000 ms d'AVANCE, quelle que soit `recvWindow` —
# donc AUCUNE route ne répondait, sur une clé pourtant parfaitement valide.


def _reponse_temps(server_time_ms: int) -> httpx.Response:
    return httpx.Response(200, json={"serverTime": server_time_ms})


def test_un_1021_declenche_une_resynchronisation_puis_un_rejeu() -> None:
    """Le client se répare seul au lieu d'épuiser ses essais sur la même erreur."""
    chemins: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            chemins.append(request.url.path)
            if request.url.path.endswith("/api/v3/time"):
                return _reponse_temps(1_000_000_000_000)
            if len([c for c in chemins if "prediction" in c]) == 1:
                return httpx.Response(
                    400,
                    json={"code": -1021, "msg": "Timestamp for this request was ahead"},
                )
            return httpx.Response(200, json={"data": {"used": 3}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            resultat = await client.quota_status()

        assert resultat == {"used": 3}

    asyncio.run(scenario())
    assert any(c.endswith("/api/v3/time") for c in chemins), (
        "un -1021 doit déclencher une lecture de l'heure serveur, "
        f"chemins vus : {chemins}"
    )


def test_lecart_mesure_est_applique_a_lhorodatage_signe() -> None:
    """Ce n'est pas assez de resynchroniser : l'écart doit ENTRER dans la signature.

    Sans cette vérification, le client pourrait mesurer un écart de 6 s,
    l'afficher fidèlement, et continuer à signer avec l'heure locale fausse.
    """
    horodatages: list[int] = []
    heure_serveur = 1_000_000_000_000

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/v3/time"):
                return _reponse_temps(heure_serveur)
            for morceau in request.url.query.decode().split("&"):
                if morceau.startswith("timestamp="):
                    horodatages.append(int(morceau.split("=", 1)[1]))
            if len(horodatages) == 1:
                return httpx.Response(400, json={"code": -1021, "msg": "ahead"})
            return httpx.Response(200, json={"data": {}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.quota_status()

    asyncio.run(scenario())
    assert len(horodatages) == 2, f"attendu 2 requêtes signées, vu {horodatages}"
    # Le second horodatage doit être recalé sur l'heure SERVEUR, pas sur
    # l'horloge locale. Tolérance large : le temps d'aller-retour simulé et la
    # durée du test s'y ajoutent légitimement.
    assert abs(horodatages[1] - heure_serveur) < 5_000, (
        f"le rejeu signe encore l'heure locale ({horodatages[1]}) au lieu de "
        f"l'heure serveur ({heure_serveur})"
    )


def test_un_1021_est_rejoue_meme_en_ecriture() -> None:
    """EXCEPTION MOTIVÉE à `test_une_ecriture_ne_reessaie_jamais`.

    La règle « ne jamais rejouer une écriture » protège du cas où l'ordre a pu
    être reçu et exécuté malgré une réponse perdue. `-1021` ne relève PAS de ce
    cas : c'est un refus rendu par le serveur AVANT tout traitement, donc
    l'ordre n'existe pas. Ne pas rejouer coûterait ici un ordre perdu pour
    rien. La distinction se tient au code d'erreur, pas au verbe HTTP.
    """
    verbes: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/v3/time"):
                return _reponse_temps(1_000_000_000_000)
            verbes.append(request.method)
            if len(verbes) == 1:
                return httpx.Response(400, json={"code": -1021, "msg": "ahead"})
            return httpx.Response(200, json={"data": {"orderId": "o1"}})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            recu = await client.post("/trade/place-order-bundle", {"quoteId": "q1"})
        assert recu == {"data": {"orderId": "o1"}}

    asyncio.run(scenario())
    assert verbes == ["POST", "POST"], f"vu {verbes}"


def test_un_1021_qui_persiste_finit_par_lever_avec_son_indice() -> None:
    """Pas de boucle infinie : une seule resynchronisation, puis on rend la main.

    Et le message doit rester celui de l'horloge — c'est lui qui envoie
    l'utilisateur vers `w32tm /resync` plutôt que vers sa clé d'API.
    """
    signees: list[int] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/v3/time"):
                return _reponse_temps(1_000_000_000_000)
            signees.append(1)
            return httpx.Response(400, json={"code": -1021, "msg": "ahead"})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            with pytest.raises(BinanceApiError) as capture:
                await client.post("/trade/place-order-bundle", {"quoteId": "q1"})
        assert capture.value.code == -1021

    asyncio.run(scenario())
    assert len(signees) == 2, f"une seule resynchronisation attendue, vu {len(signees)}"


def test_un_serveur_de_temps_muet_ne_masque_pas_lerreur_dorigine() -> None:
    """Si `/api/v3/time` ne répond pas, on ne doit ni planter ailleurs ni boucler.

    L'erreur rendue reste `-1021` : c'est elle qui porte le diagnostic utile.
    """

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/v3/time"):
                return httpx.Response(503, json={"msg": "indisponible"})
            return httpx.Response(400, json={"code": -1021, "msg": "ahead"})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            with pytest.raises(BinanceApiError) as capture:
                await client.quota_status()
        assert capture.value.code == -1021

    asyncio.run(scenario())


# --- Schéma réel de /market/list -------------------------------------------
#
# ANCRAGE DE MESURE 2026-08-18, première lecture avec une clé valide. Les
# schémas REST de ce produit ne sont publiés nulle part : ce bloc fige ce qui
# a été observé en direct, pour que le jour où Binance change, l'échec pointe
# le fait exact qui a bougé.
#
#   - l'enveloppe s'appelle `marketTopics`, pas `data`/`rows`/`list` ;
#   - elle est à DEUX niveaux : un topic (la question) contient `markets` ;
#   - la pagination se fait par `limit`/`offset`. `page`/`rows` et
#     `pageIndex`/`pageSize` sont acceptés puis IGNORÉS — 200 et même contenu.

TOPIC_REEL = {
    "marketTopicId": 4585192,
    "vendor": "PREDICT_FUN",
    "chainId": "56",
    "slug": "btc-updown-5m-1787091300",
    "title": "BTC Up or Down 5m",
    "question": "Bitcoin Up or Down - August 18, 6:15PM-6:20PM ET",
    "topicType": "FLAT",
    "chartType": "CRYPTO_UP_DOWN",
    "symbol": "BTCUSDT",
    "collateral": "USDT",
    "feeRateBps": 200,
    "slippageBps": 1000,
    "tradeVolume": "517825.6",
    "liquidity": "5561.23",
    "startDate": 1787091300000,
    "endDate": 1787091600000,
    "status": "REGISTERED",
    "markets": [
        {
            "marketId": 7008435,
            "externalId": "1481687",
            "title": "Bitcoin Up or Down - August 18, 6:15PM-6:20PM ET",
            "conditionId": "0x1aab50a3eba5405c3ab09cde4ea06fdb44d10e3b",
            "status": "REGISTERED",
            "tradingStatus": "OPEN",
            "tradeVolume": "366.38",
            "liquidity": "2956.37",
            "decimalPrecision": 2,
            "outcomes": [
                {"name": "Up", "price": "0.99", "chance": "0.985",
                 "index": 0, "tokenId": "109247942808279480310162323153628139054"},
                {"name": "Down", "price": "0.01", "chance": "0.015",
                 "index": 1, "tokenId": "607631825259865073424795511898941003840"},
            ],
        }
    ],
}
MARKET_LIST_REEL = {
    "hasMore": True,
    "limit": 20,
    "offset": 0,
    "total": 1231,
    "marketTopics": [TOPIC_REEL],
}


def test_lenveloppe_market_topics_est_reconnue() -> None:
    """`marketTopics` ne figurait dans aucune des clés essayées : d'où l'échec
    « aucune liste trouvée » sur la toute première lecture authentifiée."""
    lignes = extract_rows(MARKET_LIST_REEL, where="market/list")
    assert len(lignes) == 1
    assert lignes[0]["marketTopicId"] == 4585192


def test_un_topic_est_aplati_en_marches_negociables() -> None:
    """Le topic n'est pas négociable : c'est la QUESTION. Le marché l'est.

    Confondre les deux ferait demander un carnet avec un `marketTopicId`, que
    l'API ne connaît pas — et l'erreur ressemblerait à un marché disparu.
    """
    marches = flatten_market_topics(MARKET_LIST_REEL, where="market/list")
    assert len(marches) == 1
    assert marches[0]["marketId"] == 7008435


def test_laplatissement_conserve_le_contexte_du_topic() -> None:
    """L'échéance et le taux de frais ne vivent QUE sur le topic.

    Les perdre en aplatissant rendrait tout marché « sans échéance ni taux de
    frais », c'est-à-dire inexploitable pour le moindre calcul de rendement.
    """
    marche = parse_market(flatten_market_topics(MARKET_LIST_REEL, where="x")[0])
    assert marche.end_time_ms == 1787091600000, "échéance perdue à l'aplatissement"
    assert marche.fee_rate_bps == 200, "taux de frais perdu à l'aplatissement"
    assert marche.volume_usdt == pytest.approx(366.38), "volume du MARCHÉ, pas du topic"
    assert marche.outcome_token_ids == (
        "109247942808279480310162323153628139054",
        "607631825259865073424795511898941003840",
    )


def test_un_champ_du_marche_prime_sur_celui_du_topic() -> None:
    """Le topic agrège plusieurs marchés : ses chiffres sont des totaux.

    Laisser le contexte écraser la ligne ferait afficher la liquidité de la
    question entière sur chacune de ses branches.
    """
    marche = parse_market(flatten_market_topics(MARKET_LIST_REEL, where="x")[0])
    assert marche.liquidity_usdt == pytest.approx(2956.37), (
        "la liquidité du topic (5561.23) a écrasé celle du marché"
    )


def test_un_topic_sans_marches_ne_disparait_pas_en_silence() -> None:
    """Un topic vide est une anomalie à signaler, pas une ligne à escamoter."""
    charge = {"marketTopics": [{"marketTopicId": 1, "title": "vide", "markets": []}]}
    with pytest.raises(BinanceSchemaError):
        flatten_market_topics(charge, where="market/list")


def test_la_pagination_part_en_limit_offset_pas_en_page_rows() -> None:
    """MESURÉ : `page`/`rows` rendent 200 et la MÊME première page.

    Un paramètre ignoré en silence est le pire des cas : la collecte piétine
    et le code conclut « le curseur n'avance pas » au lieu de « je pagine mal ».
    """
    vues: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            vues.append(request.url.query.decode())
            return httpx.Response(200, json=MARKET_LIST_REEL)

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.list_markets(limit=5, offset=10)

    asyncio.run(scenario())
    query = vues[0]
    assert "limit=5" in query and "offset=10" in query, query
    assert "page=" not in query and "rows=" not in query, (
        f"paramètre ignoré par le serveur encore envoyé : {query}"
    )


# --- Carnet : trois paramètres obligatoires, deux carnets par marché --------

BOOK_REST_REEL = {
    "outcome": "Up",
    "tokenId": "95971848599405445727024849658711881785435484081274233460620842541816943940616",
    "timestamp": 1787092439597,
    "bids": [{"price": "0.76", "size": "20"}, {"price": "0.75", "size": "75.56"}],
    "asks": [{"price": "0.77", "size": "31"}, {"price": "0.78", "size": "12"}],
}


def test_le_carnet_rest_exige_marketid_tokenid_et_vendor() -> None:
    """MESURÉ le 2026-08-18, un paramètre à la fois : `/order-book` réclame les
    TROIS. Chacun manquant rend `-3026` en nommant le suivant — donc l'absence
    de `vendor` ne se découvre qu'après avoir corrigé `tokenId`, et inversement.
    """
    vues: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            vues.append(request.url.query.decode())
            return httpx.Response(200, json=BOOK_REST_REEL)

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.fetch_book(7008470, token_id="95971", vendor="PREDICT_FUN")

    asyncio.run(scenario())
    for attendu in ("marketId=7008470", "tokenId=95971", "vendor=PREDICT_FUN"):
        assert attendu in vues[0], f"{attendu} absent de {vues[0]}"


def test_le_carnet_rest_se_lit_en_objets_prix_taille() -> None:
    """Le REST rend `{"price": …, "size": …}` là où le flux WS rend des paires.

    L'horodatage s'appelle `timestamp` ici, et `updateTimestampMs` sur le flux.
    """
    carnet = parse_book(BOOK_REST_REEL, market_id=7008470)
    assert carnet.market_id == 7008470
    assert carnet.best_bid is not None and carnet.best_bid.price == pytest.approx(0.76)
    assert carnet.best_ask is not None and carnet.best_ask.price == pytest.approx(0.77)
    assert carnet.updated_ms == 1787092439597, "horodatage `timestamp` non lu"


def test_chaque_branche_a_son_propre_carnet() -> None:
    """FAIT MESURÉ QUI CONTREDIT L'ADAPTATEUR PREDICT.FUN.

    Sur Predict.fun il n'existe QU'UN carnet par marché, le côté No étant
    dérivé (`no_ask = 1 − yes_bid`). Ici, chaque branche a le sien, interrogé
    par son `tokenId` propre. Mesuré sur « BTC Up or Down 5m » : meilleur bid
    Up = 0,76 et meilleur bid Down = 0,23, soit une somme de 0,99 — deux
    carnets indépendants, pas un carnet et son miroir.

    Conséquence : rien ne garantit ici la relation qui rendait l'arbitrage
    impossible PAR CONSTRUCTION chez Predict.fun. Ce test garde le fait ; il ne
    prétend pas qu'un arbitrage existe.
    """
    demandes: list[str] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.query.decode()
            demandes.append(query)
            outcome = "Up" if "tokenId=aaa" in query else "Down"
            return httpx.Response(200, json={**BOOK_REST_REEL, "outcome": outcome})

        transport = httpx.MockTransport(handler)
        marche = parse_market(
            {
                "marketId": 42,
                "vendor": "PREDICT_FUN",
                "outcomes": [
                    {"name": "Up", "tokenId": "aaa"},
                    {"name": "Down", "tokenId": "bbb"},
                ],
            }
        )
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            carnets = await client.fetch_books([marche])
        assert set(carnets) == {"aaa", "bbb"}, (
            "les carnets doivent être indexés par JETON, pas par marché : "
            "deux branches d'un même marché s'écraseraient l'une l'autre"
        )

    asyncio.run(scenario())
    assert len(demandes) == 2, f"une requête par branche attendue, vu {demandes}"


# --- Adresse du portefeuille de prédiction ---------------------------------
#
# MESURÉ le 2026-08-18 : `position/list`, `pnl/portfolio`, `order/list`,
# `order/history` et `trade/get-quote` exigent TOUS `walletAddress`, que le
# client n'envoyait pas. Ces cinq lectures étaient donc cassées — et l'erreur
# `-3026` ne nommait qu'un paramètre à la fois, donc rien ne disait que le
# défaut était commun.

WALLET_LIST_REEL = {
    "wallets": [
        {
            "walletAddress": "0x5e4d4890351d0ea889e99d392dda5e007405bd66",
            "walletId": "ad424a7a2e50401ca34b9fefeb0108b9",
            "registeredTime": 1786174726035,
        }
    ]
}


def _transport_avec_portefeuille(vues: list[str], reponse: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        vues.append(str(request.url))
        if request.url.path.endswith("/wallet/list"):
            return httpx.Response(200, json=WALLET_LIST_REEL)
        return httpx.Response(200, json=reponse)

    return httpx.MockTransport(handler)


def test_les_routes_de_compte_portent_ladresse_du_portefeuille() -> None:
    vues: list[str] = []

    async def scenario() -> None:
        transport = _transport_avec_portefeuille(vues, {"positions": []})
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.positions()

    asyncio.run(scenario())
    appel = [u for u in vues if "position/list" in u][0]
    assert "walletAddress=0x5e4d4890351d0ea889e99d392dda5e007405bd66" in appel, appel


def test_ladresse_nest_lue_quune_seule_fois() -> None:
    """Un aller-retour par lecture ferait doubler le coût de chaque balayage."""
    vues: list[str] = []

    async def scenario() -> None:
        transport = _transport_avec_portefeuille(vues, {"positions": []})
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            await client.positions()
            await client.positions()
            await client.active_orders()

    asyncio.run(scenario())
    lectures = [u for u in vues if "wallet/list" in u]
    assert len(lectures) == 1, f"{len(lectures)} lectures de wallet/list"


def test_sans_portefeuille_de_prediction_le_message_est_actionnable() -> None:
    """Un compte sans portefeuille n'est pas une panne : c'est une étape non faite.

    Le dire explicitement évite de renvoyer l'utilisateur inspecter sa clé.
    """

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"wallets": []})

        transport = httpx.MockTransport(handler)
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            with pytest.raises(BinanceApiError) as capture:
                await client.positions()
        assert "compte Prédiction" in str(capture.value)

    asyncio.run(scenario())


def test_les_enveloppes_orders_et_positions_sont_reconnues() -> None:
    """MESURÉ : `order/list` enveloppe sous `orders`, `position/list` sous
    `positions`. Aucun des deux ne figurait dans les clés essayées."""
    assert len(extract_rows({"total": 0, "orders": [{"a": 1}]}, where="x")) == 1
    assert len(extract_rows({"summary": {}, "positions": [{"b": 2}]}, where="x")) == 1


def test_le_devis_porte_adresse_vendor_et_amountin() -> None:
    """MESURÉ en escalier : get-quote réclame `walletAddress`, puis `amountIn`.

    `quantity` seul ne suffit pas : c'est un montant EN USDT qui est demandé.
    """
    vues: list[str] = []

    async def scenario() -> None:
        transport = _transport_avec_portefeuille(
            vues, {"quoteId": "q-1", "amountOut": "3.0"}
        )
        async with BinancePredictionClient(
            credentials=FAKE, transport=transport
        ) as client:
            trader = PredictionTrader(client=client, limits=LIMITES, armed=False)
            ordre = PredictionOrder(
                market_id=7008480,
                token_id="abc",
                side="BUY",
                order_type=LIMIT,
                price=0.30,
                size=10.0,
            )
            devis = await trader.get_quote(ordre)
            assert devis.quote_id == "q-1"

    asyncio.run(scenario())
    appel = [u for u in vues if "get-quote" in u][0]
    for attendu in ("walletAddress=0x5e4d", "vendor=PREDICT_FUN", "amountIn=3"):
        assert attendu in appel, f"{attendu} absent de {appel}"


# --- Unité de `amountIn` ----------------------------------------------------
#
# MESURÉ le 2026-08-18, et c'est le piège le plus dangereux du module.
# `amountIn` est en UNITÉS DE BASE à 18 décimales, pas en USDT. Envoyer `8.0`
# demande huit wei : le serveur répond `-9000 order amount is too small`, un
# message qui accuse le SOLDE alors que la faute est à l'UNITÉ. L'utilisateur
# avait 8,73 USDT et des ordres passés à 1 et 5 USDT — un minimum supérieur à 8
# était donc impossible, ce qui a permis de trancher.
#
# L'erreur symétrique serait bien pire : une conversion appliquée deux fois
# demanderait 10^18 fois trop. D'où un test qui fige la valeur exacte.


def test_amount_in_est_converti_en_unites_de_base_18_decimales() -> None:
    assert to_base_units(2.0) == "2000000000000000000"
    assert to_base_units(0.01) == "10000000000000000"
    assert to_base_units(8.73) == "8730000000000000000"


def test_une_conversion_appliquee_deux_fois_est_refusee() -> None:
    """GARDE-FOU. 10^18 USDT n'existe pas : c'est une conversion en double.

    Sans ce refus, l'erreur ne se verrait qu'au moment où le serveur accepte —
    ou pire, l'accepte partiellement.
    """
    with pytest.raises(ValueError):
        to_base_units(2e18)


def test_un_montant_nul_ou_negatif_est_refuse() -> None:
    for mauvais in (0.0, -1.0):
        with pytest.raises(ValueError):
            to_base_units(mauvais)


def test_la_conversion_ne_passe_jamais_par_un_flottant_binaire() -> None:
    """0,07 n'est pas représentable en binaire : `int(0.07 * 10**18)` rend
    69999999999999992. Un ordre décalé d'un wei n'est pas grave ; une méthode
    qui perd des décimales sur des montants plus gros l'est."""
    assert to_base_units(0.07) == "70000000000000000"
    assert to_base_units(1.1) == "1100000000000000000"
