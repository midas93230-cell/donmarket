# DONmarket

Moteur de lecture, d'analyse et (à terme) d'exécution sur Polymarket.

*[English version](README.md) — ce README français est le journal d'ingénierie
de référence ; la version anglaise en est un résumé fidèle.*

## État au 2026-08-06

**Fait et testé en réel :**

- Lecture de l'univers complet des marchés ouverts (2 100 marchés, plafond de l'API).
- Récupération des carnets d'ordres réels par lots parallèles (4 200 carnets).
- Moteur de détection d'arbitrage de jeu complet, avec deux régimes de seuils.
- **Stratégie de récompenses de liquidité**, mesurée et branchée sur la CLI :
  entonnoir complet, risque d'inventaire chiffré, contrainte de portefeuille.
- **Flux WebSocket temps réel** sur les jetons des candidats retenus, avec
  recalcul continu du rendement (protocole vérifié par réconciliation).
- **Moyenne temporelle** de la liquidité concurrente : c'est elle qui fait foi,
  l'instantané n'étant qu'un tirage (voir la quatrième mesure).
- **Tableau de bord local** (`serve`), en boucle locale seule, sans dépendance.
- **Vote d'ensemble à N modèles** (`consensus`), construit et mesuré — verdict
  dans la cinquième mesure.
- **Score de récompense selon la formule publiée par Polymarket**, effet de
  notre propre ordre sur le milieu compris (sixième mesure).
- **Rejeu de tenue de marché sur historique** (`backtest`), qui a démenti le
  majorant de risque et l'a remplacé (septième mesure).
- **Moteur d'exécution** (`trade`), désarmé par défaut : sans `--arm`, le
  chemin complet est parcouru, plafonds compris, et s'arrête avant signature.
  **Aucun ordre réel n'est jamais parti** — voir « Ce qui n'est pas fait ».
- **Compte de démonstration** (`paper`) : capital fictif, ordres confrontés aux
  vraies exécutions du marché, récompense notée sur les ordres tels qu'ils
  dorment (huitième mesure).
- Persistance SQLite (historique des marchés, des scans et des opportunités).
- 291 tests, dont cinq régressions payées cher (voir « Pièges »).

**Performance mesurée :** 2 100 marchés + 4 200 carnets analysés en **13,5 s** ;
un scan de récompenses complet (2 099 marchés, 948 carnets, 60 historiques à la
minute) en **48 à 71 s**.

## Utilisation

```bash
.venv/Scripts/python -m donmarket scan --mode serieux --bankroll 14.47
.venv/Scripts/python -m donmarket scan --mode normal --max-markets 300
.venv/Scripts/python -m donmarket rewards --bankroll 100
.venv/Scripts/python -m donmarket serve --bankroll 100   # http://127.0.0.1:8787
.venv/Scripts/python -m donmarket consensus --members 31 --threshold 28
.venv/Scripts/python -m donmarket backtest
.venv/Scripts/python -m donmarket paper --bankroll 100 --minutes 30
.venv/Scripts/python -m donmarket stats
.venv/Scripts/python -m pytest tests -q

# Le seul chemin qui peut engager de l'argent. Sans --arm, rien ne part.
.venv/Scripts/python -m donmarket trade --max-total 20 --max-per-market 10
```

Aucune clé n'est requise **sauf pour `trade --arm`** : tout le reste passe par
les API publiques en lecture seule, y compris `paper`, qui n'envoie aucun ordre.

## Le « mode sérieux »

Il n'existe ni transe ni état de grâce dans un programme. Ce que le mode sérieux
fait réellement, et qui est bien plus utile : exiger que **plusieurs conditions
strictes soient réunies simultanément** avant de retenir une opportunité, et
rester immobile le reste du temps.

| Condition | Mode normal | Mode sérieux |
|---|---|---|
| Marge nette minimale | 0 | 0,01 $ par jeu |
| Profondeur réelle au carnet | — | 50 $ |
| Volume 24 h du marché | — | 1 000 $ |
| Spread maximal (par branche) | — | 0,03 |
| Délai de résolution | — | 90 jours |

Le mode sérieux ne rend pas les décisions meilleures par magie : il **réduit le
nombre de décisions**, ce qui est le seul levier réel quand aucun edge n'est
mesuré.

## Résultat de la première mesure complète (2026-07-28)

Sur **1 937 marchés** dont les deux carnets étaient lisibles :

| Coût d'achat d'un jeu complet | Marchés | Part |
|---|---|---|
| < 1,000 $ (arbitrage) | **0** | **0 %** |
| 1,000 – 1,0015 $ (verrouillé au tick) | 724 | 37,4 % |
| 1,0015 – 1,010 $ | 434 | 22,4 % |
| > 1,010 $ (carnet lâche) | 779 | 40,2 % |

Côté vente, la meilleure somme de bid observée est **0,999** — soit exactement
un tick *sous* 1.

**Conclusion, sans détour :** l'arbitrage « YES + NO < 1 $ » n'est pas rare sur
Polymarket, il est **structurellement absent**. Les teneurs de marché tiennent la
somme des ask à un tick au-dessus de 1 et la somme des bid à un tick en dessous.
Le tick vaut 0,001 : la fourchette ne se croise jamais. Toute promesse de gain
fondée sur cet arbitrage (les « 100 $ → 1 787 $ en une nuit » vus sur les réseaux)
est fausse, et ce tableau est la preuve chiffrée.

C'est un résultat utile, pas un échec : il ferme définitivement une piste au lieu
de la laisser coûter du temps et du capital.

## Deuxième mesure : le côté teneur, sur exécutions réelles (2026-07-28)

La première mesure ne regardait que le côté **preneur** (acheter à l'ask). Restait
la question du côté **teneur** : poster des ordres à l'achat et être servi au bid.
Les carnets Up/Down affichaient une somme des bid à 0,98, soit +2 % apparents.

Le flux public `data-api.polymarket.com/trades` (sans clé, champs `side`, `price`,
`size`, `outcomeIndex`) permet de trancher sur des **exécutions constatées** plutôt
que sur des cotations affichées. Un ordre d'achat posé à `p` est servi quand un
preneur vend à `p` ou moins — et ces ventes sont dans le flux.

Sur **10 500 transactions réelles**, 852 marchés, dont 69 Up/Down :

| Mesure | Résultat |
|---|---|
| Les deux branches vendues (les deux ordres servis) | 27 marchés |
| Une seule branche vendue — **sélection adverse** | 10 marchés (27 %) |
| Somme du jeu complet, prix médian pondéré par la taille | **0,9943** (+0,57 %) |
| Marchés où cette somme est < 1,00 | **14/27** (52 %) |

Et le point décisif : les deux marchés les plus liquides de l'échantillon
(6 829 et 3 897 parts vendues) ressortent à **1,0057** et **1,0853** — perdants.
Les marchés qui affichent +50 % ont `vol=0/0`. **L'edge et la liquidité sont
anticorrélés.**

**Conclusion :** les +2 % lus dans les carnets étaient la fourchette *affichée*,
pas la fourchette *obtenue*. Sur exécutions réelles la stratégie rend +0,57 %
médian, perd dans 48 % des cas, devient négative là où il y a du volume, et
laisse 27 % de chances de n'être servi que d'un côté (pari directionnel nu).

Attention à une métrique trompeuse : additionner le *meilleur* prix servi sur
chaque branche donne 0,65 médian (+35 %), mais ces deux prints ont lieu à des
instants différents — cette somme n'est jamais réalisable simultanément.

Piste restante, non invalidée : les **récompenses de liquidité**, payées pour
poster des ordres et non pour être rempli — donc insensibles à la sélection
adverse et au taux de remplissage. Ticket d'entrée mesuré : `rewardsMinSize`
= 50 parts de chaque côté, soit environ 50 $.

## Troisième mesure : les récompenses ne sont pas de l'argent gratuit (2026-07-28)

Être payé pour poster ne protège de rien : l'ordre posté peut être **rempli**, et
on porte alors la position pendant que le prix s'éloigne. C'est le **risque
d'inventaire**, et il se mesure sur l'historique des prix à la minute.

Entonnoir du scan, chiffres réels : **2 099** marchés lus → **639** récompensés →
**571** à plus de 24 h de l'échéance → **474** finançables à 100 $ → **456** aux
deux carnets lisibles. Sur les **60 meilleurs au rendement brut**, avec 24 h
d'historique :

| Mesure | Résultat |
|---|---|
| Rendement brut médian | **+0,75 %/jour** |
| Dérive médiane sur 24 h (en % du capital engagé) | **3,00 %/jour** |
| **NET médian** (rendement − dérive) | **−1,60 %/jour** |
| Marchés où la récompense couvre son propre risque | **16 / 60** |

**Conclusion :** classer les marchés récompensés au rendement **brut** mène à une
médiane **perdante**. Le tri se fait donc au **net**, et il ne reste que 3 à 6
candidats sur 2 099 marchés.

Mais la queue haute existe et se voit : le meilleur candidat payait 150 $/jour
face à 256 $ de liquidité concurrente, soit **+42 %/jour net** sur un ticket de
50 $. Deux scans à quelques minutes d'écart l'ont vu passer de 461 $ à 256 $ de
concurrence, et son net de 22 % à 42 % : **ces pools bougent vite**. C'est ce qui
justifie de balayer en boucle plutôt que de tenir une liste figée — et c'est
aussi pourquoi aucun de ces chiffres ne doit être lu comme un rendement acquis.

Trois limites assumées, à ne pas oublier avant d'engager un dollar :

1. ~~**La dérive est un majorant.**~~ **DÉMENTI le 2026-08-01** — voir la
   septième mesure. Cette page a longtemps écrit que le net affiché était un
   *plancher* : c'était faux, et le rejeu l'a montré sur 6 des 17 marchés
   réellement cotés, jusqu'à 31,5 points/jour d'écart. Le risque retenu est
   désormais le **pire** de deux mesures, la dérive et le rejeu, parce
   qu'aucune des deux ne majore l'autre.
2. ~~**La part au prorata est linéaire dans le modèle.**~~ **CORRIGÉ le
   2026-07-31** — voir la sixième mesure. La concurrence se compte maintenant
   avec la formule publiée par Polymarket (`analysis/scoring.py`), en parts
   pondérées par la distance au milieu, et non plus en dollars postés.
3. **Le risque de saut n'est pas modélisé.** Un historique de 24 h ne dit rien
   d'un marché qui se résout sur une publication ponctuelle (un compteur de vues,
   un chiffre officiel) : le prix y saute au lieu de dériver. **Toujours vrai.**

Les deux premières sont laissées barrées plutôt que supprimées : savoir qu'une
affirmation a été crue puis démentie vaut mieux que de la voir disparaître, et
c'est la seule protection contre le fait de la recroire.

## Quatrième mesure : le chiffre du balayage est périmé quand il s'affiche (2026-07-29)

Les trois premières mesures reposaient sur des instantanés. Le flux WebSocket
permet enfin de regarder ce qui se passe *entre* deux balayages — et ce qui s'y
passe change la lecture de tout le reste.

Balayage réel, 2 099 marchés, 5 candidats retenus, puis 45 secondes d'écoute sur
les 10 jetons correspondants (1 694 mises à jour reçues, les 5 lignes
rafraîchies) :

| Moment | Liquidité concurrente | Net du meilleur candidat |
|---|---|---|
| au balayage | — | **+140,47 %/jour** |
| + 5 s | 171 $ | +29,61 %/jour |
| + 15 s | **52 $** | **+83,10 %/jour** |
| + 25 s | 122 $ | +42,89 %/jour |
| + 45 s | 76 $ | +64,59 %/jour |

**Conclusion :** le rendement affiché par un balayage est déjà faux au moment où
il s'imprime — ici surestimé d'un facteur 2 à 5. La concurrence sur un pool
oscille du simple au triple en quelques secondes.

Et une conséquence qui n'est pas encore traitée : Polymarket paie sur des
relevés **échantillonnés tout au long de la journée**. Le chiffre pertinent est
donc la **moyenne temporelle** de la liquidité concurrente, pas sa valeur à
l'instant du regard. Ni le balayage ni le flux ne la calculent aujourd'hui ; les
deux affichent un instantané. C'est la prochaine correction utile, et elle est
plus importante que n'importe quel ajout de fonctionnalité.

## Cinquième mesure : le vote d'ensemble « 28 sur 31 » ne décide jamais (2026-07-29)

Méthode vue sur les réseaux : N modèles de prédiction en parallèle, le trade ne
part que si 28 sur 31 votent dans le même sens. La technique de fond est réelle
— l'ECMWF fait tourner 51 membres perturbés — et elle est ici construite telle
quelle (`donmarket/consensus/`), puis mesurée sur 40 marchés Polymarket.

| Mesure | Résultat |
|---|---|
| Corrélation moyenne entre membres | **+0,090** (médiane) |
| **Votes réellement indépendants** | **8,3 sur 31** |
| Meilleur cas observé | 15,7 sur 31 |

La corrélation est basse, ce qui est une bonne nouvelle : des familles opposées
(élan contre retour à la moyenne) produisent bien de la diversité. Mais c'est
précisément ce qui condamne le seuil :

| Seuil | Décisions prises |
|---|---|
| 31/31 | **0,0 %** |
| 28/31 | **0,0 %** |
| 24/31 | **0,0 %** |
| 20/31 | 1,9 % |
| 12/31 | 11,6 % |
| 8/31 | 29,3 % |
| 6/31 | **81,9 %** |
| 4/31 | 97,7 % |

**Conclusion :** la méthode est prise en tenaille par sa propre logique. Si les
membres sont assez variés pour valoir quelque chose, ils ne se mettent jamais
d'accord à 28 sur 31 — le système ne trade pas, et aucune performance ne peut
en venir. S'ils s'accordent à 28 sur 31, c'est qu'ils sont des copies : le vote
mesure alors sa propre redondance et le seuil ne filtre rien tout en ayant l'air
exigeant. Entre 8/31 (33 %) et 6/31 (82 %), il n'existe aucun palier où le vote
soit à la fois sélectif et actif.

Ce n'est pas un échec de mise en œuvre, et le code est là pour être relancé avec
d'autres réglages : `python -m donmarket consensus --threshold 20 --members 31`.

## Sixième mesure : notre propre ordre déplace le milieu dont il se mesure (2026-07-31)

La concurrence était comptée en **dollars postés** dans la bande. Elle se compte
maintenant avec la formule publiée par Polymarket (`analysis/scoring.py`) :
`S(v,s) = ((v−s)/v)²·b`, en **parts**, seaux `Qone`/`Qtwo` croisés (les bids
d'une branche avec les asks de l'autre), puis `Qmin` avec une pénalité `/3` pour
la liquidité unilatérale quand le milieu est dans [0,10 ; 0,90]. Trois biais
tombaient ensemble : pas de pondération par la distance, des dollars au lieu de
parts, une somme au lieu d'un `min`.

Mais la correction en a révélé une bien pire, et **systématique** : en postant à
`m − v/2`, notre ordre **devient le meilleur bid**. Le milieu monte vers lui, et
la distance finale ne vaut pas `v/2` mais `(A−B)/2 + v/2`. **On ne marque donc
que si l'écart du carnet ne dépasse pas trois fois la bande.**

Cas réel, « LIV Golf shutdown 2026 » : bid 0,452 / ask 0,698 (écart 0,246), bande
0,045. On visait 0,5525 ; une fois posté, le milieu passait à 0,62525 et notre
distance à 0,07275 — **hors bande, score nul**. Le bot annonçait **+238 %/jour**
sur ce marché.

Le piège n'était pas occasionnel : `competing_q = 0` et « notre score est nul »
sont **le même phénomène** — un carnet béant. Les marchés invendables montaient
donc mécaniquement en **tête** du classement, précisément parce qu'ils
paraissaient déserts.

| | Avant | Après |
|---|---|---|
| Tête du classement | +238 %/jour | **+21,90 %/jour** |
| Portefeuille tenable à 100 $ | +70,81 $/jour | **+12,70 $/jour** |

Reste fragile : les parts « 100 % du pool » reposent sur un score de ~0,4 point.
Un seul teneur qui arrive les dilue à néant.

## Septième mesure : le majorant est démenti (2026-08-01)

Le rejeu de tenue de marché (`backtest/`, `replay_quotes`) cote symétriquement à
±`quote_spread(bande)`, recote chaque minute et plafonne l'inventaire. Il ne
compte **aucune récompense** — il n'y a pas de carnet passé à interroger, donc
il mesure uniquement ce que le chemin de prix aurait coûté.

Sur 60 marchés / 49 événements distincts, le majorant `−drift` — la limite n° 1
de cette page, « le net affiché est un plancher » — **cède sur 6 des 17 marchés
réellement cotés**, jusqu'à **31,5 points/jour** d'écart. Et les pires écarts
sont tous des marchés d'actualité chaude (Israël/Iran ×3) : donc à gros pool,
donc **exactement ceux que le classement remonte en tête**.

Correctif : `RewardCandidate.inventory_cost` retient le **pire** de deux mesures,
`−drift` et le rejeu, parce qu'aucune ne majore l'autre. La dérive rate les
allers-retours (0,50 → 0,53 → 0,50 : dérive nulle, coût réel −1 %) ; le rejeu
rate les tendances lentes (0,4 cent/minute ne touche jamais une cote à 1,5 cent,
coût mesuré 0 sur 23 cents parcourus).

Deux défauts de méthode corrigés au passage, tous deux silencieux :
`fetch_price_histories` perdait 56 séries sur 60 sans une ligne de journal (le
verdict était rendu sur n=4) ; et le tri par pool concentrait l'échantillon sur
un seul fait d'actualité, d'où `diversified_head` (2 marchés maximum par
événement). 43 marchés sur 60 ne sont jamais remplis — leur « réalisé 0,00 »
faisait afficher un faux 90 % de succès, d'où le découpage `BacktestReport.active`.

## Huitième mesure : le compte de démonstration se payait d'ordres imaginaires (2026-08-06)

`python -m donmarket paper` ouvre un compte fictif et confronte nos ordres aux
exécutions réelles. Son crédit de récompense recalculait à chaque tour le score
d'un ordre **frais**, reposté au prix idéal du milieu **courant**, à la taille
pleine. Autrement dit : un teneur qui annulerait et reposterait sans cesse,
gratuitement, sans jamais se faire remplir.

Quatre situations où le carnet réel ne paie rien et où ce modèle payait quand
même — les quatre du même côté, celui qui flatte :

| Situation | Carnet réel | Ancien modèle |
|---|---|---|
| Ordre entièrement servi | il a quitté le carnet | continue de payer |
| Milieu qui dérive | l'ordre sort de la bande, score nul | le recentre gratuitement |
| Remplissage partiel | moins de parts posées | compte la taille pleine |
| Achat passé au-dessus du meilleur ask | il s'exécute, il ne dort pas | le rémunère comme posté |

Le cas qui tranche : une session **sans aucun ordre** se créditait **9,09 $/jour**.

`PaperSession.resting_score` note désormais les ordres tels qu'ils dorment — à
leur prix, pour les parts qui leur restent — et applique `rewardsMinSize` comme
le seuil de qualification qu'il est. La règle est appliquée à nos ordres seuls :
le carnet public agrège des paliers et non des ordres, donc elle y est
inapplicable. Cette asymétrie nous **sous-estime**, et c'est le sens acceptable.

Cause racine : `PaperSession` — la mécanique complète du compte — n'avait
**aucun test**. Les 240 lignes de `test_paper.py` couvraient le registre et les
remplissages, jamais la session. Onze tests l'entourent maintenant.

## Pièges vérifiés sur l'API (ne pas les redécouvrir)

1. **Les carnets ne sont pas triés dans l'ordre utile.** `bids` arrive par prix
   croissant et `asks` par prix décroissant : le meilleur prix est en **dernière**
   position. Se fier à `bids[0]` donne le pire prix du carnet.
2. **Le spread se mesure branche par branche.** Comparer l'ask du « No » au bid
   du « Yes » produit des spreads de 0,96 sur des carnets pourtant serrés à 0,001.
3. **Gamma plafonne à 100 marchés par page**, quelle que soit la limite demandée,
   et renvoie **422 au-delà d'offset 2100**. Une condition d'arrêt « page
   incomplète = fin » stoppe donc la pagination dès la première page.
4. **Le CLOB sert ~50 000 marchés clos** avant d'atteindre les actifs : il est
   inutilisable comme source de marchés négociables.
5. **`orderMinSize` vaut souvent 5 parts**, pas 1. Le ticket d'entrée réel est
   `5 × prix`, ce qui exclut beaucoup de marchés à petit capital.
6. **`rewardsMaxSpread` est en pourcent, pas en dollars.** `3.0` signifie 3
   *cents*. Sans la division par 100, toute la profondeur du carnet entre dans la
   bande qualifiante et la concurrence est massivement surestimée.
7. **Un marché résolu peut rester `closed=false`** : son pool de récompenses est
   intact et sa liquidité concurrente nulle, ce qui produit des rendements
   fantômes (118 %/jour mesuré). Filtrer sur `endDate`, pas sur `closed`.
8. **L'historique des prix ne se groupe pas** : `POST /books` accepte des
   centaines de jetons par requête, `/prices-history` en prend **un seul**. D'où
   un scan en deux passes — brut sur tout l'univers, net sur la tête seulement.
9. **Dérive et rendement doivent être dans la même unité.** Le rendement est en
   % du capital engagé ; la dérive doit l'être aussi, donc rapportée au jeu
   complet (1 $), jamais au prix moyen (≈ 0,45 $) — sans quoi la soustraction
   gonfle le risque d'un facteur ~2 et le classement change.

10. **Dans un `price_change`, `size` est la NOUVELLE taille du palier**, pas une
    variation ; une taille nulle supprime le palier. Vérifié par réconciliation
    avec un instantané REST (4 carnets sur 12 identiques au palier près, les
    autres divergeant de 0,07 à 3 % — l'activité de deux secondes). Se tromper
    de convention ne lève aucune erreur : le carnet dérive lentement et tous les
    rendements calculés dessus restent crédibles.
11. **Le flux n'envoie d'instantané qu'à l'abonnement.** Appliquer des
    `price_change` à un jeton dont on n'a pas reçu le `book` construit un carnet
    partiel qui a l'air complet.

## Ce qui n'est pas fait

Ce qui figurait ici jusqu'au 2026-08-06 et qui est désormais **fait** : moyenne
temporelle de la concurrence (`watch/average.py`), moteur d'exécution
(`execute/`), persistance des scans (table `reward_candidates`), part pondérée
par la distance au milieu (`analysis/scoring.py`), backtest sur historique
(`backtest/`). Reste :

- **Le chemin armé n'a jamais tourné.** Aucun ordre n'est jamais parti. Les
  tests vérifient ce que le moteur REFUSE de faire, pas ce qu'il réussit : une
  signature EIP-712 qui passe les tests et se trompe en production se paie en
  dollars, et seul un premier ordre réel minuscule la validera. C'est un geste
  qui appartient au propriétaire du compte, pas au code.
- **La prime d'entrée sur carnet large n'est pas modélisée.** Dès que l'écart du
  carnet dépasse trois fois la bande, marquer exigerait de poster à `A − v`,
  soit parfois 20 cents au-dessus du meilleur bid. Ce surpaiement immédiat
  n'entre dans aucun calcul de risque (la dérive sur 24 h ne le capture pas).
  Tant qu'il n'y entre pas, la bonne réponse est de ne pas jouer ces marchés —
  c'est ce que le code fait, en les rejetant avec le motif « carnet trop large ».
- **Le risque de saut** (troisième limite ci-dessus), toujours pas modélisé.
- **Le mode ombre n'est pas câblé.** `execute/shadow.py` est écrit et testé mais
  aucun appelant ne l'utilise : la part de pool réellement obtenue face à de
  vrais ordres à nous reste le seul terme que rien n'a jamais mesuré.
- Pont MCP vers DON.

## Contexte légal

L'ANJ a ordonné le blocage FAI de Polymarket en France le 16/07/2026. Ce dépôt
n'inclut aucun moyen de contourner ce blocage et n'en inclura pas.
