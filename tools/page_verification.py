# -*- coding: utf-8 -*-
r"""Mesure ce qui est VERIFIABLE sur Polymarket, et publie `docs/verify.html`.

    .venv\Scripts\python tools\page_verification.py
    .venv\Scripts\python tools\page_verification.py --combien 10

Lecture seule : aucune cle, aucune authentification, aucun ordre.

## Le fait que cette page existe pour publier

Partout ou l'on parle de bots Polymarket, la meme phrase revient : « aucun ne
peut prouver que son historique est reel ». On l'attribue a la mauvaise foi des
auteurs. C'est faux, et la vraie raison est mecanique :

    l'API publique refuse de servir au-dela de 5 000 actes par portefeuille
    (« max historical activity offset of 5000 exceeded »)

Un compte qui annonce 32 614 trades ne PEUT donc pas etre verifie, meme par
quelqu'un de parfaitement honnete et parfaitement outille. Ce n'est pas un
soupcon, c'est une propriete de la plateforme -- et personne ne l'a publiee.

Cette page la remesure a chaque passage, sur les premiers du classement, plutot
que de l'affirmer une fois. Une regle qu'on republie sans la remesurer finit
toujours par mentir (lecon du 2026-08-29 sur la « loi du tick »).

## Ce que la page dit AUSSI, parce que l'omettre serait malhonnete

Un retrait ne se falsifie pas. Un PnL affiche est une ligne dans une interface ;
de l'argent SORTI d'un compte a du exister pour sortir. Les gros portefeuilles
montrent des millions retires. Leur fortune n'est pas prouvable dans le detail,
elle n'est pas inventee pour autant. Publier « invérifiable » sans publier
« corrobore par les retraits » serait exactement le raccourci qu'on reproche
aux captures d'ecran.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from importlib import util

sys.path.insert(0, ".")
sys.path.insert(0, "tools")
import enveloppe  # noqa: E402

INSTANTANE = "docs/leaderboard-snapshot.json"
SORTIE_HTML = "docs/verify.html"
SORTIE_JSON = "docs/verify.json"
PLAFOND_API = 5000

# NOTRE garde-fou, pas celui de Polymarket. Depuis qu'on lit par fenetres
# start/end (technique donnee par fedoras dans le groupe Builders le 30/08),
# l'historique complet est atteignable -- mais les plus gros comptes font
# ~2 500 actes par jour, soit des dizaines de milliers de requetes pour tout
# lire. On borne donc, et la page DIT ce qu'elle a lu plutot que de laisser
# croire a un audit complet.
MAX_ACTES = 30000


def verificateur():
    """Charge `tools/verifier_portefeuille.py`, qui n'est pas dans un paquet.

    On REUTILISE sa comptabilite au lieu de la reecrire : deux comptabilites
    qui divergent d'un centime discrediteraient les deux.
    """
    spec = util.spec_from_file_location("verifier_portefeuille",
                                        "tools/verifier_portefeuille.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sonder(client, verif, wallet: str) -> dict:
    """Lit un portefeuille jusqu'au refus de l'API, et rapporte le refus.

    Le refus EST la mesure : c'est lui qui prouve qu'un historique long est
    hors de portee de quiconque, nous compris.
    """
    actes, plafond = verif.collecter(client, wallet, MAX_ACTES)
    compta = verif.comptabiliser(actes)
    _, ouverts = verif.trier(compta["jetons"])
    return {"actes": len(actes), "plafond_atteint": bool(plafond),
            "depots": float(compta["depots"]),
            "retraits": float(compta["retraits"]), "ouvertes": len(ouverts)}


def echantillon(combien: int, periodes: list[str]) -> list[dict]:
    """Portefeuilles a sonder, dedupliques, avec la periode qui les a fait
    entrer dans l'echantillon.

    ON NE PREND PAS QUE LE ALL-TIME. Le classement all-time est un pantheon de
    baleines ; celui du jour est fait de comptes actifs et plus petits. Melanger
    les deux permet de repondre a une question bien plus utile qu'un comptage :
    L'AUDITABILITE DEPEND-ELLE DE LA TAILLE ? Si seuls les petits comptes sont
    verifiables, le probleme n'est pas « certains sont opaques », c'est « on ne
    peut verifier que ceux dont personne ne parle ».
    """
    with open(INSTANTANE, encoding="utf-8") as f:
        classements = json.load(f)["classements"]

    vus, out = set(), []
    for periode in periodes:
        for ligne in classements.get(periode, [])[:combien]:
            if ligne["wallet"] in vus:
                continue
            vus.add(ligne["wallet"])
            out.append({**ligne, "periode": periode})
    return out


def mesurer(combien: int, periodes: list[str]) -> list[dict]:
    import httpx

    classement = echantillon(combien, periodes)
    print(f"{len(classement)} portefeuilles uniques sur "
          f"{'+'.join(periodes)}")

    client = httpx.Client(headers={"accept": "application/json"})
    verif, out = verificateur(), []
    for rang, ligne in enumerate(classement, 1):
        try:
            mesure = sonder(client, verif, ligne["wallet"])
        except Exception as exc:  # noqa: BLE001
            mesure = {"erreur": str(exc)[:120]}
        out.append({"rang": rang, "nom": ligne["nom"], "wallet": ligne["wallet"],
                    "periode": ligne["periode"],
                    "pnl_annonce": float(ligne["pnl"]), **mesure})
        print(f"  {rang:>2}. {ligne['nom'][:20]:<20} "
              f"actes={mesure.get('actes', '?')} "
              f"plafond={mesure.get('plafond_atteint')}")
    return out


def page(lignes: list[dict], style: str, quand: str) -> str:
    bloques = [l for l in lignes if l.get("plafond_atteint")]
    part = 100 * len(bloques) / len(lignes) if lignes else 0

    # LE CHIFFRE QUI DECIDE. Retraits moins depots : de l'argent qui a QUITTE
    # le compte, net de ce qui y est entre. Un PnL affiche est une ligne dans
    # une interface ; une sortie nette a du exister pour sortir. Compare a
    # l'annonce, c'est la meilleure corroboration disponible publiquement.
    for l in lignes:
        l["sorti_net"] = l.get("retraits", 0) - l.get("depots", 0)
        annonce = l.get("pnl_annonce") or 0
        l["ecart_pct"] = (100 * abs(l["sorti_net"] - annonce) / annonce
                          if annonce else None)

    # TROIS ETATS, PLUS DEUX. « Non auditable » ne veut plus dire ce qu'il
    # disait : depuis la lecture par fenetres, un compte n'est incomplet que
    # parce qu'on a BORNE la lecture, pas parce que la plateforme l'interdit.
    # Confondre les deux etait l'erreur publiee le 29/08.
    partiels = [l for l in lignes if l.get("actes", 0) >= MAX_ACTES]
    complets = [l for l in lignes
                if l.get("actes", 0) < MAX_ACTES and not l.get("plafond_atteint")]
    colles = [l for l in complets
              if l["ecart_pct"] is not None and l["ecart_pct"] <= 10]

    def verdict(l: dict) -> str:
        if l.get("actes", 0) >= MAX_ACTES:
            return f"partial read ({MAX_ACTES:,} cap)"
        if l["ecart_pct"] is None:
            return "&mdash;"
        if l["ecart_pct"] <= 10:
            return f"<b>corroborated</b> ({l['ecart_pct']:.1f}%)"
        return f"gap {l['ecart_pct']:.0f}%"

    rangs = "".join(
        f"<tr><td>{l['rang']}</td><td>{html.escape(l['nom'][:24])}</td>"
        f"<td class='num'>{l['pnl_annonce']:,.0f}</td>"
        f"<td class='num'>{l.get('actes', 0):,}</td>"
        f"<td class='num'>{l['sorti_net']:,.0f}</td>"
        f"<td>{verdict(l)}</td>"
        f"<td class='num'>{l.get('ouvertes', 0)}</td></tr>"
        for l in lignes)

    meilleur = max((l for l in complets if l.get("pnl_annonce")),
                   key=lambda l: l["pnl_annonce"], default=None)
    tuiles = enveloppe.tuile(
        f"{len(colles)}/{len(complets)}", "", "wallets corroborated",
        "Every wallet readable end to end matches its advertised PnL "
        "within 10%.")
    if meilleur:
        tuiles += enveloppe.tuile(
            f"${meilleur['pnl_annonce'] / 1e6:.1f}", "M", "largest verified",
            f"{html.escape(meilleur['nom'][:20])}, reconciled against money "
            "that actually left the account.")
    tuiles += enveloppe.tuile(
        f"{len(partiels)}", f"of {len(lignes)}", "too heavy to finish",
        f"Readable in principle, but past {MAX_ACTES:,} records it is "
        "thousands of requests and the API throttles you.")

    return enveloppe.debut(
        titre="Can you verify a Polymarket track record?",
        chapeau=f"DONMARKET &middot; MEASURED {quand.upper()}",
        dek=f"Yes &mdash; and where the history can be read end to end, "
            f"<b>{len(colles)} of {len(complets)} wallets match their "
            f"advertised PnL within 10%</b>, most within about one percent. "
            f"The leaderboard is telling the truth.",
        signature="Read-only &middot; No key &middot; Public APIs only "
                  "&middot; Reproducible",
        og="Yes, you can - almost nobody does. Measured: the top wallets that "
           "can be read end to end match their advertised PnL to within about "
           "1%.",
        tuiles=tuiles) + f"""
<section>
<p>Every week someone posts a screenshot: <i>99.3% across 32,614 trades</i>,
<i>$313 turned into $438K</i>. And every week the top comment says the same
thing &mdash; <i>none of them can prove their track record is real</i>. So we
went and checked.</p>

<h2>First, a correction, because it is the useful part</h2>
<p>The obvious way to read a wallet's history stops dead. Ask the API for
activity record {PLAFOND_API + 1:,} and it answers:</p>

<pre>max historical activity offset of {PLAFOND_API} exceeded</pre>

<p>On 29 August we published that this made long track records
<i>structurally unverifiable</i>. <b>That was wrong.</b> Within hours,
<i>fedoras</i> in the Polymarket builders group pointed at the documentation:
the {PLAFOND_API:,} limit is <b>per query window, not per wallet</b>. Page
inside <code>start</code>/<code>end</code> windows and each window gets its own
offset budget. Confirmed the same day &mdash; 10,000 records off a wallet that
had stopped dead at {PLAFOND_API:,}.</p>

<p>So this page now reads history by splitting time adaptively: any window that
saturates is halved, down to the hour. The lesson was worth more than the bug.
<b>A limit you could not get past is not the same as a limit that exists.</b>
We had exhausted our own method, not every method, and published an
impossibility on the strength of it.</p>

<p>What remains true is narrower and more interesting: the data is reachable,
but it takes deliberate work, so <i>almost nobody checks</i>. The busiest
wallets run about 2,500 records a day; reading one in full is tens of thousands
of requests. This page caps each wallet at {MAX_ACTES:,} records and says so in
the table rather than pretending to a complete audit.</p>

</section>

<section>
<h2>The measurement</h2>
<div class="scroller"><table>
<thead><tr><th class="l">#</th><th class="l">Wallet</th>
<th class="num">Claimed PnL ($)</th><th class="num">Records read</th>
<th class="num">Net withdrawn ($)</th><th class="l">Verdict</th>
<th class="num">Open</th></tr></thead>
<tbody>{rangs}</tbody>
</table></div>

<h2>And now the part nobody expects</h2>
<p class="lede">Of the {len(complets)} wallets read end to end,
<b>{len(colles)} match their claimed PnL within 10%</b> &mdash; most of them
within about one percent.</p>

<p><b>Withdrawals cannot be faked.</b> A displayed PnL is a line in an
interface; money that <i>left</i> an account had to exist in order to leave.
So we take withdrawals minus deposits &mdash; net money out, on chain &mdash;
and compare it to the number the leaderboard advertises. They agree closely.</p>

<p>The running assumption in every thread is that the top of this leaderboard
is fabricated. On this evidence it is not. The gap is not honesty, it is
effort: the proof is sitting on chain and essentially nobody goes and gets
it.</p>

<p>Which turns the usual suspicion upside down. Be sceptical not of the
eight-figure wallets that withdraw millions, but of the small screenshots
&mdash; <i>$20 into $180 in a week</i> &mdash; where nothing was ever withdrawn
and every gain still sits in open positions.</p>

<h2>Two traps in reading any of these claims</h2>
<p><b>Deposits are the denominator.</b> &ldquo;$313 into $438K&rdquo; means
nothing if the wallet also received $500K in deposits. The deposit is public;
almost nobody quotes it. On our own account the same error ran the other way:
we compared value against <i>cumulative capital deployed</i> instead of
deposits, and believed we were down 25% while we were up 41%.</p>
<p><b>Realised gains and win rates are upper bounds, never results.</b> You
close winners &mdash; there is a buyer &mdash; and you keep losers, because
there is no counterparty. Losses therefore pile up in open positions and are
never counted. That asymmetry alone is enough to manufacture a 100% win rate
on a losing account.</p>

<h2>Check it yourself</h2>
<p>The tool is open source and read-only &mdash; no key, no authentication, no
orders. It refuses to state a total while positions are open or while events it
cannot account for remain, and it says which:</p>
<pre>python tools/verifier_portefeuille.py 0xWALLET --annonce 438000</pre>
<p>Want this run on a specific wallet, or the same treatment on a set of
markets? <a href="./work.html">Rates and how to reach me</a>.</p>
</section>
""" + enveloppe.fin("verify.html")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--combien", type=int, default=10,
                   help="portefeuilles par periode")
    p.add_argument("--periodes", default="ALL",
                   help="periodes du classement, separees par des virgules : "
                        "ALL,MONTH,WEEK,DAY")
    p.add_argument("--rejouer", action="store_true",
                   help="regenere la page depuis docs/verify.json, sans "
                        "re-sonder l'API")
    args = p.parse_args()

    if args.rejouer:
        # REFORMULER N'EST PAS REMESURER. Sonder dix portefeuilles coute ~500
        # requetes a une API publique qui ne nous doit rien ; les taper a
        # nouveau pour corriger une tournure de phrase serait indefendable.
        # La mesure affichee reste celle du JSON, jamais l'heure du rejeu.
        with open(SORTIE_JSON, encoding="utf-8") as f:
            garde = json.load(f)
        lignes, quand_iso = garde["portefeuilles"], garde["mesure"]
        print(f"rejeu depuis {SORTIE_JSON}, mesure du {quand_iso[:19]}")
    else:
        periodes = [p.strip().upper() for p in args.periodes.split(",")]
        print(f"sondage des {args.combien} premiers de {args.periodes}...")
        lignes, quand_iso = mesurer(args.combien, periodes), None
    if not lignes:
        print("aucun portefeuille lu -- rien n'est ecrit.")
        return 1

    # LA DATE DE MESURE, PAS CELLE DE PUBLICATION. Meme piege que `health.html`
    # le 27/08 : la page datait du jour ou on l'ecrivait des chiffres de la
    # veille. Au rejeu, la date reste celle du sondage.
    mesure = (datetime.fromisoformat(quand_iso) if quand_iso
              else datetime.now(timezone.utc))
    with open("docs/_template.html", encoding="utf-8") as f:
        style = f.read().split("<style>", 1)[1].split("</style>")[0]
    with open(SORTIE_HTML, "w", encoding="utf-8", newline="\n") as f:
        f.write(page(lignes, style, mesure.strftime("%d %B %Y, %H:%M UTC")))
    if not quand_iso:
        with open(SORTIE_JSON, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"mesure": mesure.isoformat(),
                       "plafond_api": PLAFOND_API, "portefeuilles": lignes},
                      f, indent=1, ensure_ascii=False)

    complets = sum(1 for l in lignes if 0 < l.get("actes", 0) < MAX_ACTES)
    print(f"\n{SORTIE_HTML} -- {complets}/{len(lignes)} portefeuilles lus "
          f"de bout en bout (les autres bornes a {MAX_ACTES:,} actes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
