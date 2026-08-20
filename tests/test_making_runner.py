"""Tests de la boucle de tenue de marché Polymarket.

Le client est remplacé par un double : la boucle doit être vérifiable sans
place de marché en face, et surtout sans qu'un test puisse poser un ordre réel.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from donmarket.making.core import DesiredOrder, Rung
from donmarket.making.runner import (
    LiveOrder,
    read_inventory,
    read_live_orders,
    reconcile,
    run_making,
)


def _rung(token="t1", bid=0.20, ask=0.24) -> Rung:
    return Rung(
        condition_id="0xC",
        token_id=token,
        question="une question ?",
        buy_price=bid,
        sell_price=ask,
        ticket_usd=bid * 5,
        spread_ticks=round((ask - bid) / 0.01),
    )


def _voulu(price=0.20, side="BUY", token="t1") -> DesiredOrder:
    return DesiredOrder(
        condition_id="0xC", token_id=token, side=side, price=price, size=10.0
    )


def _vivant(order_id="O-1", price=0.20, side="BUY", token="t1") -> LiveOrder:
    return LiveOrder(order_id=order_id, token_id=token, side=side, price=price)


# --- Réconciliation --------------------------------------------------------


def test_un_ordre_deja_au_bon_prix_est_garde_pas_rejoue() -> None:
    """ANCRAGE. Réémettre un ordre identique lui fait perdre sa place dans la
    file — et la place dans la file est exactement ce qui décide d'être rempli.
    C'est le seul avantage d'un teneur arrivé tôt."""
    poser, annuler, garder = reconcile(
        [_voulu()], [_vivant()], nous=frozenset({"O-1"})
    )
    assert poser == [] and annuler == [] and len(garder) == 1


def test_un_ordre_vraiment_loin_du_prix_est_annule_et_repose() -> None:
    """RECALÉ le 2026-08-20 : ce test utilisait un écart d'UN pas, qui doit
    désormais être ignoré. C'est justement le comportement corrigé — recoter
    pour un pas renvoyait l'ordre en fin de file à chaque tour et l'empêchait
    d'être jamais servi. Voir les tests d'hystérésis plus bas."""
    poser, annuler, _ = reconcile(
        [_voulu(price=0.24)], [_vivant(price=0.20)], nous=frozenset({"O-1"})
    )
    assert len(poser) == 1 and poser[0].price == 0.24
    assert len(annuler) == 1


def test_un_ordre_etranger_nest_jamais_annule() -> None:
    """ANCRAGE DE SÉCURITÉ, et il vient d'une vraie alerte du 2026-08-19 : la
    boucle Binance a visé trente fois l'ordre posé à la main par le
    propriétaire du compte. Une machine n'a pas à défaire ce qu'un humain a
    décidé, et « je ne l'ai pas reconnu » n'est pas une raison de supprimer."""
    etranger = _vivant(order_id="ETRANGER", token="tX")
    a_nous = _vivant(order_id="O-1", token="t1")
    _poser, annuler, _garder = reconcile(
        [], [etranger, a_nous], nous=frozenset({"O-1"})
    )
    assert [o.order_id for o in annuler] == ["O-1"]


def test_achat_et_vente_sont_des_cles_distinctes() -> None:
    poser, annuler, _ = reconcile(
        [_voulu(side="SELL", price=0.24)],
        [_vivant(side="BUY", price=0.24)],
        nous=frozenset({"O-1"}),
    )
    assert len(poser) == 1 and poser[0].side == "SELL"
    assert len(annuler) == 1 and annuler[0].side == "BUY"


# --- Lectures défensives ---------------------------------------------------


@dataclass
class _OrdreBrut:
    id: str = "A"
    asset_id: str = "t1"
    side: str = "BUY"
    price: str = "0.20"


def test_un_ordre_illisible_est_ignore_pas_devine() -> None:
    class Cassé:
        pass

    lus = read_live_orders([_OrdreBrut(), Cassé()])
    assert len(lus) == 1 and lus[0].order_id == "A"


@dataclass
class _PositionBrute:
    asset: str = "t1"
    size: float = 4.0


def test_une_position_lisible_alimente_linventaire() -> None:
    inv, motif = read_inventory([_PositionBrute()])
    assert motif is None and inv.held("t1") == pytest.approx(4.0)


def test_une_position_illisible_suspend_la_vente() -> None:
    """« Je ne détiens rien » et « je n'ai pas su lire » mènent à des décisions
    opposées : la première fait acheter, la seconde doit faire s'abstenir."""

    class Inconnue:
        pass

    _inv, motif = read_inventory([Inconnue()])
    assert motif is not None


# --- Boucle ----------------------------------------------------------------


@dataclass
class _Recu:
    ok: bool = True
    order_id: str = "O-NEUF"


class _ClientDouble:
    """Double de `SecureClient` : enregistre au lieu d'envoyer."""

    def __init__(self, ouverts=None, positions=None, ok=True):
        self.ouverts = ouverts or []
        self.positions = positions or []
        self.ok = ok
        self.poses: list[tuple] = []
        self.annules: list[list[str]] = []

    def list_open_orders(self):
        return self.ouverts

    def list_positions(self):
        return self.positions

    def place_limit_order(self, *, token_id, price, size, side, post_only=False):
        self.poses.append((token_id, price, size, side, post_only))
        return _Recu(ok=self.ok, order_id=f"O-{len(self.poses)}")

    def cancel_orders(self, *, order_ids):
        self.annules.append(list(order_ids))
        return None


def _source(rungs=None):
    return lambda: (rungs if rungs is not None else [_rung()], [])


def _horloge():
    valeurs = iter(range(0, 100_000, 40))
    return lambda: next(valeurs)


def test_desarmee_la_boucle_planifie_et_nenvoie_rien() -> None:
    client = _ClientDouble()
    rapport = run_making(
        client, _source(), bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=False, sleep=lambda _s: None, now=_horloge(),
    )
    assert rapport.armed is False
    assert rapport.placed > 0, "aucun ordre planifié"
    assert client.poses == [] and client.annules == []


def test_armee_chaque_ordre_part_en_post_only() -> None:
    """Non négociable : un teneur qui traverse l'écart devient preneur, paie
    les frais au lieu de les éviter, et détruit la raison d'être de la
    stratégie. Mieux vaut un ordre refusé qu'un ordre qui coûte."""
    client = _ClientDouble()
    run_making(
        client, _source(), bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=True, sleep=lambda _s: None, now=_horloge(),
    )
    assert client.poses, "aucun ordre posé"
    assert all(pose[4] is True for pose in client.poses), "post_only absent"


def test_un_refus_post_only_est_compte_pas_confondu_avec_un_succes() -> None:
    """`post_only` refuse exactement quand il doit : ce n'est pas une panne, et
    le compter comme un ordre pose fausserait toute mesure de remplissage."""
    client = _ClientDouble(ok=False)
    rapport = run_making(
        client, _source(), bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=True, sleep=lambda _s: None, now=_horloge(),
    )
    assert rapport.refused > 0 and rapport.placed == 0


def test_le_nettoyage_final_porte_sur_ce_quon_a_pose() -> None:
    """Un ordre posé APRÈS la dernière lecture ne figure dans aucun relevé —
    c'est le cas de tout ordre du dernier tour, donc le plus fréquent. Partir
    des relevés le laissait vivant au carnet après l'arrêt."""
    client = _ClientDouble()
    run_making(
        client, _source(), bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=True, sleep=lambda _s: None, now=_horloge(),
    )
    assert client.annules, "aucun nettoyage final"
    assert "O-1" in client.annules[-1]


def test_un_releve_rate_narrete_pas_la_boucle() -> None:
    """Les ordres posés restent au carnet : il faut continuer à les surveiller
    plutôt que de rendre la main en les abandonnant."""

    def source_cassee():
        raise RuntimeError("réseau coupé")

    client = _ClientDouble()
    rapport = run_making(
        client, source_cassee, bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=False, sleep=lambda _s: None, now=_horloge(),
    )
    assert rapport.ticks >= 1


# --- Hysteresis ------------------------------------------------------------


def test_un_ecart_dun_pas_ne_declenche_pas_de_recotation() -> None:
    """ANCRAGE, et il vient de la premiere heure armee du 2026-08-20 : 13
    ordres poses pour 24 annules en 47 tours, et ZERO remplissage.

    Recoter renvoie l'ordre en FIN DE FILE, et la place dans la file est le
    seul avantage d'un teneur. Un ordre qui ne reste jamais en place n'est
    jamais servi. Mieux vaut un ordre un pas trop bas qui vieillit qu'un ordre
    parfaitement place qui repart de zero chaque minute.
    """
    poser, annuler, garder = reconcile(
        [_voulu(price=0.21)], [_vivant(price=0.20)], nous=frozenset({"O-1"})
    )
    assert poser == [], "recotation declenchee pour un seul pas"
    assert annuler == []
    assert len(garder) == 1


def test_un_ecart_de_trois_pas_declenche_bien_la_recotation() -> None:
    """L'hysteresis ne doit pas devenir de l'immobilisme : quand le marche a
    vraiment bouge, un ordre reste loin du carnet ne sera jamais servi non
    plus, et il immobilise du capital pour rien."""
    poser, annuler, _garder = reconcile(
        [_voulu(price=0.23)], [_vivant(price=0.20)], nous=frozenset({"O-1"})
    )
    assert len(poser) == 1 and poser[0].price == 0.23
    assert len(annuler) == 1


def test_le_seuil_est_reglable() -> None:
    poser, _annuler, garder = reconcile(
        [_voulu(price=0.25)], [_vivant(price=0.20)],
        nous=frozenset({"O-1"}), hysteresis_ticks=10,
    )
    assert poser == [] and len(garder) == 1
