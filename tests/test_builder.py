"""Tests du programme Builders.

Chaque test rejoue un piège MESURÉ contre l'API le 2026-08-15, pas une
mécanique inventée : c'est la seule façon de vérifier qu'un correctif tient
sans repasser une journée à sonder le réseau.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from donmarket.builder.attribution import (
    API_VARS,
    CODE_VAR,
    AttributionNotConfigured,
    attribution_status,
    build_builder_config,
    load_attribution,
)
from donmarket.builder.api import (
    BuilderApiError,
    BuilderTrade,
    LeaderboardEntry,
    TradeSample,
    fetch_builder_trades,
    fetch_leaderboard,
)
from donmarket.builder.codes import (
    BuilderCode,
    InvalidBuilderCode,
    is_valid_builder_code,
    normalise_builder_code,
)
from donmarket.builder.fees import (
    FeeModelError,
    builder_fee_usd,
    infer_rate,
    infer_schedule,
    platform_fee_usd,
    published_max_bps,
)
from donmarket.builder.revenue import (
    Projection,
    build_estimate,
    rank_by_revenue,
    volume_needed_for,
)

VALID_CODE = "0x" + "0" * 63 + "1"


# --------------------------------------------------------------------------
# Les codes : la faute de frappe qui se paie en revenu perdu
# --------------------------------------------------------------------------


def test_les_cinq_formes_muettes_mesurees_sont_toutes_refusees():
    """Les cinq variantes qui rendent une page VIDE au lieu d'une erreur.

    Mesurées le 2026-08-13 : le serveur répond HTTP 200 et `{"data":[]}` pour
    chacune, indiscernable d'un builder sans volume.
    """
    for muet in ("0x01", "0" * 63 + "1", "0xZZ" + "0" * 62, "CHOUCROUTE", ""):
        with pytest.raises(InvalidBuilderCode):
            normalise_builder_code(muet)


def test_le_prefixe_majuscule_est_normalise_et_non_rejete():
    """`0X…` désigne le même nombre : on normalise au lieu de refuser."""
    assert normalise_builder_code("0X" + "0" * 62 + "AB") == "0x" + "0" * 62 + "ab"


def test_les_espaces_de_copier_coller_sont_retires():
    assert normalise_builder_code(f"  {VALID_CODE}\n") == VALID_CODE


def test_un_code_court_n_est_jamais_complete_par_des_zeros():
    """Compléter `0x01` en `0x00…01` attribuerait le volume à un TIERS."""
    with pytest.raises(InvalidBuilderCode) as exc:
        normalise_builder_code("0x01")
    assert "2 chiffres après" in str(exc.value)


def test_le_message_dit_ce_qui_cloche_pas_seulement_que_ca_cloche():
    with pytest.raises(InvalidBuilderCode) as exc:
        normalise_builder_code("zz" + "0" * 64)
    assert "préfixe" in str(exc.value)


def test_le_type_valide_ne_peut_pas_etre_malforme_apres_construction():
    code = BuilderCode("  0X" + "0" * 63 + "1  ")
    assert code.value == VALID_CODE
    assert code.short == "0x0000…0001"
    with pytest.raises(InvalidBuilderCode):
        BuilderCode("CHOUCROUTE")


def test_le_predicat_ne_leve_jamais():
    assert is_valid_builder_code(VALID_CODE)
    assert not is_valid_builder_code(None)
    assert not is_valid_builder_code(42)
    assert not is_valid_builder_code("0x01")


# --------------------------------------------------------------------------
# Le modèle de frais
# --------------------------------------------------------------------------


def test_le_frais_builder_porte_sur_le_notionnel_usdc():
    """MetaMask : 400 bps sur 20,00 $ de notionnel = 0,80 $ (ligne réelle)."""
    assert builder_fee_usd(20.0, 400.0) == pytest.approx(0.79999, abs=1e-4)


def test_le_frais_builder_n_impose_aucun_plafond():
    """Imposer les 100 bps publiés rendrait un chiffre FAUX sur MetaMask."""
    assert builder_fee_usd(100.0, 400.0) == pytest.approx(4.0)


def test_le_frais_de_plateforme_suit_la_variance_pas_le_notionnel():
    """Ligne réelle : 28,11111 parts à 0,18, taux 0,07 → 0,29044 $."""
    assert platform_fee_usd(28.11111, 0.18, 0.07) == pytest.approx(0.29044, abs=1e-5)


def test_le_frais_de_plateforme_est_maximal_au_milieu():
    """`p(1−p)` culmine à 0,50 — l'inverse d'un pourcentage du notionnel."""
    milieu = platform_fee_usd(100.0, 0.50, 0.07)
    bord = platform_fee_usd(100.0, 0.98, 0.07)
    assert milieu > bord * 12


def test_le_taux_de_marche_n_a_pas_de_defaut():
    with pytest.raises(TypeError):
        platform_fee_usd(100.0, 0.5)  # type: ignore[call-arg]


def test_un_prix_hors_bornes_est_refuse():
    with pytest.raises(FeeModelError):
        platform_fee_usd(10.0, 1.4, 0.07)


def test_le_cote_inconnu_est_refuse():
    with pytest.raises(FeeModelError):
        published_max_bps("PRENEUR")


# --------------------------------------------------------------------------
# L'inférence du taux : le maximum, pas la médiane
# --------------------------------------------------------------------------


def _trade(notional: float, fee: float, side: str = "TAKER") -> BuilderTrade:
    return BuilderTrade(
        trade_id=f"t{notional}-{fee}-{side}",
        trade_type=side,
        market="0xmarket",
        price=0.5,
        shares=notional * 2,
        notional_usd=notional,
        platform_fee_usd=0.0,
        builder_fee_usd=fee,
        builder_code=VALID_CODE,
        match_time=1_780_000_000,
        outcome="Yes",
        side="BUY",
    )


def test_le_taux_est_le_maximum_car_la_troncature_ne_peut_que_baisser():
    """Réglage à 10 bps, avec des lignes tronquées vers le bas.

    La médiane dirait 7,5 bps. Le maximum dit 10 — le vrai réglage.
    """
    trades = [
        _trade(1000.0, 1.0),  # 10,00 bps, gros ticket, non tronqué
        _trade(100.0, 0.075),  # 7,50 bps, tronqué
        _trade(50.0, 0.030),  # 6,00 bps, tronqué
    ]
    rate = infer_rate(trades, "TAKER")
    assert rate is not None
    assert rate.bps == pytest.approx(10.0)
    assert rate.median_bps < rate.bps


def test_les_lignes_trop_petites_sont_exclues_de_l_inference():
    """Une ligne à 1 $ facturée 0,00 $ ne dit rien du réglage.

    Mesurée chez MetaMask : notionnel 1,00 $, builderFee 0,000000.
    """
    trades = [_trade(1000.0, 40.0), _trade(1.0, 0.0)]
    rate = infer_rate(trades, "TAKER")
    assert rate is not None
    assert rate.bps == pytest.approx(400.0)
    assert rate.samples == 2  # les totaux gardent la ligne muette


def test_un_builder_gratuit_se_distingue_d_un_builder_inconnu():
    """0 bps avec des échantillons, ce n'est pas la même chose qu'aucune donnée."""
    gratuit = infer_rate([_trade(1000.0, 0.0)], "TAKER")
    assert gratuit is not None and gratuit.bps == 0.0
    assert infer_rate([_trade(1000.0, 1.0, side="MAKER")], "TAKER") is None


def test_le_depassement_du_plafond_publie_est_signale_pas_corrige():
    """MetaMask à 400 bps : on le rapporte, on ne le rabote pas."""
    rate = infer_rate([_trade(1000.0, 40.0)], "TAKER")
    assert rate is not None
    assert rate.bps == pytest.approx(400.0)
    assert rate.exceeds_published_cap


def test_un_taux_pile_au_plafond_n_est_PAS_signale_comme_au_dessus():
    """Cas réel : Bagel à 100,0/50,0 — pile aux deux plafonds.

    `0.5 / 1000 * 10000` vaut `50.000000000000007` en virgule flottante. Sans
    tolérance, ce bit de bruit fait accuser un builder d'enfreindre un plafond
    public, à côté de MetaMask et RedotPay qui, eux, facturent 400 bps. Sur une
    page publique, mélanger les deux discrédite la mesure qui compte.
    """
    maker = infer_rate([_trade(1000.0, 5.0, side="MAKER")], "MAKER")
    assert maker is not None
    assert maker.bps == pytest.approx(50.0)
    assert not maker.exceeds_published_cap

    taker = infer_rate([_trade(1000.0, 10.0)], "TAKER")
    assert taker is not None
    assert taker.bps == pytest.approx(100.0)
    assert not taker.exceeds_published_cap


def test_un_vrai_depassement_reste_signale():
    """La tolérance ne doit pas laisser passer MetaMask à 400 bps."""
    rate = infer_rate([_trade(1000.0, 40.0)], "TAKER")
    assert rate is not None and rate.exceeds_published_cap

    # Et un dépassement franc mais modeste doit passer aussi : 2 bps au-dessus
    # du plafond n'est pas du bruit de flottant.
    modeste = infer_rate([_trade(1000.0, 10.2)], "TAKER")
    assert modeste is not None and modeste.exceeds_published_cap


def test_deux_paliers_ronds_trahissent_un_changement_de_taux():
    trades = [_trade(1000.0 + i, 5.0 + i * 0.005) for i in range(6)]  # ~50 bps
    trades += [_trade(1000.0 + i, 10.0 + i * 0.01) for i in range(6)]  # ~100 bps
    rate = infer_rate(trades, "TAKER")
    assert rate is not None
    assert rate.epochs_suspected
    assert len(rate.clusters) >= 2


def test_le_taux_melange_pese_la_part_teneur_et_preneur():
    """traderline : 10 bps preneur / 5 bps teneur, mais 7,32 bps encaissés."""
    trades = [_trade(1000.0, 1.0, "TAKER")] + [_trade(1000.0, 0.5, "MAKER")] * 3
    schedule = infer_schedule(trades)
    assert schedule.taker is not None and schedule.taker.bps == pytest.approx(10.0)
    assert schedule.maker is not None and schedule.maker.bps == pytest.approx(5.0)
    # (1×10 + 3×5) / 4 = 6,25 bps — entre les deux, et ni l'un ni l'autre
    assert schedule.blended_bps == pytest.approx(6.25)


def test_un_builder_sans_frais_est_reconnu_comme_tel():
    schedule = infer_schedule([_trade(1000.0, 0.0), _trade(500.0, 0.0, "MAKER")])
    assert schedule.charges_nothing


# --------------------------------------------------------------------------
# Le client HTTP : pagination, sentinelle de fin, code invalide
# --------------------------------------------------------------------------


def _page(rows: list[dict], cursor: str) -> httpx.Response:
    return httpx.Response(
        200, json={"data": rows, "next_cursor": cursor, "limit": 300, "count": len(rows)}
    )


def _row(i: int) -> dict:
    return {
        "id": f"id-{i}",
        "tradeType": "TAKER",
        "market": "0xm",
        "price": "0.5",
        "size": "20",
        "sizeUsdc": "10",
        "feeUsdc": "0.1",
        "builderFee": "0.05",
        "builderCode": VALID_CODE,
        "matchTime": "1780000000",
        "outcome": "Yes",
        "side": "BUY",
    }


def test_la_sentinelle_de_fin_arrete_la_pagination():
    """`LTE=` est la FIN, pas un curseur. Le traiter comme tel boucle sans fin."""
    pages = [
        _page([_row(i) for i in range(300)], "MzAw"),
        _page([_row(i) for i in range(300, 450)], "LTE="),
    ]
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return pages[len(calls) - 1]

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            return await fetch_builder_trades(client, VALID_CODE)

    sample = asyncio.run(scenario())

    assert len(calls) == 2
    assert len(sample) == 450
    assert sample.is_complete
    assert not sample.truncated


def test_le_nom_du_parametre_est_snake_case():
    """`builderCode` rend un HTTP 400 : le seul nom accepté est `builder_code`."""
    vu: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vu.append(dict(request.url.params))
        return _page([], "LTE=")

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            await fetch_builder_trades(client, VALID_CODE)

    asyncio.run(scenario())

    assert "builder_code" in vu[0]
    assert "builderCode" not in vu[0]


def test_un_code_invalide_leve_avant_le_moindre_appel_reseau():
    """Sans cette garde, la page vide passerait pour « ce builder n'a rien »."""
    appels = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal appels
        appels += 1
        return _page([], "LTE=")

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            with pytest.raises(InvalidBuilderCode):
                await fetch_builder_trades(client, "0x01")

    asyncio.run(scenario())

    assert appels == 0


def test_l_echantillon_tronque_l_avoue():
    """Plafond de pages atteint : les sommes deviennent des planchers."""
    offset = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal offset
        offset += 300
        rows = [_row(i) for i in range(offset, offset + 300)]
        return _page(rows, f"curseur-{offset}")

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            return await fetch_builder_trades(client, VALID_CODE, max_pages=3)

    sample = asyncio.run(scenario())

    assert sample.truncated
    assert not sample.is_complete
    assert sample.pages == 3


def test_un_curseur_qui_pietine_ne_se_declare_pas_complet():
    """Le mode de panne de Gamma et de Predict.fun, appliqué ici.

    S'arrêter est juste ; se déclarer complet ne l'est pas — rien ne dit qu'on
    a tout vu, et le total serait faux tout en paraissant définitif.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _page([_row(i) for i in range(300)], "MzAw")

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            return await fetch_builder_trades(client, VALID_CODE, max_pages=10)

    sample = asyncio.run(scenario())

    assert sample.pages == 2  # une page, puis la répétition détectée
    assert sample.truncated
    assert not sample.is_complete


def test_un_400_ne_declenche_pas_trois_reessais():
    """Le 400 est structurel : réessayer ne fait que retarder le diagnostic."""
    appels = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal appels
        appels += 1
        return httpx.Response(400, json={"error": "builder code is required"})

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://clob.test") as client:
            with pytest.raises(BuilderApiError):
                await fetch_builder_trades(client, VALID_CODE)

    asyncio.run(scenario())

    assert appels == 1


def test_le_classement_rend_une_liste_nue_pas_une_enveloppe():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "rank": "1",
                    "builder": "betmoar",
                    "builderCode": VALID_CODE,
                    "volume": 2060476871.0,
                    "activeUsers": 3735,
                    "verified": True,
                }
            ],
        )

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://data.test") as client:
            return await fetch_leaderboard(client, period="ALL")

    entries = asyncio.run(scenario())

    assert len(entries) == 1
    assert entries[0].builder == "betmoar"
    assert entries[0].has_usable_code
    assert entries[0].volume_unit_is_assumed


def test_une_periode_inconnue_est_refusee_avant_l_appel():
    async def scenario():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
        async with httpx.AsyncClient(transport=transport, base_url="https://data.test") as client:
            with pytest.raises(BuilderApiError):
                await fetch_leaderboard(client, period="TRIMESTRE")

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Le revenu
# --------------------------------------------------------------------------


def _entry(name: str, volume: float, users: int = 100) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=1, builder=name, code=VALID_CODE, volume=volume, active_users=users, verified=True
    )


def _sample(trades: list[BuilderTrade], truncated: bool = False) -> TradeSample:
    return TradeSample(code=VALID_CODE, trades=tuple(trades), pages=1, truncated=truncated)


def test_le_volume_n_est_pas_le_revenu():
    """Le fait central : le premier au volume peut encaisser zéro."""
    gros_gratuit = build_estimate(_entry("betmoar", 2_000_000_000.0), _sample([_trade(1000.0, 0.0)]))
    petit_payant = build_estimate(_entry("Polycule", 10_000_000.0), _sample([_trade(1000.0, 10.0)]))

    assert gros_gratuit.estimated_period_revenue_usd == pytest.approx(0.0)
    assert petit_payant.estimated_period_revenue_usd == pytest.approx(100_000.0)

    classement = rank_by_revenue([gros_gratuit, petit_payant])
    assert classement[0].builder == "Polycule"


def test_un_taux_inconnu_ne_devient_pas_zero():
    """« Inconnu » et « gratuit » sont deux réponses différentes."""
    inconnu = build_estimate(_entry("Mystere", 1_000_000.0), _sample([]))
    assert inconnu.estimated_period_revenue_usd is None

    gratuit = build_estimate(_entry("Gate", 1_000_000.0), _sample([_trade(1000.0, 0.0)]))
    assert gratuit.estimated_period_revenue_usd == pytest.approx(0.0)

    classement = rank_by_revenue([inconnu, gratuit])
    assert classement[-1].builder == "Mystere"  # l'inconnu passe DERRIÈRE le gratuit


def test_aucun_revenu_estime_ne_se_presente_comme_une_mesure():
    est = build_estimate(_entry("X", 1_000_000.0), _sample([_trade(1000.0, 10.0)]))
    assert est.is_measured is False
    assert any("unité du volume" in c for c in est.caveats)


def test_la_troncature_apparait_dans_les_reserves():
    est = build_estimate(_entry("X", 1e6), _sample([_trade(1000.0, 10.0)], truncated=True))
    assert any("tronqué" in c for c in est.caveats)


def test_le_depassement_de_plafond_apparait_dans_les_reserves():
    est = build_estimate(_entry("MetaMask", 1e6), _sample([_trade(1000.0, 40.0)]))
    assert any("plafond documenté n'est pas appliqué" in c for c in est.caveats)


def test_le_revenu_par_utilisateur_distingue_les_deux_metiers():
    baleine = build_estimate(_entry("Jupiter", 120_000_000.0, users=3), _sample([_trade(1000.0, 5.0)]))
    audience = build_estimate(_entry("polymtrade", 268_000_000.0, users=31_289), _sample([_trade(1000.0, 5.0)]))
    assert baleine.revenue_per_user_usd > audience.revenue_per_user_usd * 100


def test_la_projection_est_de_l_arithmetique_explicite():
    p = Projection(daily_volume_usd=200_000.0, taker_bps=100.0, maker_bps=0.0, taker_share=0.5)
    assert p.blended_bps == pytest.approx(50.0)
    assert p.daily_usd == pytest.approx(1000.0)
    assert p.monthly_usd == pytest.approx(30_000.0)


def test_le_volume_requis_dit_la_verite_brutale():
    """10 $/jour à 50 bps mélangés (0,50 %) = 2 000 $ de volume routé par jour."""
    besoin = volume_needed_for(10.0, taker_bps=100.0, maker_bps=0.0, taker_share=0.5)
    assert besoin == pytest.approx(2_000.0)


def test_un_bareme_gratuit_ne_produit_jamais_le_revenu_vise():
    with pytest.raises(ValueError):
        volume_needed_for(10.0, taker_bps=0.0, maker_bps=0.0)


def test_une_part_preneur_hors_bornes_est_refusee():
    with pytest.raises(ValueError):
        Projection(daily_volume_usd=1.0, taker_bps=10.0, maker_bps=5.0, taker_share=1.5)


# --------------------------------------------------------------------------
# L'attribution : le code lit, ce sont les identifiants qui attribuent
# --------------------------------------------------------------------------


@pytest.fixture
def env_vierge(monkeypatch):
    """Isole du `.env` de la machine : sinon le test dit la vérité d'ici."""
    for name in (CODE_VAR, *API_VARS):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_sans_rien_configure_on_ne_peut_ni_lire_ni_attribuer(env_vierge):
    a = load_attribution()
    assert not a.can_read_attribution
    assert not a.can_attribute
    assert set(a.missing) == {CODE_VAR, *API_VARS}


def test_le_code_seul_permet_de_lire_mais_PAS_d_attribuer(env_vierge):
    """Le piège central : croire que poser le code suffit à toucher les frais."""
    env_vierge.setenv(CODE_VAR, VALID_CODE)
    a = load_attribution()
    assert a.can_read_attribution
    assert not a.can_attribute  # la signature attribue, pas le code
    assert set(a.missing) == set(API_VARS)


def test_les_identifiants_seuls_attribuent(env_vierge):
    for name in API_VARS:
        env_vierge.setenv(name, "valeur-factice")
    a = load_attribution()
    assert a.can_attribute
    assert not a.can_read_attribution  # on attribue sans savoir lire le résultat


def test_un_code_malforme_n_est_pas_traite_comme_un_code_absent(env_vierge):
    """Réglage à faire ≠ faute de frappe. La seconde produit un faux zéro."""
    env_vierge.setenv(CODE_VAR, "0x01")
    a = load_attribution()
    assert a.code is None
    assert a.code_error is not None
    assert "2 chiffres après" in a.code_error


def test_le_code_est_normalise_a_la_lecture(env_vierge):
    env_vierge.setenv(CODE_VAR, "  0X" + "0" * 63 + "1  ")
    a = load_attribution()
    assert a.code is not None and a.code.value == VALID_CODE


def test_le_refus_de_construire_explique_ce_qui_manque(env_vierge):
    env_vierge.setenv(CODE_VAR, VALID_CODE)
    with pytest.raises(AttributionNotConfigured) as exc:
        build_builder_config()
    assert "POLYMARKET_BUILDER_API_KEY" in str(exc.value)
    assert "perdus définitivement" in str(exc.value)


def test_le_rapport_ne_laisse_fuir_aucun_secret(env_vierge):
    env_vierge.setenv(CODE_VAR, VALID_CODE)
    for name in API_VARS:
        env_vierge.setenv(name, "secret-tres-confidentiel")
    rapport = attribution_status()
    assert "secret-tres-confidentiel" not in repr(rapport)
    assert rapport["code"] == "0x0000…0001"  # tronqué, et ce n'est pas un secret
    assert rapport["tier_is_unknown"] is True  # le palier ne se lit pas dans l'API
