# -*- coding: utf-8 -*-
"""Convertit l'USDC NATIF en USDC.e sur Polygon, le collateral de Polymarket.

POURQUOI CETTE ETAPE EXISTE. Binance ne retire que de l'USDC natif
(0x3c499c54...) sur Polygon ; son entree « MATICUSDCE / Bridged USDC » est un
canal de depot, pas un actif echangeable -- verifie le 2026-08-20, solde 0 et
absent de la conversion. Or `py_clob_client/config.py` fixe le collateral a
0x2791Bca1... c'est-a-dire l'USDC.e ponte. Envoyer le mauvais jeton ne perd
rien, mais Polymarket ne le voit pas.

Les deux jetons valent le meme dollar et s'echangent dans un pool Uniswap v3 a
0,01 % de frais. Le cout reel est le gaz, quelques centimes sur Polygon.

CE QUE CE SCRIPT NE FAIT PAS :
  - il ne s'execute pas tout seul : c'est une commande que l'operateur lance,
    comme `--arm` ailleurs dans ce depot ;
  - il ne devine pas le pool : si le pool n'existe pas au palier de frais vise,
    il s'arrete au lieu d'essayer autre chose ;
  - il n'accepte pas n'importe quel prix : `amountOutMinimum` plafonne la perte
    a 0,5 %, et la transaction echoue plutot que de subir pire.

Sans argument, il ne fait que LIRE et montrer ce qu'il ferait.
Avec `--arm`, il signe et envoie.

    .venv\\Scripts\\python tools\\convertir_usdc.py
    .venv\\Scripts\\python tools\\convertir_usdc.py --arm
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

sys.path.insert(0, ".")

RPC = "https://polygon-bor-rpc.publicnode.com"
CHAIN_ID = 137

USDC_NATIF = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"  # Uniswap SwapRouter02
FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

FEE_TIER = 100  # 0,01 % : le palier des paires de stables
SLIPPAGE = 0.005  # 0,5 % maximum de perte toleree
GAS_APPROVE = 120_000
GAS_SWAP = 300_000


def rpc(method: str, params: list) -> object:
    reponse = httpx.post(
        RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=40,
    ).json()
    if "error" in reponse:
        raise RuntimeError(f"{method} : {reponse['error']}")
    return reponse.get("result")


def selecteur(signature: str) -> bytes:
    from eth_utils import keccak

    return keccak(text=signature)[:4]


def solde_erc20(token: str, compte: str) -> int:
    data = "0x70a08231" + "0" * 24 + compte[2:].lower()
    brut = rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return int(brut, 16) if brut and brut != "0x" else 0


def pool_existe(fee: int) -> str | None:
    from eth_abi import encode

    data = "0x" + (
        selecteur("getPool(address,address,uint24)")
        + encode(["address", "address", "uint24"], [USDC_NATIF, USDC_E, fee])
    ).hex()
    brut = rpc("eth_call", [{"to": FACTORY, "data": data}, "latest"])
    adresse = "0x" + brut[-40:]
    return None if int(adresse, 16) == 0 else adresse


def envoyer(compte, to: str, data: str, gas: int) -> str:
    nonce = int(rpc("eth_getTransactionCount", [compte.address, "pending"]), 16)
    prix = int(rpc("eth_gasPrice", []), 16)
    # Marge de 25 % : sur Polygon le prix du gaz bouge vite, et une transaction
    # sous-payee reste bloquee au lieu d'echouer proprement.
    tx = {
        "to": to,
        "value": 0,
        "gas": gas,
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
    raise TimeoutError(f"aucun reçu pour {tx_hash} apres {secondes}s")


def main() -> int:
    from eth_abi import encode
    from eth_account import Account

    # `.env` n'est charge que par le CLI de donmarket : un script autonome doit
    # le faire lui-meme, sinon `read_secret` ne voit rien et le message parle
    # d'une cle absente alors qu'elle est bien la.
    from dotenv import load_dotenv

    from donmarket.store import vault

    load_dotenv(".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true", help="signer et envoyer")
    args = parser.parse_args()

    cle = vault.read_secret("POLYMARKET_PRIVATE_KEY")
    if not cle:
        print("POLYMARKET_PRIVATE_KEY absente de .env")
        return 1
    compte = Account.from_key(cle)
    print(f"compte : {compte.address}")

    natif = solde_erc20(USDC_NATIF, compte.address)
    ponte = solde_erc20(USDC_E, compte.address)
    gaz = int(rpc("eth_getBalance", [compte.address, "latest"]), 16)
    print(f"  USDC natif : {natif / 1e6:.6f}")
    print(f"  USDC.e     : {ponte / 1e6:.6f}")
    print(f"  POL        : {gaz / 1e18:.6f}")

    if natif == 0:
        print("\nRien a convertir.")
        return 0
    if gaz == 0:
        print("\nAucun POL : impossible de payer le gaz.")
        return 1

    pool = pool_existe(FEE_TIER)
    if pool is None:
        print(f"\nAucun pool USDC/USDC.e au palier {FEE_TIER}. On s'arrete ici")
        print("plutot que d'essayer un autre palier au hasard.")
        return 1
    print(f"\npool {FEE_TIER / 10000:.2f} % : {pool}")

    minimum = int(natif * (1 - SLIPPAGE))
    print(f"convertir {natif / 1e6:.6f} USDC natif")
    print(f"  minimum accepte : {minimum / 1e6:.6f} USDC.e (perte max 0,5 %)")

    if not args.arm:
        print("\nLECTURE SEULE — rien n'a ete envoye.")
        print("Relancer avec --arm pour signer les deux transactions.")
        return 0

    print("\n1/2 autorisation du routeur...")
    data_approve = "0x" + (
        selecteur("approve(address,uint256)")
        + encode(["address", "uint256"], [ROUTER, natif])
    ).hex()
    h1 = envoyer(compte, USDC_NATIF, data_approve, GAS_APPROVE)
    print(f"    {h1}")
    r1 = attendre(h1)
    if int(r1.get("status", "0x0"), 16) != 1:
        print("    ECHEC de l'autorisation — on s'arrete.")
        return 1
    print("    ok")

    print("2/2 echange...")
    params = (
        USDC_NATIF, USDC_E, FEE_TIER, compte.address, natif, minimum, 0,
    )
    data_swap = "0x" + (
        selecteur(
            "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
        )
        + encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"], [params]
        )
    ).hex()
    h2 = envoyer(compte, ROUTER, data_swap, GAS_SWAP)
    print(f"    {h2}")
    r2 = attendre(h2)
    if int(r2.get("status", "0x0"), 16) != 1:
        print("    ECHEC de l'echange. L'USDC natif est intact.")
        return 1
    print("    ok")

    print("\nSoldes apres :")
    print(f"  USDC natif : {solde_erc20(USDC_NATIF, compte.address) / 1e6:.6f}")
    print(f"  USDC.e     : {solde_erc20(USDC_E, compte.address) / 1e6:.6f}")
    print(f"  POL        : {int(rpc('eth_getBalance', [compte.address, 'latest']), 16) / 1e18:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
