# -*- coding: utf-8 -*-
"""Cree une cle de signature dediee et la scelle dans .env, sans l'afficher.

POURQUOI PLUTOT QU'UN EXPORT. La cle du portefeuille OKX existant ne s'exporte
pas facilement depuis son interface. Or `py-clob-client` doit signer chaque
ordre (EIP-712) : sans cle, rien ne part. Creer la cle ici renverse le
probleme -- c'est le portefeuille qui l'importe, pas nous qui l'extrayons.

CE QUE CE SCRIPT NE FAIT JAMAIS : afficher la cle. Elle est generee, scellee
par DPAPI et ecrite dans .env en une seule fois. Rien ne passe par la sortie
standard, donc rien ne reste dans l'historique du terminal ni dans un journal.
Seule l'ADRESSE est imprimee -- elle est publique par nature.

CE QUE ZA PROTEGE, ET CE QUE ZA NE PROTEGE PAS. Le scellement lie la valeur a
ce compte Windows sur cette machine : un .env copie ailleurs devient inerte.
Il ne protege pas d'un programme lance sous le meme compte, qui appellera
Unprotect exactement comme nous. DPAPI deplace la barriere, il ne la supprime
pas -- d'ou l'interet d'un portefeuille DEDIE, dont le solde est le seul
capital expose.

Lancement :
    .venv\\Scripts\\python tools\\creer_cle_signature.py
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, ".")

ENV = pathlib.Path(".env")
VARIABLE = "POLYMARKET_PRIVATE_KEY"


def main() -> int:
    from eth_account import Account

    from donmarket.store import vault

    texte = ENV.read_text(encoding="utf-8")
    ligne = re.search(rf"^{VARIABLE}=(.*)$", texte, flags=re.M)
    if ligne is None:
        print(f"{VARIABLE} absent de .env — ajoutez la ligne vide d'abord.")
        return 1
    if ligne.group(1).strip():
        # Ecraser une cle qui detient peut-etre des fonds serait irreversible :
        # la valeur precedente n'existe nulle part ailleurs.
        print(
            f"{VARIABLE} est DEJA renseigne. Ce script refuse de l'ecraser : "
            "une cle perdue l'est definitivement, avec ce qu'elle detient.\n"
            "Videz la ligne a la main si vous voulez vraiment repartir a neuf."
        )
        return 1

    compte = Account.create()
    scelle = vault.seal(compte.key.hex())

    ENV.write_text(
        re.sub(rf"^{VARIABLE}=.*$", f"{VARIABLE}={scelle}", texte, count=1, flags=re.M),
        encoding="utf-8",
    )

    print("Cle de signature creee et scellee dans .env.")
    print("Elle n'a ete affichee nulle part, y compris ici.\n")
    print(f"ADRESSE A IMPORTER / CONNECTER : {compte.address}\n")
    print(
        "Prochaines etapes, dans cet ordre :\n"
        "  1. importer cette cle dans OKX (page « Importation de la cle privee »)\n"
        "     -- il faudra la relire depuis .env, voir plus bas ;\n"
        "  2. connecter CE compte a polymarket.com ;\n"
        "  3. relever l'adresse de depot (Deposer -> Transfer Crypto) et la\n"
        "     mettre dans POLYMARKET_FUNDER ;\n"
        "  4. y envoyer l'USDC.\n"
    )
    print(
        "Pour reafficher la cle au moment de l'importer dans OKX, et seulement\n"
        "a ce moment :\n"
        "    .venv\\Scripts\\python -c \"from donmarket.store import vault;\\\n"
        "        print(vault.read_secret('POLYMARKET_PRIVATE_KEY'))\"\n"
        "Fermez le terminal juste apres : la ligne reste dans son historique."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
