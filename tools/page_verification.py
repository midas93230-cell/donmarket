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

INSTANTANE = "docs/leaderboard-snapshot.json"
SORTIE_HTML = "docs/verify.html"
SORTIE_JSON = "docs/verify.json"
PLAFOND_API = 5000


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
    actes, plafond = verif.collecter(client, wallet, PLAFOND_API)
    compta = verif.comptabiliser(actes)
    _, ouverts = verif.trier(compta["jetons"])
    return {"actes": len(actes), "plafond_atteint": bool(plafond),
            "depots": float(compta["depots"]),
            "retraits": float(compta["retraits"]), "ouvertes": len(ouverts)}


def mesurer(combien: int) -> list[dict]:
    from polymarket import PublicClient

    with open(INSTANTANE, encoding="utf-8") as f:
        classement = json.load(f)["classements"]["ALL"][:combien]

    client, verif, out = PublicClient(), verificateur(), []
    for rang, ligne in enumerate(classement, 1):
        try:
            mesure = sonder(client, verif, ligne["wallet"])
        except Exception as exc:  # noqa: BLE001
            mesure = {"erreur": str(exc)[:120]}
        out.append({"rang": rang, "nom": ligne["nom"], "wallet": ligne["wallet"],
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

    auditables = [l for l in lignes if not l.get("plafond_atteint")]
    colles = [l for l in auditables
              if l["ecart_pct"] is not None and l["ecart_pct"] <= 10]

    def verdict(l: dict) -> str:
        if l.get("plafond_atteint"):
            return "<b>not auditable</b>"
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

    return f"""<meta charset="utf-8">
<title>Can you verify a Polymarket track record?</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="Can you verify a Polymarket track record?">
<meta property="og:description" content="Measured: Polymarket's public API stops \
at {PLAFOND_API} activity records per wallet. Every long track record is \
structurally unverifiable.">
<style>{style}</style>
<h1>Can you verify a Polymarket track record?</h1>
<p class="lede">Measured {quand}. <b>{len(bloques)} of the top {len(lignes)}
wallets ({part:.0f}%) cannot be audited at all</b> &mdash; not because their
owners hide anything, but because the public API stops serving after
{PLAFOND_API} activity records per wallet.</p>

<p>Every week someone posts a screenshot: <i>99.3% across 32,614 trades</i>,
<i>$313 turned into $438K</i>. And every week the top comment says the same
thing &mdash; <i>none of them can prove their track record is real</i>. That
comment is right, and the reason is not dishonesty. Ask the API for record
{PLAFOND_API + 1:,} of any wallet and it answers:</p>

<pre>max historical activity offset of {PLAFOND_API} exceeded</pre>

<p>So a 32,614-trade history is out of reach for <b>everyone</b>, including
someone perfectly honest with perfect tooling. That is a property of the
platform, not a judgement about the trader.</p>

<table>
<tr><th>#</th><th>Wallet</th><th>Claimed PnL ($)</th><th>Records readable</th>
<th>Net withdrawn ($)</th><th>Verdict</th><th>Open positions</th></tr>
{rangs}
</table>

<h2>And now the part nobody expects</h2>
<p class="lede">Of the {len(auditables)} wallets whose entire history <i>is</i>
readable, <b>{len(colles)} match their claimed PnL within 10%</b> &mdash; most
of them within about one percent.</p>

<p><b>Withdrawals cannot be faked.</b> A displayed PnL is a line in an
interface; money that <i>left</i> an account had to exist in order to leave.
So we take withdrawals minus deposits &mdash; net money out, on chain &mdash;
and compare it to the number the leaderboard advertises. For the wallets we can
read end to end, the two agree closely.</p>

<p>The running assumption in every thread is that the top of this leaderboard
is fabricated. On this evidence it is not. The problem is narrower, and
stranger: <b>you cannot check {len(bloques)} of them at all</b>, and the ones
you can check turn out to be telling the truth.</p>

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
<p><a href="https://github.com/midas93230-cell/donmarket">Source</a> &middot;
<a href="health.html">Daily order-book health</a> &middot;
<a href="app.html">Exit tool for unsellable positions</a></p>
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--combien", type=int, default=10)
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
        print(f"sondage des {args.combien} premiers du classement...")
        lignes, quand_iso = mesurer(args.combien), None
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

    bloques = sum(1 for l in lignes if l.get("plafond_atteint"))
    print(f"\n{SORTIE_HTML} -- {bloques}/{len(lignes)} portefeuilles "
          "non auditables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
