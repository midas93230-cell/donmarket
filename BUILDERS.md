# Devenir builder Polymarket — ce qui reste à faire à la main

État au 2026-08-17. Le code de DONmarket est prêt (`donmarket/builder/`,
`python -m donmarket builder`). Ce qui suit ne peut PAS être fait en codant :
il faut un compte connecté et une approbation humaine chez Polymarket.

Avancement : étape 1 faite (code builder obtenu, attribution branchée sur le
client CLOB), étape 2 envoyée le 2026-08-17, étape 3 soumise le 2026-08-17.
Les deux dernières attendent une réponse humaine, annoncée « within a few
business days ».

## Le fait qui commande tout

Le classement officiel trie par **volume**. Mesuré sur les 12 premiers :
**7 ne prélèvent rien**. Le n° 1 (betmoar, 29 M$/semaine) encaisse **zéro**.
Le volume n'est pas le revenu — c'est le taux × le volume, et le taux est un
réglage, pas une fatalité.

## Les trois étapes, dans l'ordre

### 1. Se connecter et récupérer les identifiants

`polymarket.com → Settings → Builders` (sur un compte **connecté** — au
2026-08-15 la session de ce navigateur était déconnectée).

Il y a **deux choses à copier**, et les confondre coûte tout le revenu :

| Élément | Forme | Rôle |
|---|---|---|
| Code builder | `0x` + 64 hexadécimaux | **Lecture seule** — savoir ce qui a été attribué |
| Identifiants d'API builder | `key` / `secret` / `passphrase` | **Attribuent réellement** — ils signent les en-têtes |

Le code seul n'attribue **rien**. L'attribution se joue à la signature de
l'ordre, jamais après coup : un ordre parti sans en-tête signé est perdu
définitivement.

Les coller dans `.env` (jamais dans un fichier suivi par git) :

```
POLYMARKET_BUILDER_CODE=
POLYMARKET_BUILDER_API_KEY=
POLYMARKET_BUILDER_API_SECRET=
POLYMARKET_BUILDER_API_PASSPHRASE=
```

Vérification immédiate, sans passer le moindre ordre :

```
.venv\Scripts\python -c "from donmarket.builder import attribution_status; print(attribution_status())"
```

Un code mal recopié est signalé sur-le-champ. C'est le seul moment où l'erreur
est rattrapable : côté serveur, un code malformé rend une page **vide** avec un
HTTP 200, strictement indiscernable d'un compte à zéro légitime.

### 2. Demander le palier « Verified » — pour le leaderboard, pas pour le droit de facturer

Le palier par défaut est **Unverified**. Ce qu'il coûte réellement, relevé sur
`docs.polymarket.com/programs/builders/tiers` le 2026-08-16 :

| | Unverified | Verified | Partner |
|---|---|---|---|
| Transactions relayer/jour | **100** | 10 000 | illimité |
| Frais builder | oui | oui | oui |
| Visibilité leaderboard | **non** | oui | oui |

**Correction d'une affirmation antérieure de ce fichier** : il y était écrit
qu'Unverified n'a « aucun droit de facturer ». C'est faux — le tableau des
paliers accorde les frais aux trois niveaux. Les deux vraies contraintes sont
le plafond de **100 transactions par jour**, qu'une stratégie de tenue de marché
épuise en une heure en recotant deux branches à la minute, et l'**absence du
leaderboard**, qui est le seul canal par lequel un teneur de liquidité peut
découvrir l'outil.

Courriel à **builder@polymarket.com**, avec clé d'API, cas d'usage, volume
attendu ; réponse annoncée « within a few business days ».

Texte prêt à envoyer — tout est déjà rempli sauf la clé et le nom :

> **À :** builder@polymarket.com
> **Objet :** Verified builder tier request — DONmarket
>
> Hello,
>
> I'm requesting the Verified builder tier for DONmarket, an open-source
> measurement and market-making toolkit for Polymarket.
>
> - **Builder name:** DONmarket
> - **Builder code:** `0xfc74d798dceeb76af5d5a7b8b385729ae46e2eb29ffb971dd791a9164faf0162`
> - **Builder API key:** `<coller la clé UNIQUEMENT — jamais le secret ni la passphrase>`
> - **Repository:** https://github.com/midas93230-cell/donmarket
> - **Live measurements:** https://midas93230-cell.github.io/donmarket/
>
> **What it does.** DONmarket measures liquidity-reward economics on
> Polymarket and quotes both sides of rewarded markets. It scores pool
> competition with your published scoring function, corrects for the fact
> that a posted order moves the midpoint it measures its own distance from,
> and replays historical fills to price inventory drift. It refuses to report
> a yield it cannot measure — 469 tests, and the disproven strategies stay
> documented on the page rather than being quietly deleted.
>
> **Why it may interest you.** While integrating the Builders program I
> measured a few things that aren't published anywhere: the fee base is USDC
> notional rather than shares (settled by dispersion analysis across 16
> builder-side pairs), the documented 100 bps taker cap is exceeded in
> production, and most top-volume builders charge nothing at all. The page
> above shows the method and the evidence. Happy to be corrected on any of it.
>
> **Expected volume:** starting from zero — the toolkit is new and I'm
> onboarding liquidity providers rather than retail flow. Fee rates are set
> deliberately low (10 bps taker / 5 bps maker) for that audience.
>
> Happy to provide anything else you need.
>
> Best,
> `<ton nom>`

**Ne jamais mettre le secret ni la passphrase dans un courriel.** La clé
publique suffit à t'identifier. Le code builder, lui, est public : il figure
dans chaque ligne de `/builder/trades`.

Pourquoi annoncer un volume de zéro plutôt que de gonfler le chiffre : ils
voient ton volume réel dans leur propre base. Un chiffre inventé est la seule
chose qui puisse faire refuser un dossier par ailleurs solide.

### 3. (Optionnel) Candidater à la subvention

Formulaire sur `builders.polymarket.com` — dossier **distinct** du code builder,
pour les subventions (2,5 M$ annoncés) et la mise en avant sur le leaderboard.
Champs : Product Name, Project Description, Website URL, Email, X Handle,
Telegram Handle, Builder API key.

Le formulaire délivre aussi un **badge de constructeur**, indépendamment de la
décision de subvention : la visibilité est acquise même si la subvention est
refusée. Piège mesuré : le jeton Cloudflare Turnstile **expire en 5 minutes** et
regrise le bouton d'envoi — valider le captcha puis soumettre dans la foulée.
L'aperçu de badge affiché à gauche est une illustration statique, il ne reflète
jamais les champs saisis.

Brouillon de description :

> DONmarket is an open-source Python toolkit for Polymarket liquidity
> provision. It scores reward-pool competition using the published scoring
> function, replays historical fills to measure inventory drift, and refuses to
> report a yield it cannot measure. 469 tests. It also maps the Builders
> program itself: it infers each builder's actual fee schedule from attributed
> executions, which reveals that most top-volume builders charge nothing.

## Le chiffre à garder en tête

Revenu = volume routé × taux. Pour **20 $/jour** :

| Barème | Volume/jour nécessaire |
|---|---|
| 10/5 bps (traderline) | 26 667 $ |
| 50/25 bps (polymtrade) | 5 333 $ |
| 100/50 bps (Bullpen, Polycule) | 2 667 $ |

Ce n'est pas un mur. Mais ça ne dépend pas de savoir trader — ça dépend d'avoir
des **utilisateurs**. C'est le vrai chantier, et aucune ligne de code ne le
remplace.

## Ce qui n'est pas vérifiable

Le palier atteint (Unverified / Verified / Partner) **ne se lit nulle part dans
l'API**. Le code ne prétendra donc jamais savoir si les frais seront réellement
perçus : `attribution_status()["tier_is_unknown"]` vaut toujours `True`.
