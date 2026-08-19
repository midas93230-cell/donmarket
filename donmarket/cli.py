"""Ligne de commande de DONmarket.

    python -m donmarket scan --mode serieux
    python -m donmarket scan --mode normal --max-markets 300 --bankroll 14.47
    python -m donmarket rewards --bankroll 100
    python -m donmarket serve --bankroll 100
    python -m donmarket stats
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Sequence

from .analysis.opportunities import Mode, Opportunity, affordable
from .analysis.rewards import RewardCandidate, allocate
from .backtest.replay import DEFAULT_MAX_INVENTORY
from .backtest.runner import DEFAULT_MARKETS, BacktestReport, run_backtest
from .api import clob
from .config import SETTINGS
from .consensus.survey import ConsensusSurvey, survey_consensus
from .execute.engine import ExecutionRefused, execute_plan
from .execute.limits import ExecutionLimits
from .execute.orders import plan_portfolio
from .paper.fills import RestingOrder
from .paper.live import DEFAULT_INTERVAL_SECONDS, run_session
from .paper.session import PaperMarket, PaperSession, SessionSnapshot
from .scan.rewards_scan import HISTORY_BUDGET, RewardScanResult, scan_rewards
from .scan.scanner import ScanResult, full_scan
from .store import db
from .web import server as web_server

SEPARATOR = "─" * 78

# Taille d'ordre minimale la plus courante sur Polymarket (en parts).
COMMON_MIN_ORDER_SIZE = 5.0


def force_utf8_console() -> None:
    """Force la sortie en UTF-8, quelle que soit la page de code du terminal.

    Sur une console Windows en cp1252 — le défaut sur un Windows français —
    `print` lève `UnicodeEncodeError` au premier caractère hors Latin-1 : le
    séparateur du rapport, mais aussi les flèches et les motifs de rejet. Le
    rapport meurt alors en plein milieu, après avoir déjà imprimé des lignes,
    ce qui donne l'impression que le balayage a échoué alors que la mesure est
    intacte et que seul l'affichage a lâché — un scan de 50 s perdu à l'écriture.

    `errors="replace"` est délibéré : si la reconfiguration ne suffisait pas,
    un caractère de remplacement vaut mieux qu'un rapport tronqué.
    """
    for stream in (sys.stdout, sys.stderr):
        # `capsys` et les redirections de test remplacent les flux par des
        # objets sans `reconfigure` : l'absence n'est pas une anomalie.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # flux détaché ou non textuel
            pass


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _format_opportunity(opp: Opportunity, *, index: int) -> str:
    days = "?" if opp.days_to_resolution is None else f"{opp.days_to_resolution:.0f}j"
    return (
        f"{index:>3}. [{opp.kind}] marge nette {opp.edge:+.4f} $/jeu  "
        f"somme={opp.sum_price:.4f}  profondeur={opp.depth_usd:,.0f} $  "
        f"spread={opp.spread:.4f}  vol24h={opp.volume_24h:,.0f} $  fin dans {days}\n"
        f"     {opp.question[:90]}\n"
        f"     gain théorique sur la taille dispo : {opp.profit_at:,.2f} $"
    )


def _print_report(result: ScanResult, bankroll: float | None) -> None:
    print(SEPARATOR)
    print(f"SCAN DONmarket — mode {result.mode.value.upper()}")
    print(SEPARATOR)
    print(f"Marchés lus            : {result.markets_seen:,}")
    print(f"Marchés négociables    : {result.markets_tradable:,}")
    print(f"Carnets d'ordres reçus : {result.books_fetched:,}")
    print(f"Durée                  : {result.duration_seconds:.1f} s")
    print(f"Opportunités retenues  : {result.found}")
    print(SEPARATOR)

    if result.opportunities:
        print("\nOPPORTUNITÉS RETENUES\n")
        for index, opp in enumerate(result.opportunities[:20], start=1):
            print(_format_opportunity(opp, index=index))
            if bankroll is not None:
                ok = affordable(opp, bankroll, min_order_size=COMMON_MIN_ORDER_SIZE)
                verdict = "ACCESSIBLE" if ok else "HORS DE PORTÉE"
                need = opp.sum_price * COMMON_MIN_ORDER_SIZE
                print(
                    f"     avec {bankroll:.2f} $ : {verdict} "
                    f"(entrée minimale ≈ {need:.2f} $)"
                )
            print()
    else:
        print("\nAucune opportunité ne franchit les seuils.")
        print("Ce n'est pas une panne : c'est le résultat de la mesure.\n")

    if result.near_misses:
        print("LES PLUS PROCHES DU SEUIL (rejetées, et pourquoi)\n")
        for index, opp in enumerate(result.near_misses, start=1):
            print(
                f"{index:>3}. [{opp.kind}] marge brute {opp.gross_edge:+.4f} $  "
                f"somme={opp.sum_price:.4f}  {opp.question[:60]}"
            )
            print(f"     rejetée par : {', '.join(opp.rejected_by)}")
        print()


def _share_percent(candidate: RewardCandidate) -> float:
    """La part du pool qui nous revient, en pourcent.

    Affichée plutôt que le score concurrent seul : un score n'a pas d'échelle
    absolue lisible (il dépend de la taille des ordres ET de leur distance au
    milieu), alors qu'une part se compare d'un marché à l'autre et se relie
    directement au pool en dollars affiché juste avant.
    """
    if candidate.own_q <= 0:
        return 0.0
    return 100.0 * candidate.own_q / (candidate.competing_q + candidate.own_q)


def _format_candidate(candidate: RewardCandidate, *, index: int) -> str:
    """Affiche le NET en premier : c'est le seul chiffre sur lequel décider.

    Le brut et la dérive restent visibles à côté, parce qu'un net positif obtenu
    d'un gros pool très disputé n'a pas le même sens qu'un net positif obtenu
    d'un petit pool désert — le second est reproductible, le premier non.
    """
    hours = "?" if candidate.hours_left is None else f"{candidate.hours_left:.0f} h"
    return (
        f"{index:>3}. NET {candidate.net_yield:+.2f} %/jour  "
        f"= {candidate.daily_usd:+.2f} $/jour sur {candidate.engaged_usd:.0f} $\n"
        # Le coût retenu ET sa source : lire « risque 0,00 » sans savoir si
        # c'est la dérive ou le rejeu qui l'a produit empêche de contester le
        # chiffre, et c'est justement celui qui décide du classement.
        f"     brut {candidate.gross_yield:.2f} % − risque "
        f"{-candidate.inventory_cost:.2f} % ({_cost_source(candidate)})  "
        f"pool {candidate.daily_pool:,.0f} $/j  "
        f"part {_share_percent(candidate):.1f} % du pool "
        # Une décimale : arrondir à l'entier affichait « 0 pts contre 0 » sur
        # les marchés déserts, où notre score vaut 0,4 — donc « part 100 % »
        # avec un numérateur qui a l'air nul. C'est peu, et il faut le VOIR :
        # un seul teneur qui arrive dilue une part pareille à néant.
        f"({candidate.own_q:,.1f} pts contre {candidate.competing_q:,.1f})  "
        f"fin dans {hours}\n"
        f"     {candidate.question[:90]}"
    )


def _print_rewards_report(result: RewardScanResult) -> None:
    print(SEPARATOR)
    print(f"RÉCOMPENSES DE LIQUIDITÉ — mode {result.mode.value.upper()}")
    print(SEPARATOR)
    print(f"Marchés lus              : {result.markets_seen:,}")
    print(f"dont récompensés         : {result.rewarded:,}")
    print(f"dont ≥ 24 h restantes    : {result.alive:,}")
    print(f"dont finançables         : {result.affordable:,}  (capital {result.bankroll:.2f} $)")
    print(f"Carnets reçus            : {result.books_fetched:,}")
    print(f"Historiques 24 h reçus   : {result.histories_fetched:,}")
    print(f"Durée                    : {result.duration_seconds:.1f} s")
    print(f"Candidats retenus        : {result.found}")
    print(SEPARATOR)

    if result.candidates:
        print("\nCANDIDATS RETENUS\n")
        for index, candidate in enumerate(result.candidates[:20], start=1):
            print(_format_candidate(candidate, index=index))
            print()

        # Chaque candidat tient dans le capital pris isolément ; leur somme,
        # non. Ne totaliser que ce qui est finançable EN MÊME TEMPS.
        held = allocate(result.candidates, bankroll=result.bankroll)
        engaged = sum(c.engaged_usd for c in held)
        total = sum(c.daily_usd for c in held)
        print(
            f"Tenable simultanément avec {result.bankroll:.2f} $ : "
            f"{len(held)} position(s) sur {result.found}, "
            f"{engaged:.2f} $ engagés → {total:+.2f} $/jour"
        )
        if len(held) < result.found:
            print(
                "Les autres sont hors budget une fois les premiers pris : "
                "leur ticket est imposé par `rewardsMinSize`, on ne peut pas "
                "en prendre une fraction."
            )
        print(
            "Le « risque » est le PIRE de deux mesures sur les mêmes 24 h : la\n"
            "dérive bout-à-bout, et le rejeu de la cotation minute par minute.\n"
            "Aucune ne majore l'autre — la dérive rate les allers-retours, le\n"
            "rejeu rate les tendances lentes. Le net affiché n'est donc PAS un\n"
            "plancher : c'est une estimation, démentie sur 6 marchés cotés sur\n"
            "17 le 01/08/2026. Et rien de tout cela n'est un ordre passé.\n"
        )
    else:
        print("\nAucun marché récompensé ne franchit les seuils.")
        print("Ce n'est pas une panne : c'est le résultat de la mesure.\n")

    if result.near_misses:
        print("LES PLUS PROCHES DU SEUIL (rejetés, et pourquoi)\n")
        for index, candidate in enumerate(result.near_misses, start=1):
            print(
                f"{index:>3}. net {candidate.net_yield:+7.2f} %/j  "
                f"{candidate.question[:60]}"
            )
            print(f"     rejeté par : {', '.join(candidate.rejected_by)}")
        print()


async def _run_rewards(args: argparse.Namespace) -> int:
    mode = Mode.SERIEUX if args.mode == "serieux" else Mode.NORMAL
    result = await scan_rewards(
        mode=mode,
        bankroll=args.bankroll,
        history_budget=args.history_budget,
        persist=not args.no_persist,
    )
    _print_rewards_report(result)
    return 0


def _print_execution_report(result, plan) -> None:
    print(SEPARATOR)
    print("EXÉCUTION" if result.armed else "EXÉCUTION À BLANC — MOTEUR NON ARMÉ")
    print(SEPARATOR)

    if not result.armed:
        print(
            "Aucun ordre n'est parti. Le portier a tourné exactement comme il\n"
            "tournerait armé : ce qui suit est ce qui SERAIT envoyé.\n"
        )

    print(f"Ordres planifiés         : {len(plan.orders)}")
    print(f"Retenus par le portier   : {len(result.sent)}")
    print(f"Refusés par les plafonds : {len(result.refused)}")
    if result.armed:
        print(f"Acceptés par le CLOB     : {result.accepted_count}")
        print(f"Échecs CLOB              : {len(result.failed)}")
        print(f"Capital réellement engagé: {result.engaged_usd:.2f} $")

    if result.sent:
        print("\nORDRES")
        for order in result.sent:
            marque = "OK " if order.accepted else ("—  " if not result.armed else "!! ")
            print(
                f"  {marque}{order.side:<4} {order.size:>8.1f} parts @ "
                f"{order.price:.3f} = {order.cost_usd:>8.2f} $  "
                f"{order.condition_id[:18]}"
            )

    # Les refus sont imprimés AVEC leur motif : un ordre qui disparaît en
    # silence se lit comme un bug de stratégie alors que c'est un plafond qui a
    # fait son travail.
    if result.refused:
        print("\nREFUSÉS PAR LES PLAFONDS")
        for order, reason in result.refused:
            print(f"  · {getattr(order, 'question', '')[:40]:<40} {reason}")

    if getattr(result, "failed", ()):
        print("\nREFUSÉS PAR LE CLOB")
        for order, reason in result.failed:
            print(f"  · {getattr(order, 'question', '')[:40]:<40} {reason}")

    if plan.skipped:
        print("\nÉCARTÉS À LA PLANIFICATION")
        for reason in plan.skipped[:10]:
            print(f"  · {reason}")


async def _run_trade(args: argparse.Namespace) -> int:
    mode = Mode.SERIEUX if args.mode == "serieux" else Mode.NORMAL

    try:
        limits = ExecutionLimits(
            max_total_usd=args.max_total,
            max_per_market_usd=args.max_per_market,
            max_orders=args.max_orders,
        )
    except ValueError as exc:
        print(f"Plafonds incohérents : {exc}")
        return 2

    result_scan = await scan_rewards(
        mode=mode, bankroll=args.max_total, history_budget=args.history_budget
    )
    if not result_scan.candidates:
        print("Aucun candidat retenu : rien à coter.")
        return 0

    # Carnets FRAIS pour les seuls candidats retenus : le balayage a pu prendre
    # une minute, et coter sur un carnet périmé pose des prix qui n'existent
    # plus. C'est peu d'appels puisque la liste est déjà filtrée.
    token_ids = [tid for c in result_scan.candidates for tid in c.token_ids]
    books = await clob.fetch_books(token_ids)

    plan = plan_portfolio(
        result_scan.candidates, books, bankroll=min(args.max_total, args.bankroll)
    )
    if not plan.orders:
        print("Aucun ordre planifiable sur les candidats retenus.")
        for reason in plan.skipped[:10]:
            print(f"  · {reason}")
        return 0

    try:
        result = execute_plan(plan.orders, limits=limits, armed=args.arm)
    except ExecutionRefused as exc:
        print(SEPARATOR)
        print("EXÉCUTION REFUSÉE")
        print(SEPARATOR)
        print(f"{exc}\n")
        print("Voir .env.example pour les variables attendues.")
        return 2

    _print_execution_report(result, plan)
    return 0


def _print_tick(snap: SessionSnapshot) -> None:
    """Une ligne par tour. Le solde d'abord, sa décomposition ensuite."""
    move = snap.pnl_usd
    arrow = "▲" if move > 0 else ("▼" if move < 0 else "=")
    print(
        f"  {snap.elapsed_seconds:6.0f}s  SOLDE {snap.equity_usd:12,.2f} $  "
        f"{arrow} {move:+9.2f} $ ({snap.pnl_pct:+6.3f} %)  "
        f"cash {snap.cash_usd:10,.2f}  inv {snap.inventory_usd:9,.2f}  "
        f"récomp {snap.rewards_usd:7.4f}  rempl {snap.fills}",
        flush=True,
    )


def _paper_markets(
    candidates: Sequence[RewardCandidate], condition_ids: set[str]
) -> tuple[PaperMarket, ...]:
    """Les marchés effectivement cotés, avec de quoi recalculer leur score."""
    return tuple(
        PaperMarket(
            condition_id=candidate.condition_id,
            question=candidate.question,
            token_ids=candidate.token_ids,
            max_spread=candidate.max_spread,
            daily_pool=candidate.daily_pool,
            # `engaged_usd` porte `rewardsMinSize` en PARTS — même convention
            # que `plan_orders`, où le jeu complet vaut 1 $.
            min_size=candidate.engaged_usd,
        )
        for candidate in candidates
        if candidate.condition_id in condition_ids
    )


async def _run_paper(args: argparse.Namespace) -> int:
    mode = Mode.SERIEUX if args.mode == "serieux" else Mode.NORMAL

    result_scan = await scan_rewards(
        mode=mode, bankroll=args.bankroll, history_budget=args.history_budget
    )
    if not result_scan.candidates:
        print("Aucun candidat retenu : rien à coter.")
        return 0

    token_ids = [tid for c in result_scan.candidates for tid in c.token_ids]
    books = await clob.fetch_books(token_ids)
    plan = plan_portfolio(result_scan.candidates, books, bankroll=args.bankroll)
    if not plan.orders:
        print("Aucun ordre planifiable sur les candidats retenus.")
        for reason in plan.skipped[:10]:
            print(f"  · {reason}")
        return 0

    quoted = {order.condition_id for order in plan.orders}
    session = PaperSession.opening(
        bankroll=args.bankroll,
        markets=_paper_markets(result_scan.candidates, quoted),
        orders=tuple(
            RestingOrder(token_id=o.token_id, price=o.price, size=o.size)
            for o in plan.orders
        ),
    )

    print(SEPARATOR)
    print(f"DÉMONSTRATION — {args.bankroll:,.2f} $ DE CAPITAL FICTIF")
    print(SEPARATOR)
    print(
        f"{len(session.orders)} ordres posés sur {len(session.markets)} marchés, "
        f"{plan.notional:,.2f} $ engagés.\n"
        f"Durée {args.minutes:.0f} min, relevé toutes les {args.interval:.0f} s.\n"
        "Aucun ordre n'est envoyé : les remplissages sont déduits des VRAIES\n"
        "exécutions du marché, en nous supposant toujours DERNIERS dans la file.\n"
    )

    final = await run_session(
        session,
        duration_seconds=args.minutes * 60.0,
        interval_seconds=args.interval,
        on_tick=_print_tick,
    )

    print(f"\n{SEPARATOR}")
    print("RÉSULTAT DE LA DÉMONSTRATION")
    print(SEPARATOR)
    print(f"Capital de départ        : {final.account.starting_usd:12,.2f} $")
    print(f"Solde final              : {final.equity:12,.2f} $")
    print(f"  dont cash              : {final.account.cash_usd:12,.2f} $")
    print(f"  dont inventaire        : {final.account.inventory_value(final.marks):12,.2f} $")
    print(f"Récompenses accumulées   : {final.account.rewards_usd:12,.4f} $")
    print(f"RÉSULTAT                 : {final.pnl:+12,.2f} $  ({final.pnl_pct:+.3f} %)")
    print(f"Remplissages             : {len(final.account.fills)}")
    print(f"Ordres entièrement servis: {final.filled_orders} / {len(final.orders)}")
    if final.rejected_for_cash:
        print(f"Remplissages refusés (cash insuffisant) : {final.rejected_for_cash}")

    print(
        "\nLa récompense ne paie que les ordres ENCORE POSÉS, au prix où ils ont\n"
        "été posés : un ordre servi a quitté le carnet, un ordre que le milieu a\n"
        "distancé est sorti de la bande. Un total qui cesse de monter n'est donc\n"
        "pas une panne — c'est la position qui a changé sous nos ordres."
    )
    if not final.account.fills:
        print(
            "\nAucun remplissage. Ce n'est pas une panne non plus : nos ordres sont\n"
            "posés DERRIÈRE toute la file existante, et il faut qu'un vendeur écoule\n"
            "plus de parts que la file n'en contient pour qu'il nous en reste."
        )
    return 0


def _print_consensus_report(survey: ConsensusSurvey) -> None:
    print(SEPARATOR)
    print(f"VOTE D'ENSEMBLE — {survey.members} membres, seuil {survey.threshold}")
    print(SEPARATOR)
    print(f"Marchés mesurés          : {survey.markets}")
    print(f"Durée                    : {survey.duration_seconds:.1f} s")

    if not survey.effectives:
        print("\nAucune série mesurable : les membres n'ont jamais varié.")
        return

    correlation = survey.median_correlation or 0.0
    effective = survey.median_effective or 0.0
    print(f"Corrélation entre membres: {correlation:+.3f} (médiane)")
    print(f"VOTES INDÉPENDANTS       : {effective:.1f} sur {survey.members}")
    print(SEPARATOR)

    print("\nÀ QUEL SEUIL CE VOTE DÉCIDE-T-IL QUELQUE CHOSE ?\n")
    for threshold, rate in sorted(survey.by_threshold.items(), reverse=True):
        bar = "█" * int(rate * 40)
        print(f"  {threshold:>2}/{survey.members} : {rate * 100:6.1f} %  {bar}")

    print(
        "\nLire ce tableau avant toute autre chose. Un seuil qui n'est jamais\n"
        "atteint ne peut ni gagner ni perdre : il ne fait rien. Un seuil atteint\n"
        "presque toujours ne filtre rien. S'il n'existe aucun palier entre les\n"
        "deux, le vote à supermajorité n'apporte pas ce qu'il promet."
    )


async def _run_consensus(args: argparse.Namespace) -> int:
    survey = await survey_consensus(
        markets=args.markets,
        threshold=args.threshold,
        step=args.step,
        size=args.members,
    )
    _print_consensus_report(survey)
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    mode = Mode.SERIEUX if args.mode == "serieux" else Mode.NORMAL
    web_server.serve(
        bankroll=args.bankroll,
        port=args.port,
        mode=mode,
        history_budget=args.history_budget,
    )
    return 0


async def _run_scan(args: argparse.Namespace) -> int:
    mode = Mode.SERIEUX if args.mode == "serieux" else Mode.NORMAL
    result = await full_scan(
        mode=mode, max_markets=args.max_markets, persist=not args.no_persist
    )
    _print_report(result, args.bankroll)
    return 0


def _cost_source(candidate) -> str:
    """D'où vient le coût retenu : la dérive, le rejeu, ou rien."""
    if candidate.replay_cost is None:
        return "dérive, sans historique"
    if candidate.replay_cost <= -candidate.drift:
        return "rejeu"
    return "dérive"


def _print_backtest_report(report: BacktestReport) -> None:
    print(SEPARATOR)
    print("REJEU DE LA TENUE DE MARCHÉ — le majorant tient-il ?")
    print(SEPARATOR)
    print(f"Marchés lus              : {report.markets_seen:,}")
    print(f"Récompensés              : {report.rewarded:,}")
    print(
        f"Historiques récupérés    : {report.histories_fetched:,}"
        f" / {report.histories_requested:,} demandés"
    )
    print(f"Marchés rejoués          : {report.replayed:,}")
    print(f"Événements distincts     : {report.events_covered:,}")
    print(f"Durée                    : {report.duration_seconds:.1f} s")

    if not report.replays:
        print("\nAucun marché rejouable : pas d'historique exploitable.")
        return

    # Les réserves passent AVANT les chiffres : lues après, elles ne servent
    # qu'à excuser une conclusion déjà formée.
    complaints = report.sample_complaints
    if complaints:
        print(SEPARATOR)
        print("CE RÉSULTAT N'EST PAS UNE MESURE — l'échantillon ne le permet pas :")
        for complaint in complaints:
            print(f"  · {complaint}")
        print(
            "\nLes chiffres suivent, pour diagnostic seulement. Relancez sur un\n"
            "échantillon complet avant d'en conclure quoi que ce soit."
        )

    total = report.replayed
    holds = report.majorant_holds_count
    print(SEPARATOR)
    print(f"Résultat SUPPOSÉ (−dérive), médiane : {report.median_assumed:+8.2f} %/jour")
    print(f"Résultat RÉALISÉ (rejeu),   médiane : {report.median_realized:+8.2f} %/jour")
    print(f"Aller-retours, médiane              : {report.median_round_trips:8.1f}")
    print(
        f"Marchés où le majorant TIENT        : {holds}/{total} "
        f"({holds / total * 100:.0f} %)"
    )
    print(
        f"Marchés réellement gagnants         : {report.profitable_count}/{total} "
        f"({report.profitable_count / total * 100:.0f} %)"
    )
    print(SEPARATOR)

    # Le taux global est trompeur seul : un marché jamais rempli affiche un
    # réalisé de 0,00 et fait « tenir » le majorant sans l'avoir éprouvé.
    active = len(report.active)
    print(f"\nDont marchés RÉELLEMENT COTÉS (au moins un aller-retour) : {active}/{total}")
    if active:
        holds = report.active_holds_count
        print(
            f"  Majorant tenu sur ceux-là         : {holds}/{active} "
            f"({holds / active * 100:.0f} %)"
        )
        print(
            f"  Écart médian du modèle            : "
            f"{report.median_error_active:+8.2f} points/jour"
        )
    else:
        print("  Aucun : le taux global ci-dessus n'éprouve rien.")

    print("\nLES CINQ OÙ LE MODÈLE SE TROMPE LE PLUS (réalisé pire que supposé)\n")
    for index, replay in enumerate(report.replays[:5], start=1):
        print(
            f"{index:>3}. supposé {replay.assumed_pnl_pct:+7.2f}  "
            f"réalisé {replay.realized_pnl_pct:+7.2f}  "
            f"écart {replay.error:+7.2f}  {replay.question[:44]}"
        )

    print(
        "\nComment lire : « supposé » est ce que `rewards` retranche aujourd'hui du\n"
        "rendement ; « réalisé » est ce que la cotation aurait vraiment rapporté ou\n"
        "coûté sur le chemin de prix. Si le majorant tient partout, le net affiché\n"
        "est un plancher et il est trop sévère. S'il cède, les candidats retenus\n"
        "l'ont été sur un chiffre trop flatteur — et c'est le classement entier qui\n"
        "est à refaire.\n"
        "\nCe rejeu ne compte AUCUNE récompense : le carnet passé n'existe pas, donc\n"
        "la part du pool non plus. Il mesure le coût, pas le gain.\n"
    )


async def _run_backtest(args: argparse.Namespace) -> int:
    report = await run_backtest(
        markets_limit=args.markets, max_inventory=args.max_inventory
    )
    _print_backtest_report(report)
    return 0


def _print_tracks(tracks: Sequence[db.CandidateTrack], *, minimum: int) -> None:
    print(SEPARATOR)
    print("PERSISTANCE DES CANDIDATS — un marché, plusieurs balayages")
    print(SEPARATOR)

    if not tracks:
        print(
            f"\nAucun marché revu au moins {minimum} fois.\n"
            "Relancez `rewards` à quelques minutes d'intervalle : la série se\n"
            "construit un balayage à la fois, elle ne s'invente pas.\n"
        )
        return

    print(f"{'vus':>4} {'agit':>5} {'net méd.':>9} {'min':>8} {'max':>8}  marché")
    for track in tracks:
        print(
            f"{track.observations:>4} "
            f"{track.actionable_rate * 100:>4.0f}% "
            f"{track.net_median:>+8.2f} "
            f"{track.net_min:>+8.2f} "
            f"{track.net_max:>+8.2f}  "
            f"{track.question[:48]}"
        )

    print(
        "\n« agit » est la part des relevés où ce marché franchissait les seuils.\n"
        "L'écart min–max est la mesure honnête : si un candidat oscille de −20 à\n"
        "+80 %/jour, le +80 lu dans un balayage n'était pas un rendement, c'était\n"
        "un tirage. Seul un net médian positif ET une amplitude serrée décrit\n"
        "quelque chose qu'on pourrait tenir.\n"
    )


def _run_history(args: argparse.Namespace) -> int:
    with db.connect() as connection:
        tracks = db.candidate_tracks(
            connection, min_observations=args.min_observations, limit=args.limit
        )
    _print_tracks(tracks, minimum=args.min_observations)
    return 0


def _run_stats(_: argparse.Namespace) -> int:
    with db.connect() as connection:
        totals = db.counts(connection)
        print(SEPARATOR)
        print(f"BASE LOCALE — {SETTINGS.db_path}")
        print(SEPARATOR)
        for table, count in totals.items():
            print(f"{table:<16}: {count:,}")

        rows = connection.execute(
            """
            SELECT started_at, mode, markets_seen, markets_traded,
                   books_fetched, found
            FROM scans WHERE finished_at IS NOT NULL
            ORDER BY id DESC LIMIT 5
            """
        ).fetchall()
        if rows:
            print("\nDerniers scans :")
            for row in rows:
                print(
                    f"  {row['started_at'][:19]}  mode={row['mode']:<8} "
                    f"lus={row['markets_seen']:<6} négociables={row['markets_traded']:<6} "
                    f"carnets={row['books_fetched']:<6} retenues={row['found']}"
                )
    return 0


async def _run_predictfun(args: argparse.Namespace) -> int:
    """Balaie Predict.fun et rend compte, réserves d'abord.

    Volontairement séparé de `_run_scan` : les deux places n'ont ni le même
    modèle de carnet ni le même modèle de récompense, et un rapport commun
    ferait croire que les chiffres se comparent.
    """
    from .predictfun.rebates import MAKER_REBATE_SHARE, REBATE_TRIAL_ENDS
    from .predictfun.scan import scan_predictfun

    result = await scan_predictfun(
        network=args.network,
        bankroll=args.bankroll,
        include_closed=args.include_closed,
    )

    print(SEPARATOR)
    print(f"PREDICT.FUN — réseau {result.network}, balayé le {result.scanned_at}")
    print(SEPARATOR)

    # Les réserves AVANT les chiffres : un rapport dont on lit les limites
    # après les nombres est un rapport dont on ne lit pas les limites.
    for note in result.complaints():
        print(f"  ⚠ {note}")

    print(
        f"\nRécompense mesurée : le teneur touche {MAKER_REBATE_SHARE:.0%} des frais "
        f"du preneur SUR CHAQUE EXÉCUTION (essai jusqu'au {REBATE_TRIAL_ENDS})."
    )
    print(
        "  rebate/part = 0,25 × taux × min(p, 1−p)  →  maximum à p = 0,50, "
        "quasi nul aux extrêmes."
    )
    print(
        "  Ce n'est PAS le modèle Polymarket : aucun pool partagé, aucune prime "
        "de proximité au milieu, aucune dilution par la concurrence."
    )

    print(
        f"\n{len(result.page.markets)} marchés distincts vus, "
        f"{len(result.candidates)} cotables, {len(result.rejections)} écartés."
    )

    if result.candidates:
        print(
            f"\n{'marché':>7}  {'milieu':>7} {'écart':>7} {'rebate/part':>12} "
            f"{'%exéc':>7} {'seuil adv.':>11} {'ticket':>8}  titre"
        )
        for c in result.candidates:
            spread = f"{c.spread:.4f}" if c.spread is not None else "   -  "
            ticks = f"{c.breakeven_ticks:.2f} pas" if c.breakeven_ticks else "   -   "
            badge = "rebate?" if c.rebate_eligible_guess else "       "
            print(
                f"{c.market.market_id:>7}  {c.reference_price:>7.3f} {spread:>7} "
                f"{c.rebate_per_share:>12.6f} {c.rebate_yield_on_fill:>6.3%} "
                f"{ticks:>11} {c.entry_ticket_usd:>7.2f}$  {badge} {c.market.title[:38]}"
            )

        # Vérification en direct de l'identité structurelle, plutôt que promesse.
        sums = [c.full_set_ask_sum for c in result.candidates if c.full_set_ask_sum]
        if sums:
            print(
                f"\nJeu complet (ask Yes + ask No) : min {min(sums):.4f}, "
                f"max {max(sums):.4f} — vaut 1 + écart par construction, donc "
                "l'arbitrage n'a pas de zéro ici. Vérifié à chaque balayage."
            )

    # Pont inter-places : Predict.fun publie lui-même le conditionId Polymarket
    # équivalent sur une partie de ses marchés. C'est le seul lien structurel
    # entre les deux carnets, et DONmarket possède déjà l'autre moitié.
    from .predictfun.crossvenue import describe, resolve_twins

    linked = [m for m in result.page.markets if m.polymarket_condition_ids]
    if linked:
        mids = {c.market.market_id: c.reference_price for c in result.candidates}
        quotes = await resolve_twins(linked, mids)
        print(
            f"\nPont Polymarket : {len(linked)} marché(s) publient un jumeau, "
            f"{len(quotes)} résolu(s)."
        )
        for line in describe(quotes):
            print(f"  {line}")

    if args.show_rejects and result.rejections:
        print("\nÉcartés :")
        for rejection in result.rejections:
            print(f"  {rejection.market_id:>7}  {rejection.reason}")

    print(
        "\nAucun ordre n'a été passé : ce module est en LECTURE SEULE. Écrire sur "
        "Predict.fun exige une signature de portefeuille BNB Chain, non branchée."
    )
    return 0


def _run_seal(args: argparse.Namespace) -> int:
    """Scelle un secret avec DPAPI pour le coller dans le `.env`.

    La valeur est demandée par saisie MASQUÉE, jamais passée en argument : la
    ligne de commande d'un processus est lisible par tout le système, et reste
    dans l'historique du terminal.
    """
    import getpass

    from .store.vault import VaultError, is_available, seal

    if not is_available():
        print("Le scellement DPAPI n'existe que sous Windows.")
        print("Ailleurs : garder le .env en clair et restreindre ses permissions.")
        return 1

    print("Scellement DPAPI (portée : ton compte Windows, sur cette machine).")
    print("Ce que ça protège : un .env copié ailleurs devient inerte.")
    print("Ce que ça ne protège PAS : un programme lancé sous ton propre compte.")
    print()

    valeur = getpass.getpass("Valeur à sceller (saisie masquée) : ").strip()
    if not valeur:
        print("Rien saisi — abandon.")
        return 1

    try:
        scelle = seal(valeur)
    except VaultError as exc:
        print(f"Échec : {exc}")
        return 1

    if not args.write:
        print("\nColler cette ligne dans .env (la valeur en clair n'y apparaît plus) :\n")
        print(f"{args.variable}={scelle}")
        print("\n(ou relancer avec --write pour que la commande écrive elle-même)")
        return 0

    from .config import ROOT_DIR
    from .store.vault import upsert_env_line

    chemin = ROOT_DIR / ".env"
    remplace = upsert_env_line(chemin, args.variable, scelle)
    verbe = "remplacée" if remplace else "ajoutée"
    print(f"\n{args.variable} {verbe} dans {chemin}")
    print("Valeur scellée par DPAPI — elle n'apparaît en clair nulle part.")
    return 0


async def _run_builder(args: argparse.Namespace) -> int:
    """Programme Builders — qui route du volume, et qui en vit réellement.

    Le classement officiel trie par VOLUME. Ce rapport le retrie par REVENU,
    parce que les deux n'ont presque rien à voir : au 2026-08-15 le premier au
    volume ne prélevait rien du tout. Le taux n'est jamais supposé — il est
    mesuré sur les exécutions attribuées, côté preneur et côté teneur.
    """
    from .builder.api import (
        build_clob_client,
        build_data_client,
        fetch_builder_trades,
        fetch_leaderboard,
    )
    from .builder.revenue import build_estimate, rank_by_revenue, volume_needed_for

    period = args.period.upper()

    async with build_data_client() as data_client:
        entries = await fetch_leaderboard(data_client, period=period, limit=50)

    usable = [e for e in entries if e.has_usable_code][: args.top]
    if not usable:
        print("Aucun builder avec un code exploitable dans le classement.")
        return 1

    semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)

    async with build_clob_client() as clob_client:

        async def sample_for(entry):
            async with semaphore:
                return await fetch_builder_trades(
                    clob_client, entry.code, max_pages=args.pages
                )

        samples = await asyncio.gather(*(sample_for(e) for e in usable))

    estimates = [
        build_estimate(entry, sample, period=period)
        for entry, sample in zip(usable, samples)
    ]
    classement = rank_by_revenue(estimates)

    # Les réserves AVANT les chiffres : un lecteur qui ne lit que le tableau
    # doit avoir déjà croisé ce qui le rend incertain.
    print(f"\n=== Builders Polymarket — période {period}, {len(usable)} builders")
    print("\nCe que ces chiffres ne sont PAS :")
    print("  · le revenu est ESTIMÉ (volume publié × taux mesuré), pas relevé ;")
    print("  · l'unité du volume publié n'a pas pu être vérifiée (dollars ou parts) ;")
    print(f"  · le taux vient d'un échantillon de {args.pages} page(s) au plus, soit "
          f"{args.pages * 300} exécutions ;")
    print("  · c'est le plus haut taux jamais observé, l'endpoint ne date pas les époques.")

    print(
        f"\n{'builder':22} {'utilis.':>8} {'volume':>15} {'preneur':>9} {'teneur':>8} "
        f"{'effectif':>9} {'revenu est.':>13} {'/utilis.':>9}"
    )
    print("-" * 100)
    for est in classement:
        taker = est.schedule.taker
        maker = est.schedule.maker
        revenue = est.estimated_period_revenue_usd
        per_user = est.revenue_per_user_usd
        blended = est.blended_bps
        print(
            f"{est.builder[:22]:22} {est.active_users:>8,} {est.volume:>15,.0f} "
            f"{('—' if taker is None else f'{taker.bps:.0f}bps'):>9} "
            f"{('—' if maker is None else f'{maker.bps:.0f}bps'):>8} "
            f"{('—' if blended is None else f'{blended:.1f}bps'):>9} "
            f"{('inconnu' if revenue is None else f'{revenue:,.0f}$'):>13} "
            f"{('—' if per_user is None else f'{per_user:,.0f}$'):>9}"
        )

    gratuits = [e for e in classement if e.schedule.charges_nothing]
    if gratuits:
        noms = ", ".join(e.builder for e in gratuits[:6])
        print(
            f"\n{len(gratuits)} builder(s) sur {len(classement)} ne prélèvent RIEN : {noms}."
        )
        print("  Le volume n'est donc pas le revenu, et le classement officiel ne le dit pas.")

    hors_plafond = [e for e in classement if e.schedule.exceeds_published_cap]
    if hors_plafond:
        for e in hors_plafond:
            taux = e.schedule.taker.bps if e.schedule.taker else 0.0
            print(
                f"\n{e.builder} facture {taux:.0f} bps côté preneur — le maximum PUBLIÉ "
                "est de 100 bps. Le plafond documenté n'est pas appliqué."
            )

    if args.target:
        print(f"\n=== Volume à router pour encaisser {args.target:,.2f} $/jour")
        print("  (arithmétique pure, aucune donnée de marché — moitié preneur / moitié teneur)")
        for taker_bps, maker_bps, etiquette in (
            (10.0, 5.0, "barème doux (traderline)"),
            (50.0, 25.0, "barème médian (polymtrade)"),
            (100.0, 50.0, "barème plafond (Bullpen, Polycule)"),
        ):
            besoin = volume_needed_for(
                args.target, taker_bps=taker_bps, maker_bps=maker_bps, taker_share=0.5
            )
            print(
                f"  {etiquette:34} {taker_bps:>3.0f}/{maker_bps:<3.0f} bps → "
                f"{besoin:>14,.0f} $ de volume par jour"
            )

    return 0


async def _run_binance(args: argparse.Namespace) -> int:
    """Marchés de prédiction Binance — état d'accès, puis lecture.

    Séparé de `predictfun` bien qu'il s'agisse de la MÊME place de marché
    (`vendor = predict_fun` dans le change-log Binance) : les schémas, les
    codes d'erreur et le chemin d'exécution n'ont rien de commun, et un rapport
    unique laisserait croire que les chiffres se comparent ligne à ligne.
    """
    from .binance.api import BinancePredictionClient
    from .binance.model import BinanceApiError, BinanceSchemaError

    print(SEPARATOR)
    print("BINANCE — PREDICTION TRADING")
    print(SEPARATOR)

    client = BinancePredictionClient()
    if not client.is_readable:
        manquantes = ", ".join(client.missing_credentials)
        print(f"\n⚠ Aucune lecture possible : {manquantes} absente(s).")
        print(
            "\nMESURÉ le 2026-08-09 contre api.binance.com : AUCUNE route de\n"
            "prédiction n'est publique — pas même la liste des catégories.\n"
            "Sans en-tête : -2014. Avec une clé bidon : -2008. Il n'y a donc\n"
            "aucun mode dégradé à proposer, et pas de testnet pour ce produit."
        )
        print(
            "\nCe qu'il faut, dans cet ordre :\n"
            "  1. créer le compte Prédiction dans l'application Binance ;\n"
            "  2. activer l'autorisation SAS (exigée par ordre/transfert/rachat) ;\n"
            "  3. cocher « Prediction Trading » sur la page de gestion des clés ;\n"
            "  4. renseigner BINANCE_API_KEY et BINANCE_API_SECRET dans .env.\n"
            "Les étapes 1 à 3 se font dans l'application : aucune ligne de code\n"
            "ne les remplace."
        )
        print(
            "\nÀ vérifier aussi, et ce n'est pas anodin : Binance écrit que la\n"
            "disponibilité « varies from region to region ». Si le produit\n"
            "n'est pas ouvert depuis ce pays, la clé sera acceptée et les\n"
            "routes refuseront quand même."
        )
        return 1

    try:
        async with client:
            quota = await client.quota_status()
            soldes = await client.payment_option_balances()
            marches = await client.list_markets(limit=args.limit)
            carnets = await client.fetch_books(marches)
    except (BinanceApiError, BinanceSchemaError) as exc:
        print(f"\n✗ {exc}")
        return 1

    print(f"\nQuota du jour : {quota}")
    print(f"Moyens de paiement lisibles : {len(soldes)}")
    print(f"\n{len(marches)} marché(s) lus, {len(carnets)} carnet(s) obtenus.")

    illisibles = {champ for m in marches for champ in m.unread_fields}
    if illisibles:
        # Les schémas REST ne sont publiés nulle part : dire ce qu'on n'a pas
        # su lire vaut mieux qu'afficher des colonnes vides qui ressemblent à
        # des données.
        print(
            f"  ⚠ champs non reconnus dans la réponse : {', '.join(sorted(illisibles))}"
            " — les noms de champs REST ne sont pas documentés par Binance"
        )

    if carnets:
        # Une ligne par BRANCHE, pas par marché : chaque branche a son carnet
        # propre (mesuré 2026-08-18). Les fondre en une seule ligne redonnerait
        # exactement l'illusion que porte l'adaptateur Predict.fun — un carnet
        # unique dont l'autre côté serait déduit.
        entete = "{:>9} {:>8} {:>7} {:>7} {:>7}  {}".format(
            "marché", "branche", "bid", "ask", "écart", "titre"
        )
        print()
        print(entete)
        for marche in marches:
            for token_id in marche.outcome_token_ids:
                carnet = carnets.get(token_id)
                if carnet is None or not carnet.is_two_sided:
                    continue
                branche = str(carnet.raw.get("outcome") or "?")
                titre = (marche.title or "(titre non lu)")[:40]
                print(
                    "{:>9} {:>8} {:>7.3f} {:>7.3f} {:>7.3f}  {}".format(
                        marche.market_id,
                        branche,
                        carnet.best_bid.price,
                        carnet.best_ask.price,
                        carnet.spread,
                        titre,
                    )
                )

    print(
        "\nCes écarts sont AFFICHÉS, pas OBTENUS. La leçon du 2026-07-28 vaut\n"
        "ici aussi : les +2 % vus dans les carnets Polymarket ne se sont jamais\n"
        "retrouvés dans les exécutions. Aucun rendement n'est annoncé tant que\n"
        "le taux d'exécution n'a pas été mesuré."
    )
    print(
        "\nAucun ordre n'a été passé : cette commande est en lecture. Le chemin\n"
        "d'écriture existe (donmarket/binance/trade.py), il est plafonné et\n"
        "désarmé — l'armer est un geste qui vous appartient."
    )
    return 0


async def _run_binance_fill(args: argparse.Namespace) -> int:
    """Sonde de remplissage teneur — le terme manquant depuis le 2026-08-09.

    Le rapport dit d'abord ce qui a été DÉCIDÉ (quel marché, quel prix, et
    pourquoi celui-là), ensuite ce qui a été MESURÉ. L'ordre inverse laisserait
    croire que le chiffre de remplissage vaut pour la place entière, alors qu'il
    vaut pour une branche, un prix et un moment.
    """
    from .binance.api import BinancePredictionClient
    from .binance.model import BinanceApiError, BinanceSchemaError
    from .binance.probe import run_probe

    print(SEPARATOR)
    print("BINANCE — SONDE DE REMPLISSAGE TENEUR")
    print(SEPARATOR)

    if args.notional <= 0:
        print("\n✗ --notional doit être strictement positif.")
        return 2

    client = BinancePredictionClient()
    if not client.is_readable:
        manquantes = ", ".join(client.missing_credentials)
        print(f"\n⚠ Aucune lecture possible : {manquantes} absente(s).")
        print("Voir `donmarket binance` pour la marche à suivre complète.")
        return 1

    if args.arm:
        print(
            f"\n⚠ ARMÉE — un ordre LIMIT de {args.notional:.2f} USDT va être "
            "réellement passé, avec de l'argent réel.\n"
            f"  Observation {args.minutes} min, relevé toutes les "
            f"{args.interval} s, reliquat annulé à la fin."
        )
    else:
        print(
            "\nDÉSARMÉE — la sonde ira jusqu'au devis et s'arrêtera là.\n"
            "  Ajouter --arm pour passer réellement l'ordre."
        )

    try:
        async with client:
            solde = await client.payment_option_balances()
            resultat = await run_probe(
                client,
                notional_usdt=args.notional,
                minutes=args.minutes,
                interval_s=args.interval,
                armed=args.arm,
                max_markets=args.max_markets,
            )
    except (BinanceApiError, BinanceSchemaError) as exc:
        print(f"\n✗ {exc}")
        return 1

    for ligne in solde:
        if str(ligne.get("accountType")) == "CeDeFi":
            print(
                f"\nSolde du portefeuille de prédiction : "
                f"{ligne.get('availableBalanceDisplay')} USDT"
            )

    if resultat.post is None:
        print(f"\n✗ {resultat.problem}")
        for market_id, motif in resultat.rejects[:12]:
            print(f"    marché {market_id} : {motif}")
        if len(resultat.rejects) > 12:
            print(f"    … et {len(resultat.rejects) - 12} autres")
        return 1

    print("\nDÉCISION")
    print(f"  {resultat.post.description}")
    print(f"  titre : {resultat.post.market.title or '(non lu)'}")
    print(f"  écartés : {len(resultat.rejects)} branche(s)/marché(s)")

    if resultat.quote is not None:
        devis = resultat.quote
        print("\nDEVIS (lecture, n'engage rien)")
        print(f"  quoteId  : {devis.quote_id}")
        if devis.size is not None:
            print(f"  parts    : {devis.size}")
        if devis.fee_usdt is not None:
            # Mesuré le 2026-08-18 : `feeAmount` est libellé en PARTS, pas en
            # USDT. Le dire ici évite de relire ce nombre comme un coût en
            # dollars, ce qui le sous-estimerait ou le surestimerait selon le prix.
            print(f"  frais    : {devis.fee_usdt} (en PARTS, pas en USDT)")

    if resultat.problem:
        print(f"\n⚠ {resultat.problem}")

    if not resultat.armed:
        print(
            "\nAucun ordre n'a été passé. Le taux de remplissage reste NON "
            "MESURÉ : c'est --arm qui le mesure, rien d'autre."
        )
        return 0

    print(f"\nMESURE — ordre {resultat.order_id}")
    for snap in resultat.snapshots:
        print(snap.line)

    final = resultat.final_fill
    if final is None:
        print("\n⚠ Aucun relevé exploitable : le remplissage reste inconnu.")
    else:
        delai = resultat.time_to_first_fill_s
        print(
            f"\nRÉSULTAT : {final.status} — {final.fraction * 100:.0f} % rempli "
            f"({final.filled_shares:.2f} parts, {final.filled_usdt:.2f} USDT)"
        )
        print(
            f"  délai avant premier remplissage : "
            f"{f'{delai:.0f} s' if delai is not None else 'jamais rempli'}"
        )
    if resultat.cancelled:
        print("  reliquat ANNULÉ — rien ne reste au carnet.")
    elif final is not None and final.is_terminal:
        print("  état terminal atteint : rien à annuler.")
    else:
        print(
            "  ⚠ reliquat NON annulé — vérifier `donmarket binance` et "
            "l'application Binance."
        )

    print(
        "\nCe chiffre vaut pour UNE branche, UN prix et UN moment. Un seul "
        "ordre ne fait pas un taux de remplissage : il dit seulement si le "
        "chemin teneur fonctionne."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="donmarket", description="Lecture et analyse de tout Polymarket."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="balayer l'univers et mesurer")
    scan.add_argument(
        "--mode",
        choices=("serieux", "normal"),
        default="serieux",
        help="serieux = haute conviction (défaut) ; normal = tout mesurer",
    )
    scan.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="limiter aux N marchés les plus actifs (scan rapide)",
    )
    scan.add_argument(
        "--bankroll",
        type=float,
        default=None,
        help="capital disponible en $, pour dire ce qui est réellement accessible",
    )
    scan.add_argument("--no-persist", action="store_true", help="ne rien écrire en base")
    scan.set_defaults(handler=lambda args: asyncio.run(_run_scan(args)))

    rewards = subparsers.add_parser(
        "rewards", help="chasser les pools de récompenses sous-peuplés"
    )
    rewards.add_argument(
        "--bankroll",
        type=float,
        required=True,
        help="capital disponible en $ — il borne le ticket d'entrée, donc l'univers",
    )
    rewards.add_argument(
        "--mode",
        choices=("serieux", "normal"),
        default="serieux",
        help="serieux = net > 1 %%/jour et historique exigé (défaut) ; normal = tout voir",
    )
    rewards.add_argument(
        "--history-budget",
        type=int,
        default=HISTORY_BUDGET,
        help=(
            "nombre de marchés pour lesquels payer un appel d'historique "
            f"(défaut {HISTORY_BUDGET}) — c'est ce qui fixe la durée du scan"
        ),
    )
    rewards.add_argument(
        "--no-persist", action="store_true", help="ne rien écrire en base"
    )
    rewards.set_defaults(handler=lambda args: asyncio.run(_run_rewards(args)))

    serve = subparsers.add_parser(
        "serve", help="tableau de bord local (lecture seule, boucle locale)"
    )
    serve.add_argument(
        "--bankroll",
        type=float,
        required=True,
        help="capital disponible en $ — modifiable ensuite depuis la page",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=web_server.DEFAULT_PORT,
        help=f"port d'écoute sur 127.0.0.1 (défaut {web_server.DEFAULT_PORT})",
    )
    serve.add_argument(
        "--mode",
        choices=("serieux", "normal"),
        default="serieux",
        help="mêmes seuils que la commande rewards",
    )
    serve.add_argument(
        "--history-budget",
        type=int,
        default=HISTORY_BUDGET,
        help=f"historiques payés par balayage (défaut {HISTORY_BUDGET})",
    )
    serve.set_defaults(handler=_run_serve)

    consensus = subparsers.add_parser(
        "consensus", help="mesurer ce que vaut un vote d'ensemble à N modèles"
    )
    consensus.add_argument(
        "--members", type=int, default=31, help="taille de l'ensemble (défaut 31)"
    )
    consensus.add_argument(
        "--threshold",
        type=int,
        default=28,
        help="voix nécessaires pour décider (défaut 28, comme la méthode d'origine)",
    )
    consensus.add_argument(
        "--markets", type=int, default=40, help="marchés à mesurer (défaut 40)"
    )
    consensus.add_argument(
        "--step",
        type=int,
        default=5,
        help="un vote tous les N points de la série (défaut 5)",
    )
    consensus.set_defaults(handler=lambda args: asyncio.run(_run_consensus(args)))

    backtest = subparsers.add_parser(
        "backtest", help="rejouer la tenue de marché sur les prix passés"
    )
    backtest.add_argument(
        "--markets",
        type=int,
        default=DEFAULT_MARKETS,
        help=f"marchés rejoués, un appel d'historique chacun (défaut {DEFAULT_MARKETS})",
    )
    backtest.add_argument(
        "--max-inventory",
        type=float,
        default=DEFAULT_MAX_INVENTORY,
        help=(
            "plafond d'inventaire en multiples de la taille cotée "
            f"(défaut {DEFAULT_MAX_INVENTORY:g}) — sans lui, une tendance "
            "accumule une position que personne n'aurait portée"
        ),
    )
    backtest.set_defaults(handler=lambda args: asyncio.run(_run_backtest(args)))

    trade = subparsers.add_parser(
        "trade",
        help="planifier et, si --arm est donné, ENVOYER des ordres réels",
        description=(
            "Sans --arm, rien ne part : le portier tourne, les ordres sont "
            "affichés, aucun dollar ne bouge. C'est le mode par lequel il faut "
            "passer d'abord."
        ),
    )
    # Aucun défaut sur ce plafond, et il n'y en aura pas. Une valeur par défaut
    # sur un plafond de dépense est une décision prise à la place du
    # propriétaire du compte, appliquée en silence le jour d'un oubli.
    trade.add_argument(
        "--max-total",
        type=float,
        required=True,
        help="PLAFOND DUR de capital engagé en $ — obligatoire, aucun défaut",
    )
    trade.add_argument(
        "--max-per-market",
        type=float,
        required=True,
        help=(
            "plafond par marché en $ — empêche que tout parte sur le premier "
            "du classement, celui-là même où le modèle de risque se trompe le plus"
        ),
    )
    trade.add_argument(
        "--max-orders",
        type=int,
        default=10,
        help="nombre maximum d'ordres envoyés (défaut 10)",
    )
    trade.add_argument(
        "--bankroll",
        type=float,
        default=float("inf"),
        help="capital de planification (défaut : le plafond dur lui-même)",
    )
    trade.add_argument(
        "--mode", choices=("serieux", "normal"), default="serieux",
        help="mêmes seuils que la commande rewards",
    )
    trade.add_argument("--history-budget", type=int, default=HISTORY_BUDGET)
    trade.add_argument(
        "--arm",
        action="store_true",
        help=(
            "ARMER : signer et envoyer pour de vrai. Sans ce drapeau rien ne "
            "part. L'armement ne se déduit jamais de la présence d'une clé."
        ),
    )
    trade.set_defaults(handler=lambda args: asyncio.run(_run_trade(args)))

    paper = subparsers.add_parser(
        "paper",
        help="DÉMONSTRATION : un compte fictif qui trade sur le vrai marché",
        description=(
            "Ouvre un compte de capital fictif, pose les ordres du plan, et les "
            "confronte aux exécutions réelles du marché en boucle. Aucun ordre "
            "n'est envoyé et aucun dollar ne bouge : le solde évolue par les "
            "remplissages déduits, les récompenses mesurées, et la réévaluation "
            "de l'inventaire au prix courant."
        ),
    )
    paper.add_argument(
        "--bankroll",
        type=float,
        required=True,
        help="capital fictif de départ en $ — obligatoire, aucun défaut",
    )
    paper.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="durée de la démonstration en minutes (défaut 10)",
    )
    paper.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            f"secondes entre deux relevés (défaut {DEFAULT_INTERVAL_SECONDS:.0f}). "
            "Au-delà d'une vingtaine, des exécutions passent inaperçues."
        ),
    )
    paper.add_argument("--mode", choices=("serieux", "normal"), default="serieux")
    paper.add_argument("--history-budget", type=int, default=HISTORY_BUDGET)
    paper.set_defaults(handler=lambda args: asyncio.run(_run_paper(args)))

    history = subparsers.add_parser(
        "history", help="ce que les balayages successifs disent des candidats"
    )
    history.add_argument(
        "--min-observations",
        type=int,
        default=2,
        help="relevés minimaux pour afficher une série (défaut 2)",
    )
    history.add_argument(
        "--limit", type=int, default=25, help="séries affichées (défaut 25)"
    )
    history.set_defaults(handler=_run_history)

    predictfun = subparsers.add_parser(
        "predictfun",
        help="lire Predict.fun (la « Prédiction » de Binance Wallet), hors Polymarket",
    )
    predictfun.add_argument(
        "--network",
        choices=("testnet", "mainnet"),
        default=None,
        help=(
            "défaut : PREDICTFUN_NETWORK ou testnet. mainnet exige "
            "PREDICTFUN_API_KEY (401 sans clé)"
        ),
    )
    predictfun.add_argument(
        "--bankroll",
        type=float,
        default=None,
        help="capital en $ — écarte ce dont le ticket d'entrée dépasse le capital",
    )
    predictfun.add_argument(
        "--include-closed",
        action="store_true",
        help="garder aussi les marchés non négociables (diagnostic)",
    )
    predictfun.add_argument(
        "--show-rejects", action="store_true", help="détailler les marchés écartés"
    )
    predictfun.set_defaults(handler=lambda args: asyncio.run(_run_predictfun(args)))

    binance = subparsers.add_parser(
        "binance",
        help="marchés de prédiction Binance (même place que Predict.fun, autre porte)",
    )
    binance.add_argument(
        "--limit",
        type=int,
        default=20,
        help="marchés demandés par page (défaut 20)",
    )
    binance.set_defaults(handler=lambda args: asyncio.run(_run_binance(args)))

    fill = subparsers.add_parser(
        "binance-fill",
        help="sonde de remplissage teneur : un ordre LIMIT, et on mesure s'il se remplit",
    )
    fill.add_argument(
        "--notional",
        type=float,
        default=2.0,
        help="montant de l'ordre en USDT (défaut 2,0)",
    )
    fill.add_argument(
        "--minutes",
        type=int,
        default=10,
        help="durée d'observation avant annulation du reliquat (défaut 10)",
    )
    fill.add_argument(
        "--interval",
        type=int,
        default=30,
        help="secondes entre deux relevés (défaut 30)",
    )
    fill.add_argument(
        "--max-markets",
        type=int,
        default=40,
        help="marchés lus pour choisir la branche (défaut 40)",
    )
    fill.add_argument(
        "--arm",
        action="store_true",
        help="PASSE UN VRAI ORDRE avec de l'argent réel. Sans ce drapeau, "
        "la sonde va jusqu'au devis et s'arrête.",
    )
    fill.set_defaults(handler=lambda args: asyncio.run(_run_binance_fill(args)))

    builder = subparsers.add_parser(
        "builder",
        help="programme Builders : qui route du volume, et qui en vit vraiment",
    )
    builder.add_argument(
        "--period",
        choices=["DAY", "WEEK", "MONTH", "ALL", "day", "week", "month", "all"],
        default="WEEK",
        help="période du classement (défaut WEEK)",
    )
    builder.add_argument(
        "--top", type=int, default=12, help="builders examinés (défaut 12)"
    )
    builder.add_argument(
        "--pages",
        type=int,
        default=2,
        help="pages d'exécutions échantillonnées par builder, 300 lignes chacune (défaut 2)",
    )
    builder.add_argument(
        "--target",
        type=float,
        default=None,
        help="revenu visé en $/jour : affiche le volume à router pour l'atteindre",
    )
    builder.set_defaults(handler=lambda args: asyncio.run(_run_builder(args)))

    seal_cmd = subparsers.add_parser(
        "seal",
        help="sceller un secret avec DPAPI avant de le coller dans .env (Windows)",
    )
    seal_cmd.add_argument(
        "variable",
        help="nom de la variable, ex. POLYMARKET_PRIVATE_KEY (la VALEUR est demandée en saisie masquée)",
    )
    seal_cmd.add_argument(
        "--write",
        action="store_true",
        help="écrire directement dans .env au lieu d'afficher la ligne à coller",
    )
    seal_cmd.set_defaults(handler=_run_seal)

    stats = subparsers.add_parser("stats", help="état de la base locale")
    stats.set_defaults(handler=_run_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Avant `parse_args` : l'aide d'argparse est elle aussi pleine d'accents.
    force_utf8_console()
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
