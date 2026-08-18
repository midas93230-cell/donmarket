# Signeur builder — pourquoi il existe et comment il se déploie

## Le fait qui rend ce service nécessaire

L'attribution du volume Polymarket passe **à 100 % par quatre en-têtes signés**
avec le secret d'API builder. Vérifié sur le format de fil, pas sur la
documentation : `order_to_json` produit exactement
`{order, owner, orderType, postOnly}` — le corps de l'ordre ne porte aucun champ
builder, et le code builder public (`0x` + 64 hexadécimaux) est un identifiant de
**lecture** qui n'attribue rien.

Conséquence, qui décide de toute l'économie du programme :

> Un utilisateur tiers qui clone ce dépôt public n'a pas le secret, donc son
> volume n'est attribué à personne. Publier le secret pour y remédier ferait
> révoquer le compte builder.

Le SDK prévoit la sortie : `BuilderType.REMOTE`. Le client envoie
`{method, path, body, timestamp}` à une URL de signature et reçoit les quatre
en-têtes. **Le tiers ne voit jamais le secret ; sa clé privée ne quitte jamais sa
machine.** Ce Worker est cette URL.

## Ce que le service voit, dit sans détour

La signature couvre le corps de l'ordre : le service **reçoit donc chaque ordre
avant qu'il n'atteigne le carnet**. C'est inhérent au protocole, pas un choix
d'architecture, et c'est l'objection qu'un teneur de liquidité sérieux posera en
premier. Elle est légitime.

Ce qui est fait en réponse : le code est public et tient dans un seul fichier
lisible, il ne journalise **ni corps d'ordre, ni en-têtes, ni jeton**, et rien
n'est stocké. Une promesse de non-rétention ne vaut que si le code la tient.

## Deux pièges du SDK, mesurés le 2026-08-17

| Piège | Ce qui se passe | Où c'est traité |
|---|---|---|
| `REMOTE` est cassé | `http_helpers.post()` rend `resp.json()`, donc un `dict`, et `py_clob_client` appelle `.to_dict()` dessus → `AttributeError` au moment d'envoyer un ordre | `donmarket/builder/remote.py` reconvertit, plutôt qu'un correctif de singe sur le paquet installé, qui redeviendrait cassé au premier `pip install --upgrade` |
| Un raté n'arrête pas l'ordre | Si les en-têtes valent `None`, le client repart sur un `post()` **sans attribution** : l'ordre s'exécute, les frais sont perdus définitivement | Le raté est compté (`config.misses`) et journalisé bruyamment. L'ordre n'est **pas** bloqué : un ordre non attribué ne coûte rien à celui qui le passe, le bloquer retournerait l'outil contre son utilisateur |

## Vérifier avant de déployer

```bash
node signer/verify-parity.mjs
```

Signe six cas des deux côtés — Worker et SDK Python — et compare les chaînes. Les
cas ne sont pas décoratifs : l'apostrophe est là parce que l'implémentation
d'origine remplace `'` par `"` avant de signer (pour que Python, Go et TypeScript
produisent le même message), et l'accent parce que le message est haché en UTF-8.
Deux endroits où une réécriture naïve diverge en silence.

Un octet d'écart et le CLOB rejette l'ordre sans dire pourquoi. **Ne pas déployer
si un seul cas diverge.**

## Déployer

```bash
cd signer
wrangler secret put BUILDER_API_KEY
wrangler secret put BUILDER_API_SECRET
wrangler secret put BUILDER_API_PASSPHRASE
wrangler secret put AUTH_TOKEN          # jeton porteur exigé des clients
wrangler deploy
```

Les quatre valeurs ne figurent **jamais** dans `wrangler.toml`, qui est versionné
et public. Une variable posée dans `[vars]` au lieu de `secret put` est la seule
erreur de ce dossier qui coûte le compte builder.

## Côté client

```
POLYMARKET_BUILDER_REMOTE_URL=https://donmarket-signer.midas93230.workers.dev
POLYMARKET_BUILDER_REMOTE_TOKEN=<le jeton distribué>
```

L'URL est publique : elle ne donne rien sans le jeton, qui se transmet
individuellement. Un appel sans jeton est refusé en **401**, mesuré en production
le 2026-08-18, en même temps que la conformité des signatures : quatre cas
signés par le Worker en ligne, quatre signatures identiques à celles du SDK
Python local.

`python -m donmarket builder` rapporte alors `attribution_mode: remote` et
`can_attribute: true`. Les identifiants **locaux**, s'ils sont présents, priment :
l'opérateur signe chez lui, un aller-retour réseau par ordre serait absurde.
