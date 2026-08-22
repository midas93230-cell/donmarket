"""Tests de la boucle de tenue de marché Polymarket.

Le client est remplacé par un double : la boucle doit être vérifiable sans
place de marché en face, et surtout sans qu'un test puisse poser un ordre réel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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
    poser, annuler, garder, _bloques = reconcile(
        [_voulu()], [_vivant()], nous=frozenset({"O-1"})
    )
    assert poser == [] and annuler == [] and len(garder) == 1


def test_un_ordre_vraiment_loin_du_prix_est_annule_et_repose() -> None:
    """RECALÉ le 2026-08-20 : ce test utilisait un écart d'UN pas, qui doit
    désormais être ignoré. C'est justement le comportement corrigé — recoter
    pour un pas renvoyait l'ordre en fin de file à chaque tour et l'empêchait
    d'être jamais servi. Voir les tests d'hystérésis plus bas."""
    poser, annuler, _, _bloques = reconcile(
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
    _poser, annuler, _garder, _bloques = reconcile(
        [], [etranger, a_nous], nous=frozenset({"O-1"})
    )
    assert [o.order_id for o in annuler] == ["O-1"]


def test_achat_et_vente_sont_des_cles_distinctes() -> None:
    poser, annuler, _, _bloques = reconcile(
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

    def place_limit_order(
        self, *, token_id, price, size, side, post_only=False, expiration=None
    ):
        self.poses.append((token_id, price, size, side, post_only, expiration))
        return _Recu(ok=self.ok, order_id=f"O-{len(self.poses)}")

    def cancel_orders(self, *, order_ids):
        self.annules.append(list(order_ids))
        return None


def _source(rungs=None, carnets=None):
    """La source rend (branches, rejets, CARNETS) depuis le 2026-08-21 : les
    sorties doivent pouvoir etre cotees pour des positions dont le marche n est
    plus eligible, donc absentes des branches."""
    return lambda: (
        rungs if rungs is not None else [_rung()],
        [],
        carnets or {},
    )


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
    poser, annuler, garder, _bloques = reconcile(
        [_voulu(price=0.21)], [_vivant(price=0.20)], nous=frozenset({"O-1"})
    )
    assert poser == [], "recotation declenchee pour un seul pas"
    assert annuler == []
    assert len(garder) == 1


def test_un_ecart_de_trois_pas_declenche_bien_la_recotation() -> None:
    """L'hysteresis ne doit pas devenir de l'immobilisme : quand le marche a
    vraiment bouge, un ordre reste loin du carnet ne sera jamais servi non
    plus, et il immobilise du capital pour rien."""
    poser, annuler, _garder, _bloques = reconcile(
        [_voulu(price=0.23)], [_vivant(price=0.20)], nous=frozenset({"O-1"})
    )
    assert len(poser) == 1 and poser[0].price == 0.23
    assert len(annuler) == 1


def test_le_seuil_est_reglable() -> None:
    poser, _annuler, garder, _bloques = reconcile(
        [_voulu(price=0.25)], [_vivant(price=0.20)],
        nous=frozenset({"O-1"}), hysteresis_ticks=10,
    )
    assert poser == [] and len(garder) == 1


def test_chaque_ordre_porte_lexpiration_demandee() -> None:
    """FILET DE SECURITE. Le nettoyage du `finally` ne s'execute pas si la
    machine s'eteint ou se met en veille : les ordres survivraient alors a la
    boucle qui les surveillait, et pourraient se remplir sans que personne
    n'ait decide de garder la position. Une expiration les fait mourir seuls,
    quoi qu'il arrive au processus."""
    client = _ClientDouble()
    run_making(
        client, _source(), bankroll=4.0, minutes=1, interval_s=30,
        max_markets=2, armed=True, expiration=1_800_000_000,
        sleep=lambda _s: None, now=_horloge(),
    )
    assert client.poses, "aucun ordre pose"
    assert all(pose[5] == 1_800_000_000 for pose in client.poses)


@dataclass
class _PositionReelle:
    """Forme REELLE d'une position, relevee le 2026-08-21."""

    token_id: str = "t1"
    size: float = 14.0
    avg_price: float = 0.13


def test_une_position_au_format_reel_est_lue() -> None:
    """MESURE COUTEUSE. Le lecteur cherchait `asset`/`asset_id` ; le SDK
    fournit `token_id`. La boucle s'est donc abstenue alors qu'elle detenait
    deux positions -- dont une gagnante -- et aucune n'a recu d'ordre de vente
    pendant qu'un match se jouait. « Je n'ai pas su lire » avait le bon
    comportement, mais pour un nom de champ."""
    inv, motif = read_inventory([_PositionReelle()])
    assert motif is None
    assert inv.held("t1") == pytest.approx(14.0)


# --- Le revient et l'echeance viennent du SDK -------------------------------


def test_le_prix_de_revient_et_l_echeance_sont_lus() -> None:
    """Sans ces deux champs le plancher de `exits()` ne s'applique JAMAIS en
    reel : il retomberait sur « revient inconnu » a chaque tour et coterait au
    carnet, exactement le comportement qui a perdu -0,16 sur le premier
    aller-retour. Les noms sont ceux MESURES sur le SDK le 2026-08-22.
    """

    @dataclass
    class _Position:
        token_id: str = "t1"
        size: float = 25.0
        avg_price: float = 0.11
        end_date: date = date(2026, 10, 1)

    inv, motif = read_inventory([_Position()])

    assert motif is None
    assert inv.cost_of("t1") == pytest.approx(0.11)
    echeance = inv.deadlines["t1"]
    assert (echeance.year, echeance.month, echeance.day) == (2026, 10, 1)
    assert echeance.tzinfo is not None


def test_une_position_sans_revient_reste_lisible() -> None:
    """Le revient est un BONUS, pas une condition : une position sans prix
    connu doit rester vendable au carnet plutot que suspendre la boucle."""

    @dataclass
    class _Position:
        token_id: str = "t1"
        size: float = 25.0

    inv, motif = read_inventory([_Position()])

    assert motif is None
    assert inv.held("t1") == pytest.approx(25.0)
    assert inv.cost_of("t1") is None


# --- Ordres survivants d'une session precedente ----------------------------


def test_une_cle_occupee_par_un_etranger_ne_recoit_pas_de_doublon() -> None:
    """LE DEFAUT TROUVE LE 2026-08-22, avant qu'il ne coute quoi que ce soit.

    `reconcile` ne peut annuler que les ordres de `nous`. Un ordre ETRANGER
    place sur la meme (jeton, sens) que ce qu'on veut poser mettait donc la
    boucle dans une impasse silencieuse : elle posait le sien SANS pouvoir
    retirer l'autre. Deux ordres de vente pour un seul inventaire -- on tentait
    de vendre le double de ce qu'on detient.

    Le cas n'est pas theorique : apres l'arret de la boucle du 22/08, deux
    ventes lui ont survecu au carnet, et la relancer les lui aurait rendues
    etrangeres.

    Regle : on ne pose JAMAIS sur une cle qu'on ne peut pas liberer.
    """
    etranger = _vivant(order_id="ETRANGER", price=0.20, side="SELL")
    poser, annuler, garder, bloques = reconcile(
        [_voulu(side="SELL", price=0.30)], [etranger], nous=frozenset()
    )
    assert poser == [], "un doublon a ete pose sur une cle non liberable"
    assert annuler == [] and garder == []
    assert len(bloques) == 1
    voulu, occupant = bloques[0]
    assert voulu.price == 0.30 and occupant.order_id == "ETRANGER"


def test_un_etranger_deja_au_bon_prix_ne_bloque_rien() -> None:
    """Un ordre etranger au prix qu'on voulait fait le travail. Le signaler
    comme bloquant crierait au loup a chaque tour."""
    etranger = _vivant(order_id="ETRANGER", price=0.30, side="SELL")
    poser, _annuler, garder, bloques = reconcile(
        [_voulu(side="SELL", price=0.30)], [etranger], nous=frozenset()
    )
    assert poser == [] and bloques == []
    assert len(garder) == 1


def test_les_ordres_adoptes_redeviennent_annulables() -> None:
    """La reprise apres relance : les identifiants retrouves sur disque
    rentrent dans `nous`, donc la boucle peut de nouveau recoter ses propres
    ordres au lieu de les subir."""
    survivant = _vivant(order_id="O-HIER", price=0.20, side="SELL")
    poser, annuler, _garder, bloques = reconcile(
        [_voulu(side="SELL", price=0.30)],
        [survivant],
        nous=frozenset({"O-HIER"}),
    )
    assert bloques == []
    assert len(poser) == 1 and poser[0].price == 0.30
    assert [o.order_id for o in annuler] == ["O-HIER"]


def test_la_boucle_adopte_les_ordres_dune_session_precedente() -> None:
    """REPRISE APRES RELANCE. Sans cela, ses propres ordres survivants lui
    reviennent ETRANGERS : elle ne peut plus les recoter, et la cle est
    bloquee. Les identifiants retrouves entrent dans `nous` des le depart.
    """
    survivant = _vivant(order_id="O-HIER", price=0.20, side="SELL", token="t1")
    client = _ClientDouble(ouverts=[survivant], positions=[])
    rapport = run_making(
        client,
        lambda: ([], [], {}),
        bankroll=0.0,
        minutes=0.01,
        interval_s=0.0,
        max_markets=1,
        armed=True,
        adopted={"O-HIER"},
        sleep=lambda _s: None,
    )
    # Adopte, donc nettoye a la fin comme n'importe lequel de ses ordres --
    # et surtout PAS compte comme etranger.
    assert rapport.foreign_seen == 0
    assert any("O-HIER" in lot for lot in client.annules)
