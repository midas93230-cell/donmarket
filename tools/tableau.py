# -*- coding: utf-8 -*-
"""Tableau de bord LECTURE SEULE de l'etat du compte, sur 127.0.0.1.

    .venv/Scripts/python tools/tableau.py
    .venv/Scripts/python tools/tableau.py --port 8787

Puis ouvrir http://127.0.0.1:8787 dans un navigateur. La page se rafraichit
toute seule.

## Pourquoi celui-ci en plus de `cli.py serve`

`cli.py serve` existe depuis le 29/07, mais il sert la CHASSE AUX RECOMPENSES :
il balaie l'univers et classe des candidats. Il ne montre ni les ordres
ouverts, ni les positions, ni les gains -- c'est-a-dire rien de ce qu'on veut
regarder quand une boucle tourne et qu'on attend un remplissage.

## Ce qu'il ne fait pas, et ne fera pas

Aucun ordre ne part d'ici. Pas de bouton, pas de POST, pas de `--arm`. Un
tableau de bord qui peut trader est un tableau de bord qu'on clique par
accident. Pour agir, il y a les outils dedies.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")

PORT_DEFAUT = 8787
RAFRAICHISSEMENT_S = 20

# L'etat partage entre le collecteur et le serveur. Un verrou suffit : un seul
# ecrivain, quelques lecteurs.
_etat: dict = {"pret": False, "erreur": None, "mesure_a": None}
_verrou = threading.Lock()


def _relever(client, session) -> dict:
    """Un releve complet. Toute erreur est RENDUE, jamais avalee."""
    from donmarket.making.runner import flatten

    maintenant = datetime.now(timezone.utc)
    etat: dict = {"mesure_a": maintenant, "pret": True, "erreur": None}

    solde = client.get_balance_allowance(asset_type="COLLATERAL")
    etat["liquide"] = int(solde.balance) / 1e6

    ordres = []
    engage = 0.0
    for o in flatten(client.list_open_orders()):
        prix = float(o.price)
        taille = float(o.original_size)
        ligne = {
            "sens": str(o.side).upper(),
            "prix": prix,
            "taille": taille,
            "rempli": float(o.size_matched or 0),
            "valeur": prix * taille,
            "bid": None, "ask": None,
        }
        try:
            carnet = client.get_order_book(token_id=str(o.token_id))
            if carnet.bids:
                ligne["bid"] = float(carnet.bids[-1].price)
            if carnet.asks:
                ligne["ask"] = float(carnet.asks[-1].price)
        except Exception:  # noqa: BLE001
            pass
        if ligne["sens"] == "BUY":
            engage += ligne["valeur"]
        ordres.append(ligne)
    etat["ordres"] = ordres
    etat["engage"] = engage
    etat["dormant"] = etat["liquide"] - engage

    positions = []
    for p in flatten(client.list_positions()):
        valeur = float(p.current_value or 0)
        parts = float(p.size or 0)
        ligne = {
            "titre": str(p.title or "")[:60],
            "parts": parts,
            "revient": float(p.avg_price or 0),
            "valeur": valeur,
            "pnl": float(p.cash_pnl or 0),
            "bid": None,
            "vendable": None,
            "gain_si_vendu": None,
        }
        # CE QU'ON AURAIT DU VOIR PLUS TOT. Le 24/08, « Trump x Greenland »
        # gagnait +63 % depuis la veille et personne ne l'a regardee : on
        # courait apres des tickets a 2 $ sur des marches neufs pendant qu'un
        # profit dormait dans le portefeuille. Le defaut n'etait pas technique,
        # il etait dans l'attention -- donc il se corrige ici, en montrant ce
        # qu'une position RAPPORTERAIT SI ON LA VENDAIT MAINTENANT.
        if valeur > 0 and parts > 0:
            try:
                carnet = client.get_order_book(token_id=str(p.token_id))
                if carnet.bids:
                    bid = float(carnet.bids[-1].price)
                    ligne["bid"] = bid
                    ligne["gain_si_vendu"] = parts * (bid - ligne["revient"])
                    # Le minimum d'ordre vaut 5 parts sur tout ce qu'on a
                    # rencontre. Une position en dessous est INVENDABLE, et
                    # c'est le piege qui a bloque 2,15 $ sur Solana le 24/08.
                    ligne["vendable"] = parts >= 5.0
            except Exception:  # noqa: BLE001
                pass
        positions.append(ligne)
    positions.sort(key=lambda x: -x["valeur"])
    etat["positions"] = positions
    etat["valeur_positions"] = sum(p["valeur"] for p in positions)

    jour = maintenant.date().isoformat()
    try:
        etat["gains_jour"] = sum(
            float(t.earnings)
            for t in client.get_total_earnings_for_user_for_day(date=jour)
        )
    except Exception:  # noqa: BLE001
        etat["gains_jour"] = None

    # Les Up/Down, demandes par slug : le tri par volume rate les marches neufs
    # (mesure du 23/08, voir `tenir_updown`).
    mois = ["january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december"]
    auj = maintenant.date()
    slugs = [
        f"{actif}-up-or-down-on-{mois[j.month - 1]}-{j.day}-{j.year}"
        for d in range(0, 4)
        for j in (auj + timedelta(days=d),)
        for actif in ("bitcoin", "ethereum", "solana")
    ]
    updown = []
    try:
        reponse = session.get(
            "https://gamma-api.polymarket.com/markets",
            params=[("slug", s) for s in slugs] + [("limit", "100")],
            timeout=25,
        )
        reponse.raise_for_status()
        for m in reponse.json():
            if m.get("closed"):
                continue
            jetons = json.loads(m.get("clobTokenIds") or "[]")
            if not jetons:
                continue
            carnet = client.get_order_book(token_id=jetons[0])
            if not carnet.bids or not carnet.asks:
                continue
            bid = float(carnet.bids[-1].price)
            ask = float(carnet.asks[-1].price)
            updown.append({
                "slug": str(m.get("slug"))[:46],
                "volume": float(m.get("volume24hr") or 0),
                "bid": bid, "ask": ask,
                "ecart": 100 * (ask - bid) / bid if bid else 0.0,
                "ticket": 5 * bid,
            })
    except Exception as exc:  # noqa: BLE001
        etat["erreur"] = f"up/down illisibles : {exc}"
    updown.sort(key=lambda x: -x["volume"])
    etat["updown"] = updown
    return etat


def _collecteur(intervalle: int) -> None:
    from dotenv import load_dotenv
    import httpx
    from polymarket import SecureClient

    from donmarket.store import vault

    load_dotenv(".env", override=True)
    client = SecureClient.create(
        private_key=vault.read_secret("POLYMARKET_PRIVATE_KEY"),
        wallet=os.environ["POLYMARKET_FUNDER"],
    )
    session = httpx.Client()
    while True:
        try:
            neuf = _relever(client, session)
        except Exception as exc:  # noqa: BLE001
            # Un releve rate laisse le PRECEDENT en place, mais il le DIT :
            # un tableau qui affiche des chiffres perimes sans le signaler est
            # pire qu'un tableau vide.
            neuf = None
            with _verrou:
                _etat["erreur"] = f"releve du {datetime.now(timezone.utc):%H:%M} rate : {exc}"
        if neuf is not None:
            with _verrou:
                _etat.clear()
                _etat.update(neuf)
        time.sleep(intervalle)


def _euro(x: float) -> str:
    return f"{x:,.3f}".replace(",", " ")


def _page(etat: dict) -> str:
    e = html.escape
    if not etat.get("pret"):
        corps = "<p class='attente'>Premier releve en cours&hellip;</p>"
        if etat.get("erreur"):
            corps += f"<p class='alarme'>{e(str(etat['erreur']))}</p>"
    else:
        mesure = etat["mesure_a"].strftime("%H:%M:%S UTC")
        total = etat["liquide"] + etat["valeur_positions"]
        alerte = (f"<p class='alarme'>{e(str(etat['erreur']))}</p>"
                  if etat.get("erreur") else "")

        tuiles = f"""
        <div class='tuiles'>
          <div class='t'><span class='n'>{_euro(total)}</span><span class='l'>valeur totale</span></div>
          <div class='t'><span class='n'>{_euro(etat['liquide'])}</span><span class='l'>liquide pUSD</span></div>
          <div class='t'><span class='n'>{_euro(etat['engage'])}</span><span class='l'>engage en achats</span></div>
          <div class='t {'chaud' if etat['dormant'] > 1 else ''}'><span class='n'>{_euro(etat['dormant'])}</span><span class='l'>dormant</span></div>
        </div>"""

        lignes = ""
        for o in etat["ordres"]:
            hors = ""
            if o["sens"] == "BUY" and o["bid"] is not None and o["prix"] < o["bid"]:
                hors = " <b class='alarme'>SOUS LE MARCHE</b>"
            lignes += (
                f"<tr><td>{e(o['sens'])}</td><td class='n'>{o['taille']:.0f}</td>"
                f"<td class='n'>{o['prix']:.3f}</td>"
                f"<td class='n'>{o['rempli']:.0f}</td>"
                f"<td class='n'>{o['bid']} / {o['ask']}{hors}</td>"
                f"<td class='n'>{_euro(o['valeur'])}</td></tr>"
            )
        ordres = (f"<table><tr><th>sens</th><th>parts</th><th>prix</th>"
                  f"<th>rempli</th><th>carnet</th><th>$</th></tr>{lignes}</table>"
                  if lignes else "<p class='vide'>aucun ordre au carnet</p>")

        lignes = ""
        a_prendre = []
        for p in etat["positions"]:
            classe = "morte" if p["valeur"] <= 0 else ""
            gain = p["gain_si_vendu"]
            if gain is None:
                sortie = "&mdash;"
            elif p["vendable"] is False:
                sortie = (f"<b class='alarme'>BLOQUEE</b> "
                          f"<span class='note'>{p['parts']:.2f} &lt; 5</span>")
            elif gain > 0:
                sortie = f"<b class='gain'>{gain:+.2f} $ a prendre</b>"
                a_prendre.append((p["titre"], gain, p["bid"]))
                classe = "profit"
            else:
                sortie = f"<span class='note'>{gain:+.2f} $</span>"
            lignes += (
                f"<tr class='{classe}'><td>{e(p['titre'])}</td>"
                f"<td class='n'>{p['parts']:.2f}</td>"
                f"<td class='n'>{p['revient']:.3f}</td>"
                f"<td class='n'>{_euro(p['valeur'])}</td>"
                f"<td class='n'>{p['pnl']:+.2f}</td>"
                f"<td class='n'>{sortie}</td></tr>"
            )
        positions = (f"<table><tr><th>marche</th><th>parts</th><th>revient</th>"
                     f"<th>valeur</th><th>pnl</th><th>si vendu au bid</th></tr>"
                     f"{lignes}</table>"
                     if lignes else "<p class='vide'>aucune position</p>")

        # LA BANNIERE. Un chiffre au milieu d'un tableau se lit quand on le
        # cherche ; celui-ci doit se voir quand on ne cherche rien.
        if a_prendre:
            total_a_prendre = sum(g for _, g, _ in a_prendre)
            detail = " &middot; ".join(
                f"{e(t[:34])} <b>{g:+.2f} $</b> au bid {b:.3f}"
                for t, g, b in sorted(a_prendre, key=lambda x: -x[1])
            )
            positions = (
                f"<div class='banniere'>{len(a_prendre)} position(s) EN GAIN "
                f"et vendable(s) &mdash; <b>{total_a_prendre:+.2f} $</b> a "
                f"prendre maintenant<div class='detail'>{detail}</div></div>"
                + positions
            )

        lignes = ""
        for u in etat["updown"]:
            large = " class='large'" if u["ecart"] >= 8 else ""
            lignes += (
                f"<tr><td>{e(u['slug'])}</td>"
                f"<td class='n'>{u['volume']:,.0f}</td>"
                f"<td class='n'{large}>{u['ecart']:.1f} %</td>"
                f"<td class='n'>{u['bid']:.3f} / {u['ask']:.3f}</td>"
                f"<td class='n'>{u['ticket']:.2f}</td></tr>"
            )
        updown = (f"<table><tr><th>marche</th><th>volume 24h</th><th>ecart</th>"
                  f"<th>carnet</th><th>ticket</th></tr>{lignes}</table>"
                  if lignes else "<p class='vide'>aucun up/down ouvert</p>")

        gains = ("&mdash;" if etat["gains_jour"] is None
                 else f"{etat['gains_jour']:.6f} $")

        corps = f"""
        {alerte}
        {tuiles}
        <h2>Ordres au carnet</h2>{ordres}
        <h2>Positions</h2>{positions}
        <h2>Up / Down crypto &mdash; ecart contre volume</h2>
        <p class='note'>La regle mesuree le 23/08 : la ou il y a du volume,
        l'ecart est a un tick. Un ecart large signale l'absence de
        contrepartie, pas une occasion.</p>{updown}
        <h2>Recompenses de liquidite (aujourd'hui)</h2>
        <p class='gros'>{gains}</p>
        <p class='pied'>Releve de {mesure} &middot; rafraichissement automatique
        toutes les {RAFRAICHISSEMENT_S} s &middot; LECTURE SEULE, aucun ordre ne
        part d'ici.</p>"""

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>DONmarket</title>
<meta http-equiv="refresh" content="{RAFRAICHISSEMENT_S}">
<style>
:root{{--fond:#0e1319;--carte:#161d25;--trait:#28313b;--encre:#e9eef3;
--encre2:#a5b1be;--vert:#63b2ae;--alarme:#e27f70;--or:#e3b155}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px;background:var(--fond);color:var(--encre);
font:15px/1.6 system-ui,'Segoe UI',sans-serif}}
h1{{font-size:20px;margin:0 0 4px}}
h2{{font-size:15px;margin:26px 0 8px;color:var(--encre2);
text-transform:uppercase;letter-spacing:.08em}}
.tuiles{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.t{{background:var(--carte);border:1px solid var(--trait);border-radius:10px;
padding:14px 18px;min-width:150px}}
.t .n{{display:block;font-size:24px;font-variant-numeric:tabular-nums}}
.t .l{{display:block;font-size:12px;color:var(--encre2);text-transform:uppercase;
letter-spacing:.06em}}
.t.chaud .n{{color:var(--or)}}
table{{width:100%;border-collapse:collapse;background:var(--carte);
border:1px solid var(--trait);border-radius:10px;overflow:hidden}}
th{{text-align:left;font-size:12px;color:var(--encre2);text-transform:uppercase;
letter-spacing:.06em;padding:10px 12px;border-bottom:1px solid var(--trait)}}
td{{padding:9px 12px;border-bottom:1px solid var(--trait)}}
tr:last-child td{{border-bottom:none}}
td.n{{font-variant-numeric:tabular-nums}}
td.large{{color:var(--or);font-weight:600}}
tr.morte td{{color:#6d7b89}}
tr.profit{{background:rgba(99,178,174,.08)}}
.gain{{color:var(--vert)}}
.banniere{{background:rgba(99,178,174,.12);border:1px solid var(--vert);
border-radius:10px;padding:14px 18px;margin:0 0 12px;color:var(--encre)}}
.banniere .detail{{margin-top:6px;font-size:13px;color:var(--encre2)}}
.alarme{{color:var(--alarme);font-weight:600}}
.vide,.attente,.note{{color:var(--encre2);font-size:13px}}
.gros{{font-size:22px;font-variant-numeric:tabular-nums}}
.pied{{margin-top:26px;color:#6d7b89;font-size:12px}}
</style></head><body>
<h1>DONmarket &mdash; etat du compte</h1>
{corps}
</body></html>"""


class _Poignee(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        with _verrou:
            copie = dict(_etat)
        corps = _page(copie).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *args):  # noqa: D102
        pass  # le journal HTTP noierait les messages utiles


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT_DEFAUT)
    parser.add_argument("--interval", type=int, default=RAFRAICHISSEMENT_S)
    args = parser.parse_args()

    fil = threading.Thread(target=_collecteur, args=(args.interval,), daemon=True)
    fil.start()

    # 127.0.0.1 et pas 0.0.0.0 : ce tableau montre un solde et des positions,
    # il n'a rien a faire sur le reseau local.
    serveur = HTTPServer(("127.0.0.1", args.port), _Poignee)
    print("=" * 62)
    print(f"  DONmarket -- tableau de bord LECTURE SEULE")
    print(f"  http://127.0.0.1:{args.port}")
    print(f"  premier releve dans quelques secondes, Ctrl+C pour arreter")
    print("=" * 62)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\narret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
