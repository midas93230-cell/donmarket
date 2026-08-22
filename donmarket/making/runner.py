"""La boucle de tenue de marché Polymarket — ce qui fait tourner le cœur.

`core.py` DÉCIDE : quelles branches sont cotables, et quel ordre poser sur
chacune. Ce module EXÉCUTE : il lit l'état réel, compare au voulu, envoie la
différence, et nettoie derrière lui.

## Trois leçons payées ailleurs, appliquées ici d'emblée

**On ne touche QUE nos propres ordres.** La boucle Binance traitait tout ordre
ouvert comme le sien et annulait ce qui n'était pas dans son plan. Le
2026-08-19, elle a visé trente fois de suite un ordre posé à la main par le
propriétaire du compte ; il n'a survécu que grâce à un bug d'annulation. Une
machine n'a pas à défaire ce qu'un humain a décidé, et « je ne l'ai pas
reconnu » n'est pas une raison de supprimer.

**Le nettoyage part de ce qu'on a POSÉ, pas du dernier relevé.** Un ordre posé
après la dernière lecture ne figure dans aucun relevé — c'est le cas de tout
ordre du dernier tour, donc le cas le plus fréquent. S'y fier le laissait vivant
au carnet après l'arrêt, ce que le `finally` existait précisément pour empêcher.

**Un ordre déjà au bon prix est GARDÉ, jamais rejoué.** Le réémettre lui ferait
perdre sa place dans la file, et la place dans la file est exactement ce qui
décide d'être rempli ou non. C'est le seul avantage d'un teneur arrivé tôt ;
le gaspiller à chaque tour reviendrait à n'être jamais servi.

## Ce que `post_only` change, et pourquoi il n'est pas négociable

Chaque ordre part avec `post_only=True` : le CLOB le REFUSE plutôt que de le
laisser traverser l'écart. Un teneur qui traverse devient preneur, paie les
frais au lieu de les éviter, et détruit la seule raison d'être de la stratégie.
Mieux vaut un ordre refusé qu'un ordre qui coûte.

## Ce que cette boucle ne sait toujours pas

Le taux de remplissage. Elle le MESURE — c'est même son premier objet. Aucun
rendement n'est annoncé tant qu'il n'est pas observé : la leçon du 2026-07-28
est que les écarts lus dans les carnets ne se retrouvent pas dans les
exécutions.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from typing import Sequence

from .core import DesiredOrder, Inventory, Rung, eligible, exits, plan

logger = logging.getLogger(__name__)

TICK = 0.01

# Nombre de pas d'écart toléré avant de recoter. MESURÉ le 2026-08-20 : sans
# hystérésis, la boucle a recoté presque à chaque tour (24 annulations pour 13
# ordres en 47 tours) et n'a rien été rempli en une heure. Chaque recotation
# renvoie l'ordre en fin de file ; la valeur 2 laisse un ordre y vieillir tant
# que le marché n'a pas vraiment bougé.
HYSTERESIS_TICKS = 2


@dataclass(frozen=True)
class LiveOrder:
    """Un ordre a nous, vivant au carnet."""

    order_id: str
    token_id: str
    side: str
    price: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.token_id, self.side.upper())


@dataclass
class MakingReport:
    """Ce que la boucle a fait, et ce qu'elle n'a pas su faire.

    `armed` y figure exprès : un rapport qui ne dit pas s'il décrit une
    répétition ou un engagement réel est un rapport dangereux.
    """

    armed: bool
    ticks: int = 0
    placed: int = 0
    refused: int = 0
    cancelled: int = 0
    kept: int = 0
    foreign_seen: int = 0
    fills: list[tuple[str, float, float]] = field(default_factory=list)
    rejects: tuple[tuple[str, str], ...] = ()
    problem: str | None = None
    left_open: tuple[str, ...] = ()
    # Positions qu on ne sait pas solder : carnet illisible, ou reliquat sous
    # le minimum d ordre. Les taire reviendrait a les abandonner une 2e fois.
    stranded: tuple[str, ...] = ()
    # Ventes cotees AU-DESSUS du carnet parce que celui-ci est passe sous le
    # prix de revient. Ce n est pas un echec -- c est le refus delibere de
    # realiser la perte -- mais ca doit se voir : ces positions ne partiront
    # pas tant que le carnet ne remonte pas, et il faut savoir combien on en
    # porte. (token_id, prix tenu, meilleur ask)
    held_above: tuple[tuple[str, float, float], ...] = ()


def reconcile(
    voulus: Sequence[DesiredOrder],
    vivants: Sequence[LiveOrder],
    *,
    nous: frozenset[str],
    hysteresis_ticks: int = HYSTERESIS_TICKS,
    tick: float = TICK,
) -> tuple[list[DesiredOrder], list[LiveOrder], list[LiveOrder]]:
    """Compare le voulu au vivant. Rend (à poser, à annuler, à garder).

    `nous` DÉLIMITE ce que la boucle a le droit de toucher : les identifiants
    des ordres qu'elle a elle-même posés. Tout le reste est ÉTRANGER — compté,
    journalisé, et laissé intact.

    HYSTÉRÉSIS, ajoutée le 2026-08-20 après la première heure armée. Le premier
    tour réel a rendu 13 ordres posés pour 24 annulés en 47 tours : la boucle
    recotait à chaque mouvement d'un seul pas, donc presque à chaque tour. Or
    recoter renvoie l'ordre en FIN DE FILE, et la place dans la file est le seul
    avantage d'un teneur. Un ordre qui ne reste jamais en place n'est jamais
    servi — zéro remplissage en une heure, ce qui n'était pas de la malchance
    mais la conséquence directe.

    On ne recote donc que si le prix voulu s'écarte de plus de
    `hysteresis_ticks` pas. Un écart d'un pas est ignoré : mieux vaut un ordre
    un pas trop bas qui vieillit dans la file qu'un ordre parfaitement placé
    qui repart de zéro toutes les minutes.
    """
    par_cle = {o.key: o for o in vivants}
    a_poser: list[DesiredOrder] = []
    a_garder: list[LiveOrder] = []
    utilises: set[tuple[str, str]] = set()
    seuil = hysteresis_ticks * tick - 1e-9

    for voulu in voulus:
        cle = (voulu.token_id, voulu.side.upper())
        vivant = par_cle.get(cle)
        if vivant is not None and abs(vivant.price - voulu.price) <= seuil:
            a_garder.append(vivant)
            utilises.add(cle)
            continue
        a_poser.append(voulu)

    a_annuler = [
        o for o in vivants if o.key not in utilises and o.order_id in nous
    ]
    return a_poser, a_annuler, a_garder


def flatten(paginator) -> list[object]:
    """Aplatit un paginateur du SDK en lignes.

    PIÈGE MESURÉ le 2026-08-20 : itérer un `Paginator` rend des PAGES, pas des
    lignes. `list(client.list_positions())` donnait donc `[<page>]` — soit une
    « position » illisible là où le compte n'en détenait aucune, ce qui faisait
    s'abstenir la boucle pour rien. Le contenu est dans `page.items`.
    """
    lignes: list[object] = []
    for page in paginator:
        lignes.extend(getattr(page, "items", ()) or ())
    return lignes


def read_live_orders(rows: Sequence[object]) -> list[LiveOrder]:
    """Lit les ordres ouverts. Une ligne illisible est IGNORÉE, pas devinée.

    Un ordre qu'on ne sait pas relire est un ordre qu'on ne saura pas annuler :
    il vaut mieux le savoir que d'en inventer les champs.
    """
    vivants: list[LiveOrder] = []
    for row in rows:
        try:
            # Les noms de champs varient d'une version du SDK a l'autre : on
            # essaie plutot que de supposer, et on ignore la ligne si aucun ne
            # colle -- un ordre qu'on ne sait pas relire est un ordre qu'on ne
            # saura pas annuler.
            jeton = next(
                (
                    str(getattr(row, n))
                    for n in ("asset_id", "token_id", "asset")
                    if getattr(row, n, None)
                ),
                None,
            )
            if jeton is None:
                continue
            vivants.append(
                LiveOrder(
                    order_id=str(getattr(row, "id", None) or getattr(row, "order_id")),
                    token_id=jeton,
                    side=str(getattr(row, "side")).upper().replace("SIDE.", ""),
                    price=float(getattr(row, "price")),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return vivants


def _as_datetime(valeur: object) -> datetime | None:
    """Normalise l'échéance du SDK en `datetime` UTC, ou rend None.

    Le SDK rend un `datetime.date` nu (mesuré le 2026-08-22), parfois une date
    bidon comme `1970-01-01`. On ne filtre pas ces dernières ici : une échéance
    passée doit se lire comme passée, et `exits()` en tire la bonne conclusion
    — liquider. Corriger la donnée à cet étage masquerait le fait.
    """
    if isinstance(valeur, datetime):
        return valeur if valeur.tzinfo else valeur.replace(tzinfo=timezone.utc)
    if isinstance(valeur, date):
        return datetime(valeur.year, valeur.month, valeur.day, tzinfo=timezone.utc)
    return None


def read_inventory(rows: Sequence[object]) -> tuple[Inventory, str | None]:
    """Lit les parts détenues. Rend aussi le MOTIF si c'est illisible.

    « Je ne détiens rien » et « je n'ai pas su lire » mènent à des décisions
    opposées : la première fait acheter, la seconde doit faire s'abstenir. Une
    machine qui achète sans savoir ce qu'elle détient ne sait pas revendre :
    elle accumule, et sur un marché de prédiction une position gardée jusqu'à
    la résolution vaut 0 ou 1, pas son prix.
    """
    inv = Inventory()
    inconnues = 0
    total = 0
    for row in rows:
        total += 1
        # `token_id` est le nom RÉEL, mesuré le 2026-08-21 sur les deux
        # premières positions remplies. Les autres restent essayés : les noms
        # varient d'une version du SDK à l'autre, et se tromper ici fait
        # s'abstenir la boucle alors qu'elle détient de quoi revendre — c'est
        # exactement ce qui est arrivé, et une position gagnante est restée
        # sans ordre de vente pendant qu'un match se jouait.
        jeton = (
            getattr(row, "token_id", None)
            or getattr(row, "asset", None)
            or getattr(row, "asset_id", None)
        )
        parts = getattr(row, "size", None)
        if parts is None:
            parts = getattr(row, "quantity", None)
        if jeton is None or parts is None:
            inconnues += 1
            continue
        # Le REVIENT et l'ÉCHÉANCE alimentent le plancher de `exits()`. Ils
        # sont facultatifs : sans eux la position reste vendable au carnet,
        # simplement sans protection. Les faire échouer ferait d'un champ
        # manquant une position abandonnée — la faute du 21/08.
        revient = getattr(row, "avg_price", None)
        try:
            revient = float(revient) if revient is not None else None
        except (TypeError, ValueError):
            revient = None

        try:
            inv.add(str(jeton), float(parts), avg_price=revient)
        except (TypeError, ValueError):
            inconnues += 1
            continue

        inv.set_deadline(str(jeton), _as_datetime(getattr(row, "end_date", None)))
    if inconnues:
        return inv, f"{inconnues} position(s) sur {total} illisibles — vente suspendue"
    return inv, None


def run_making(
    client,
    rungs_source,
    *,
    bankroll: float,
    minutes: float,
    interval_s: float,
    max_markets: int,
    armed: bool,
    expiration: int | None = None,
    sleep=time.sleep,
    now=time.monotonic,
) -> MakingReport:
    """La boucle. Rien ne part si `armed` est faux — même chemin, même plan.

    `rungs_source` est un appelable qui rend les branches cotables. Il est
    injecté plutôt qu'appelé en dur : la lecture de l'univers est asynchrone et
    lente, et l'isoler permet de tester la boucle sans place de marché.
    """
    rapport = MakingReport(armed=armed)
    par_marche = bankroll / max(max_markets, 1)
    a_nous: set[str] = set()
    debut = now()
    limite_s = minutes * 60

    try:
        while True:
            rapport.ticks += 1
            try:
                rungs: list[Rung]
                # La source rend AUSSI les carnets :  doit pouvoir
                # coter la sortie de positions dont le marche n est plus
                # eligible, donc absentes de .
                rungs, rejets, carnets = rungs_source()
                rapport.rejects = tuple(rejets[:20])
                vivants = read_live_orders(flatten(client.list_open_orders()))
                inventaire, motif = read_inventory(flatten(client.list_positions()))
            except Exception as exc:  # noqa: BLE001
                # Un relevé raté n'arrête pas la boucle : les ordres posés
                # restent au carnet et il faut continuer à les surveiller.
                logger.warning("relevé incomplet au tour %d : %s", rapport.ticks, exc)
                if now() - debut + interval_s > limite_s:
                    break
                sleep(interval_s)
                continue

            if motif is not None:
                logger.error("inventaire illisible (%s) — la boucle s'abstient", motif)
                rapport.problem = motif
                voulus: list[DesiredOrder] = []
            else:
                # LES SORTIES D'ABORD, et SANS CONDITION. Une position dont le
                # marché n'est plus éligible ne figure pas dans `rungs` et ne
                # recevrait donc jamais d'ordre de vente : c'est ainsi que
                # quatre positions sont tombées à zéro le 2026-08-21.
                # L'éligibilité gouverne l'achat, jamais la sortie.
                sorties, soucis = exits(inventaire, carnets)
                for jeton, souci in soucis:
                    logger.error("position %s : %s", jeton[:12], souci)
                rapport.stranded = tuple(j for j, _ in soucis)
                rapport.held_above = tuple(
                    (
                        o.token_id,
                        o.price,
                        float(getattr(carnets.get(o.token_id), "best_ask", 0.0) or 0.0),
                    )
                    for o in sorties
                    if o.held_above_book
                )

                achats = plan(
                    rungs,
                    inventaire,
                    notional_per_market=par_marche,
                    max_markets=max_markets,
                )
                # `plan` propose aussi des ventes pour les branches éligibles,
                # que les sorties couvrent déjà. On ne garde que les ACHATS,
                # sinon la même branche recevrait deux ordres de vente.
                voulus = sorties + [o for o in achats if o.side.upper() == "BUY"]

            etrangers = [o for o in vivants if o.order_id not in a_nous]
            rapport.foreign_seen = len(etrangers)
            if etrangers:
                logger.info(
                    "%d ordre(s) ouvert(s) ne viennent pas de cette boucle — "
                    "laissés intacts", len(etrangers)
                )

            a_poser, a_annuler, a_garder = reconcile(
                voulus, vivants, nous=frozenset(a_nous)
            )
            rapport.kept = len(a_garder)

            if armed and a_annuler:
                try:
                    client.cancel_orders(order_ids=[o.order_id for o in a_annuler])
                    rapport.cancelled += len(a_annuler)
                except Exception as exc:  # noqa: BLE001
                    logger.error("annulation refusée : %s", exc)

            for ordre in a_poser:
                if not armed:
                    rapport.placed += 1
                    continue
                try:
                    recu = client.place_limit_order(
                        token_id=ordre.token_id,
                        price=ordre.price,
                        size=ordre.size,
                        side=ordre.side,
                        # Non négociable : un teneur qui traverse l'écart
                        # devient preneur et paie au lieu d'éviter.
                        post_only=True,
                        # FILET DE SÉCURITÉ. Le nettoyage du `finally` ne
                        # s'exécute pas si la machine s'éteint ou se met en
                        # veille : les ordres survivraient alors à la boucle
                        # qui les surveillait, et pourraient se remplir sans
                        # que personne n'ait décidé de garder la position.
                        # Une expiration alignée sur la fin de la course les
                        # fait mourir seuls, quoi qu'il arrive au processus.
                        expiration=expiration,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ordre non passé sur %s : %s", ordre.token_id[:12], exc)
                    continue

                if not getattr(recu, "ok", False):
                    # Un refus n'est pas une panne : `post_only` refuse
                    # exactement quand il doit, et c'est ce qu'on veut.
                    rapport.refused += 1
                    continue
                rapport.placed += 1
                identifiant = getattr(recu, "order_id", None)
                if identifiant:
                    a_nous.add(str(identifiant))
                else:
                    logger.error(
                        "ordre posé sur %s mais identifiant illisible — "
                        "il faudra l'annuler à la main", ordre.token_id[:12]
                    )

            if now() - debut + interval_s > limite_s:
                break
            sleep(interval_s)
    finally:
        # On part de CE QU'ON A POSÉ, jamais du dernier relevé : un ordre posé
        # après la dernière lecture n'y figure pas. Redemander l'annulation
        # d'un ordre déjà rempli est sans danger — le serveur refuse.
        if armed and a_nous:
            try:
                client.cancel_orders(order_ids=sorted(a_nous))
                rapport.cancelled += len(a_nous)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "NETTOYAGE FINAL REFUSÉ (%s) — des ordres sont peut-être "
                    "ENCORE au carnet, à vérifier sur polymarket.com", exc
                )
                rapport.left_open = tuple(sorted(a_nous))

    return rapport
