# -*- coding: utf-8 -*-
"""Autorise les contrats Polymarket a deplacer l'USDC.e et les parts.

POURQUOI. Un ordre signe ne suffit pas : au moment de l'appariement, le
contrat d'echange doit pouvoir PRENDRE l'USDC.e du compte acheteur et lui
REMETTRE les parts. Sans autorisation prealable, l'ordre est accepte par le
CLOB puis echoue a l'execution -- un echec tardif, cote chaine, qui ne
ressemble pas a sa cause.

QUATRE AUTORISATIONS, et pas deux, parce qu'il y a DEUX echanges :

  - l'echange ordinaire        0x4bFb41d5...
  - l'echange « neg risk »     0xC5d563A3...   (marches a issues multiples)

Chacun a besoin de deux droits :
  - `approve` sur l'USDC.e     -> pour prendre le collateral
  - `setApprovalForAll` sur le -> pour deplacer les parts (ERC1155)
    contrat de parts

Les adresses ne sont pas ecrites de memoire : elles viennent de
`py_clob_client/config.py`, c'est-a-dire du client qui signera les ordres.
Toute autre source risquerait de diverger de lui.

MONTANT AUTORISE. On autorise le MAXIMUM (2^256-1), comme le fait l'interface
de Polymarket. C'est un choix conscient : autoriser le montant exact
obligerait a une transaction -- donc du gaz -- avant chaque ordre, ce qui
rendrait la tenue de marche impraticable. La contrepartie est reelle : si le
contrat d'echange etait compromis, il pourrait vider l'USDC.e du compte. C'est
la raison pour laquelle ce compte est DEDIE et ne detient que le capital de
trading.

Sans argument : lecture seule, montre ce qui manque.
Avec `--arm` : signe et envoie ce qui manque, et rien d'autre.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

sys.path.insert(0, ".")

RPC = "https://polygon-bor-rpc.publicnode.com"
CHAIN_ID = 137

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

MAX_UINT = (1 << 256) - 1
GAS = 120_000


def rpc(method: str, params: list) -> object:
    reponse = httpx.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=40,
    ).json()
    if "error" in reponse:
        raise RuntimeError(f"{method} : {reponse['error']}")
    return reponse.get("result")


def selecteur(signature: str) -> bytes:
    from eth_utils import keccak

    return keccak(text=signature)[:4]


def allowance(proprietaire: str, depensier: str) -> int:
    from eth_abi import encode

    data = "0x" + (
        selecteur("allowance(address,address)")
        + encode(["address", "address"], [proprietaire, depensier])
    ).hex()
    brut = rpc("eth_call", [{"to": USDC_E, "data": data}, "latest"])
    return int(brut, 16) if brut and brut != "0x" else 0


def approuve_parts(proprietaire: str, operateur: str) -> bool:
    from eth_abi import encode

    data = "0x" + (
        selecteur("isApprovedForAll(address,address)")
        + encode(["address", "address"], [proprietaire, operateur])
    ).hex()
    brut = rpc("eth_call", [{"to": CTF, "data": data}, "latest"])
    return bool(int(brut, 16)) if brut and brut != "0x" else False


def envoyer(compte, to: str, data: str) -> str:
    nonce = int(rpc("eth_getTransactionCount", [compte.address, "pending"]), 16)
    prix = int(rpc("eth_gasPrice", []), 16)
    tx = {
        "to": to,
        "value": 0,
        "gas": GAS,
        "gasPrice": int(prix * 1.25),
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "data": data,
    }
    signee = compte.sign_transaction(tx)
    return str(rpc("eth_sendRawTransaction", ["0x" + signee.raw_transaction.hex()]))


def attendre(tx_hash: str, secondes: int = 180) -> dict:
    debut = time.monotonic()
    while time.monotonic() - debut < secondes:
        recu = rpc("eth_getTransactionReceipt", [tx_hash])
        if recu:
            return recu  # type: ignore[return-value]
        time.sleep(3)
    raise TimeoutError(f"aucun recu pour {tx_hash}")


def main() -> int:
    from dotenv import load_dotenv
    from eth_abi import encode
    from eth_account import Account

    from donmarket.store import vault

    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()

    load_dotenv(".env")
    cle = vault.read_secret("POLYMARKET_PRIVATE_KEY")
    if not cle:
        print("POLYMARKET_PRIVATE_KEY absente de .env")
        return 1
    compte = Account.from_key(cle)
    print(f"compte : {compte.address}\n")

    manquantes = []
    for nom, echange in (("echange", EXCHANGE), ("neg risk", NEG_RISK_EXCHANGE)):
        # Un SEUIL plutot qu'une egalite : certaines interfaces autorisent une
        # valeur tres grande sans etre exactement 2^256-1.
        ok_usdc = allowance(compte.address, echange) > (1 << 200)
        print(f"USDC.e -> {nom:<9} : {'ok' if ok_usdc else 'MANQUE'}")
        if not ok_usdc:
            manquantes.append(
                (
                    f"approve USDC.e pour {nom}",
                    USDC_E,
                    "0x"
                    + (
                        selecteur("approve(address,uint256)")
                        + encode(["address", "uint256"], [echange, MAX_UINT])
                    ).hex(),
                )
            )

        ok_parts = approuve_parts(compte.address, echange)
        print(f"parts  -> {nom:<9} : {'ok' if ok_parts else 'MANQUE'}")
        if not ok_parts:
            manquantes.append(
                (
                    f"setApprovalForAll parts pour {nom}",
                    CTF,
                    "0x"
                    + (
                        selecteur("setApprovalForAll(address,bool)")
                        + encode(["address", "bool"], [echange, True])
                    ).hex(),
                )
            )

    gaz = int(rpc("eth_getBalance", [compte.address, "latest"]), 16)
    print(f"\nPOL disponible : {gaz / 1e18:.6f}")

    if not manquantes:
        print("\nTout est deja autorise. Rien a envoyer.")
        return 0

    print(f"\n{len(manquantes)} transaction(s) manquante(s) :")
    for libelle, _, _ in manquantes:
        print(f"  . {libelle}")

    if not args.arm:
        print("\nLECTURE SEULE -- rien n'a ete envoye.")
        print("Relancer avec --arm pour les envoyer.")
        return 0

    for libelle, contrat, data in manquantes:
        print(f"\n{libelle}...")
        tx = envoyer(compte, contrat, data)
        print(f"    {tx}")
        recu = attendre(tx)
        if int(recu.get("status", "0x0"), 16) != 1:
            # On s'arrete a la premiere qui echoue : enchainer les suivantes
            # depenserait du gaz sur un compte dont on ne comprend deja plus
            # l'etat.
            print("    ECHEC -- on s'arrete ici.")
            return 1
        print("    ok")

    print("\nAutorisations posees. Verification :")
    for nom, echange in (("echange", EXCHANGE), ("neg risk", NEG_RISK_EXCHANGE)):
        usdc_ok = allowance(compte.address, echange) > (1 << 200)
        print(f"  USDC.e -> {nom:<9} : {usdc_ok}")
        print(f"  parts  -> {nom:<9} : {approuve_parts(compte.address, echange)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
