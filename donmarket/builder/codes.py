"""Le code builder — 32 octets, et six façons mesurées de se tromper.

Un « builder code » est l'identifiant qu'un outil attache aux ordres qu'il
route, et c'est par lui que Polymarket sait à qui verser les frais du programme
Builders. C'est donc la seule chaîne du dépôt dont une faute de frappe se paie
en revenu perdu, silencieusement et pour toujours.

## Pourquoi ce module existe alors qu'il ne fait que valider une chaîne

Parce que le serveur, lui, ne valide rien. Mesuré le 2026-08-13 contre
`clob.polymarket.com/builder/trades`, six variantes du MÊME code :

    0x0000…0001                          → 936 exécutions        (la bonne)
    0x01                                 → page vide, HTTP 200   (pas de complétion à 32 octets)
    0X0000…0001                          → page vide, HTTP 200   (le préfixe est sensible à la casse)
    0000…0001                            → page vide, HTTP 200   (le préfixe 0x est exigé)
    0xZZ00…0001                          → page vide, HTTP 200   (« ZZ » n'est pas de l'hexadécimal)
    CHOUCROUTE                           → page vide, HTTP 200   (chaîne arbitraire)

Les cinq mauvaises réponses sont **strictement identiques** à celle d'un code
valide mais sans volume : `{"data":[],"next_cursor":"LTE=","limit":300,"count":0}`.
Aucun code d'erreur, aucun champ de diagnostic. Un outil qui router ait ses
ordres avec `0X` au lieu de `0x` afficherait donc « 0 $ attribué » pendant des
mois sans qu'une seule ligne de journal ne signale l'erreur — et les frais
seraient réellement perdus, puisque l'attribution se joue à la signature de
l'ordre, pas à la lecture.

D'où la règle tenue ici : **on valide la FORME localement, avant tout appel**.
C'est le seul endroit où l'erreur est encore rattrapable.

Contre-exemple utile, et rassurant : le nom du paramètre, lui, EST vérifié
côté serveur. `?builderCode=` et l'absence de paramètre rendent tous deux un
**HTTP 400**. Cet endpoint ne partage donc pas le piège de Gamma, qui ignore un
nom de paramètre erroné et sert 200 lignes non filtrées (voir
`predictfun/crossvenue`). Le silence porte ici sur la VALEUR, pas sur la CLÉ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 0x suivi de 64 chiffres hexadécimaux = 32 octets. La casse du préfixe est
# imposée en minuscules parce que le serveur compare la chaîne telle quelle :
# `0X…` a été mesuré muet le 2026-08-13.
BUILDER_CODE_PATTERN = re.compile(r"^0x[0-9a-fA-F]{64}$")

BUILDER_CODE_LENGTH = 66  # 2 pour « 0x » + 64


class InvalidBuilderCode(ValueError):
    """La chaîne fournie ne peut pas être un code builder.

    Levée et non pas rendue sous forme de `None` : un code invalide qui
    poursuit son chemin ne provoque aucune panne, seulement une page vide qui
    ressemble à un compte à zéro. C'est exactement le mode de défaillance
    qu'on refuse.
    """


def normalise_builder_code(raw: str) -> str:
    """Rend le code sous la seule forme que le serveur reconnaît.

    Normalise ce qui est normalisable **sans rien deviner** :

    - les espaces de bord sont retirés (un copier-coller en traîne presque
      toujours) ;
    - le préfixe `0X` est ramené à `0x`, et les chiffres hexadécimaux à des
      minuscules — deux différences que le serveur traite comme un autre code,
      alors qu'elles désignent le même nombre.

    Ce qui n'est PAS normalisé, délibérément : la longueur. `0x01` n'est pas
    complété en `0x00…01`, bien que ce soit le même entier. Compléter
    reviendrait à décider qu'un utilisateur qui écrit `0x01` voulait dire
    `0x00…01` — plausible, mais invérifiable, et une erreur ici attribue le
    volume à un TIERS dont le code est réellement celui qu'on a fabriqué.
    Mieux vaut refuser.
    """
    if not isinstance(raw, str):
        raise InvalidBuilderCode(f"code builder attendu sous forme de chaîne, reçu {type(raw).__name__}")

    cleaned = raw.strip()
    if not cleaned:
        raise InvalidBuilderCode("code builder vide")

    if cleaned[:2] in ("0X", "0x"):
        cleaned = "0x" + cleaned[2:].lower()

    if not BUILDER_CODE_PATTERN.match(cleaned):
        raise InvalidBuilderCode(
            f"code builder invalide : {_describe_defect(cleaned)}. "
            "Forme attendue : 0x suivi de 64 chiffres hexadécimaux (32 octets). "
            "Le serveur rend une page VIDE — jamais une erreur — pour toute "
            "autre forme, donc cette faute ne se verrait pas à l'exécution."
        )
    return cleaned


def _describe_defect(cleaned: str) -> str:
    """Dit CE QUI cloche, pas seulement que ça cloche.

    Un message « code invalide » face à une chaîne de 66 caractères qui
    ressemble à un code oblige à compter les caractères à la main. Les trois
    fautes distinguées ici sont exactement celles mesurées le 2026-08-13.
    """
    if not cleaned.startswith("0x"):
        return f"préfixe « 0x » absent (commence par « {cleaned[:4]}… »)"
    body = cleaned[2:]
    if len(body) != 64:
        return f"{len(body)} chiffres après « 0x » au lieu de 64"
    return "caractères non hexadécimaux après « 0x »"


def is_valid_builder_code(raw: object) -> bool:
    """Prédicat sans exception, pour les chemins qui trient plutôt qu'ils n'exigent."""
    if not isinstance(raw, str):
        return False
    try:
        normalise_builder_code(raw)
    except InvalidBuilderCode:
        return False
    return True


@dataclass(frozen=True)
class BuilderCode:
    """Un code validé, transportable sans re-vérification.

    L'intérêt d'un type dédié plutôt que d'un `str` : une fois construit, il ne
    peut plus être malformé. Les fonctions qui en reçoivent un n'ont pas à
    re-valider, et celles qui reçoivent un `str` savent qu'elles doivent le
    faire. La frontière est visible dans les signatures.
    """

    value: str

    def __post_init__(self) -> None:
        # Normalise DANS le constructeur, comme `Book.__post_init__` impose
        # l'ordre du carnet : un invariant qui vit dans une fonction d'analyse
        # n'est pas tenu par les objets construits à la main dans les tests.
        object.__setattr__(self, "value", normalise_builder_code(self.value))

    def __str__(self) -> str:
        return self.value

    @property
    def short(self) -> str:
        """Forme abrégée pour l'affichage : 0x0000…0001."""
        return f"{self.value[:6]}…{self.value[-4:]}"
