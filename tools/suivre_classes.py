# -*- coding: utf-8 -*-
r"""Test HORS ECHANTILLON de la copie de traders classes -- LECTURE SEULE.

    .venv\Scripts\python tools\suivre_classes.py            # mesure du jour
    .venv\Scripts\python tools\suivre_classes.py --figer     # refige la selection

## Pourquoi cet outil existe

Le 2026-08-27, `list_trader_leaderboard` s'est revele INTESTABLE
retrospectivement, et pour une raison structurelle :

  - le classement ALL-TIME est un pantheon : 3 wallets sur 20 avaient trade dans
    les 7 derniers jours, dernier trade median il y a 72 jours ;
  - les classements MONTH et WEEK sont actifs (19/20 et 20/20) mais selectionnes
    PAR leur PnL recent -- mesurer leurs trades recents est CIRCULAIRE.

Le seul groupe non circulaire ne trade plus, le seul groupe actif est circulaire.
La seule sortie est donc de FIGER la selection a une date, puis de ne mesurer
QUE les trades posterieurs. C'est ce que fait ce fichier : la selection vit dans
`docs/leaderboard-snapshot.json`, et chaque passage ajoute une ligne datee a
`docs/suivi-classes.json`.

## Deux pieges deja payes, encodes ici

  - GAMMA : `/markets?condition_ids=X` filtre `closed=false` PAR DEFAUT et rend
    zero pour un marche resolu. Un nom de parametre errone est ignore EN
    SILENCE et l'API rend 20 marches quelconques. D'ou les deux passes et le
    filtrage sur les `conditionId` demandes.
  - CONCENTRATION : 1 273 trades sur 91 marches ne sont pas 1 273 observations.
    Mediane par TRADE +53,8 %, par MARCHE +2,1 % le meme jour. On agrege par
    marche : un marche, une voix.

Le seuil de decision est le HANDICAP DE COPIE mesure le 27/08 sur 446 achats :
la derive du prix est NULLE sous 15 minutes, donc copier ne coute que l'ecart,
~3,1 %. Un avantage qui ne le depasse pas ne vaut pas d'etre copie.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
INSTANTANE = "docs/leaderboard-snapshot.json"
SUIVI = "docs/suivi-classes.json"
HANDICAP = 0.031
PERIODES = ("ALL", "MONTH", "WEEK", "DAY")


def lignes(paginator, limite):
    """Un `Paginator` itere des PAGES ; les lignes sont dans `.items`."""
    out = []
    for page in paginator:
        items = getattr(page, "items", None)
        out.extend(items if items is not None else [page])
        if len(out) >= limite:
            break
    return out[:limite]


def figer(client, chemin: str = INSTANTANE) -> dict:
    """Ecrit la selection du jour.

    A NE PAS relancer chaque jour : refiger, c'est recommencer a selectionner
    sur la performance recente -- exactement le biais que cet outil existe pour
    eviter.
    """
    instantane = {"pris_le": datetime.now(timezone.utc).isoformat(), "classements": {}}
    for periode in PERIODES:
        rows = lignes(client.list_trader_leaderboard(time_period=periode,
                                                     order_by="PNL", page_size=20), 20)
        instantane["classements"][periode] = [
            {"rang": r.rank, "wallet": r.wallet, "nom": r.user_name,
             "pnl": str(r.pnl), "vol": str(r.vol)} for r in rows
        ]
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(instantane, f, indent=2, ensure_ascii=False)
    return instantane


def charger_marches(session, cids, cache):
    """Gamma, avec ses deux pieges neutralises."""
    reste = [c for c in cids if c not in cache]
    for i in range(0, len(reste), 20):
        lot = reste[i:i + 20]
        voulus = set(lot)
        for ferme in ("true", "false"):
            try:
                r = session.get(GAMMA,
                                params=[("condition_ids", x) for x in lot]
                                + [("closed", ferme), ("limit", "40")],
                                timeout=25)
                for m in r.json():
                    if m.get("conditionId") in voulus:
                        cache[m["conditionId"]] = m
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
    for c in reste:
        cache.setdefault(c, None)


def retour(session, obs, cache):
    """Resolu -> paiement reel (0 ou 1). Ouvert -> marque au BID.

    Jamais au dernier prix : le 2026-08-26, marquer une position au prix de sa
    vente NON REMPLIE avait gonfle le total de 1,65 $.
    """
    m = cache.get(obs["cid"])
    if m is None:
        return None
    if m.get("closed"):
        try:
            prix_res = json.loads(m.get("outcomePrices") or "[]")
            return float(prix_res[obs["idx"]]) / obs["prix"] - 1
        except Exception:  # noqa: BLE001
            return None
    try:
        token = json.loads(m["clobTokenIds"])[obs["idx"]]
        b = session.get(f"{CLOB}/book", params={"token_id": token}, timeout=20).json()
        bids = b.get("bids") or []
        return float(bids[-1]["price"]) / obs["prix"] - 1 if bids else None
    except Exception:  # noqa: BLE001
        return None


def mesurer(client, session, wallets, depuis, cache):
    obs = []
    for w in wallets:
        try:
            actes = lignes(client.list_activity(user=w, page_size=100), 300)
        except Exception:  # noqa: BLE001
            continue
        for a in actes:
            if getattr(a, "type", "") != "TRADE" or getattr(a, "side", "") != "BUY":
                continue
            ts = getattr(a, "timestamp", None)
            if ts is None or ts <= depuis:
                continue          # LE COEUR DU TEST : rien d'anterieur au gel
            prix = float(getattr(a, "price", 0) or 0)
            cid = str(getattr(a, "condition_id", "") or "")
            idx = getattr(a, "outcome_index", None)
            if not (0 < prix < 1) or not cid or idx is None:
                continue
            obs.append({"cid": cid, "idx": int(idx), "prix": prix, "wallet": w})
        time.sleep(0.15)

    charger_marches(session, sorted({o["cid"] for o in obs}), cache)
    par_marche = {}
    for o in obs:
        r = retour(session, o, cache)
        if r is not None:
            par_marche.setdefault(o["cid"], []).append(r)
    if not par_marche:
        return {"achats": len(obs), "marches": 0}
    voix = sorted(sum(v) / len(v) for v in par_marche.values())
    return {
        "achats": len(obs),
        "marches": len(voix),
        "median_par_marche": voix[len(voix) // 2],
        "moyen_par_marche": sum(voix) / len(voix),
        "marches_gagnants": sum(1 for x in voix if x > 0),
        "wallets_actifs": len({o["wallet"] for o in obs}),
    }


def main() -> int:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--figer", action="store_true",
                        help="refige la selection (efface le caractere hors echantillon)")
    args = parser.parse_args()

    load_dotenv(".env", override=True)
    import httpx
    from polymarket import PublicClient

    client, session = PublicClient(), httpx.Client()

    if args.figer or not os.path.exists(INSTANTANE):
        instantane = figer(client)
        print(f"selection figee au {instantane['pris_le']}")
    else:
        instantane = json.load(open(INSTANTANE, encoding="utf-8"))

    depuis = datetime.fromisoformat(instantane["pris_le"])
    age_h = (datetime.now(timezone.utc) - depuis).total_seconds() / 3600
    print(f"selection figee il y a {age_h:.1f} h ; "
          f"on ne mesure QUE les trades posterieurs")
    if age_h < 2:
        print("-> trop tot pour conclure quoi que ce soit ; repasser demain.")

    cache = {}
    resultats = {"mesure_le": datetime.now(timezone.utc).isoformat(),
                 "fige_le": instantane["pris_le"],
                 "heures_ecoulees": round(age_h, 2),
                 "handicap_copie": HANDICAP,
                 "groupes": {}}
    for periode in PERIODES:
        wallets = [r["wallet"] for r in instantane["classements"].get(periode, [])
                   if r.get("wallet")]
        bilan = mesurer(client, session, wallets, depuis, cache)
        resultats["groupes"][periode] = bilan
        if bilan.get("marches"):
            verdict = "AU-DESSUS" if bilan["median_par_marche"] > HANDICAP else "sous"
            print(f"  {periode:6} : {bilan['achats']:>4} achats / "
                  f"{bilan['marches']:>3} marches "
                  f"({bilan['wallets_actifs']} wallets actifs) | median par marche "
                  f"{bilan['median_par_marche']*100:+6.1f} % | {verdict} le handicap "
                  f"de {HANDICAP*100:.1f} %")
        else:
            print(f"  {periode:6} : {bilan['achats']:>4} achats, "
                  f"rien de mesurable encore")

    histo = []
    if os.path.exists(SUIVI):
        try:
            histo = json.load(open(SUIVI, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            histo = []
    histo.append(resultats)
    with open(SUIVI, "w", encoding="utf-8") as f:
        json.dump(histo, f, indent=2, ensure_ascii=False)
    print(f"\n{len(histo)} releve(s) dans {SUIVI}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
