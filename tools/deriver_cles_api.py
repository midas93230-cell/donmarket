# -*- coding: utf-8 -*-
"""Derive les identifiants d'API du CLOB depuis la cle de signature.

POURQUOI CE N'EST PAS UNE PAGE A TROUVER. Polymarket n'affiche nulle part les
identifiants d'API : ils se DERIVENT. Le client signe un message avec la cle
privee, le CLOB repond avec une cle, un secret et une passphrase. C'est un
appel, pas un ecran -- d'ou les recherches infructueuses du 2026-08-20 dans les
cinq pages de reglages.

Ils sont deterministes : rederiver sur la meme cle rend les memes valeurs.
Perdre le .env ne perd donc rien tant que la cle privee existe.

TYPE DE SIGNATURE. Ce compte est un EOA ordinaire qui DETIENT lui-meme
l'USDC.e : c'est le type 0, et `POLYMARKET_FUNDER` reste vide. Les types 1 et 2
designent un proxy cree par Polymarket, ou la cle signe pour une adresse qui
n'est pas la sienne -- ce n'est pas notre montage.

Le secret et la passphrase sont SCELLES avant d'atteindre le disque, et rien
n'est imprime : ce sont des identifiants au meme titre que la cle.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, ".")

ENV = pathlib.Path(".env")


def poser(nom: str, valeur: str) -> None:
    texte = ENV.read_text(encoding="utf-8")
    if re.search(rf"^{nom}=", texte, flags=re.M):
        texte = re.sub(rf"^{nom}=.*$", f"{nom}={valeur}", texte, count=1, flags=re.M)
    else:
        texte += f"\n{nom}={valeur}\n"
    ENV.write_text(texte, encoding="utf-8")


def main() -> int:
    from dotenv import load_dotenv
    from py_clob_client.client import ClobClient

    from donmarket.store import vault

    load_dotenv(".env")
    cle = vault.read_secret("POLYMARKET_PRIVATE_KEY")
    if not cle:
        print("POLYMARKET_PRIVATE_KEY absente de .env")
        return 1

    client = ClobClient("https://clob.polymarket.com", key=cle, chain_id=137)
    print(f"adresse : {client.get_address()}")

    creds = client.create_or_derive_api_creds()

    poser("POLYMARKET_API_KEY", creds.api_key)
    poser("POLYMARKET_API_SECRET", vault.seal(creds.api_secret))
    poser("POLYMARKET_API_PASSPHRASE", vault.seal(creds.api_passphrase))
    # Type 0 : l'adresse qui signe est celle qui paie. `FUNDER` doit rester
    # vide, sinon le client cherche le collateral sur un proxy inexistant.
    poser("POLYMARKET_SIGNATURE_TYPE", "0")
    poser("POLYMARKET_FUNDER", "")

    print("\nIdentifiants derives et ecrits dans .env.")
    print("  POLYMARKET_API_KEY        pose")
    print("  POLYMARKET_API_SECRET     scelle")
    print("  POLYMARKET_API_PASSPHRASE scelle")
    print("  POLYMARKET_SIGNATURE_TYPE 0  (EOA qui detient lui-meme l'USDC.e)")
    print("  POLYMARKET_FUNDER         vide, comme l'exige le type 0")
    print("\nRien n'a ete affiche : ce sont des identifiants.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
