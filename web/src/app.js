/**
 * DONmarket — trader sur Polymarket sans rien installer.
 *
 * CE QUE CETTE APPLICATION FAIT QUE LES AUTRES NE FONT PAS
 *
 * Elle REFUSE des ordres, et elle dit pourquoi. Avant d'envoyer, elle relit le
 * carnet et bloque les pieges qui ont coute de l'argent reel sur ce compte :
 *
 *   1. Carnet mort ou piege -- un ecart large sur un marche sans volume paie
 *      sur le papier et ne se remplit jamais. Mesure du 2026-08-26 sur 789
 *      carnets : la ou il y a du volume, l'ecart vaut EXACTEMENT un tick.
 *   2. Taille au minimum exact -- un remplissage partiel laisse un reliquat
 *      SOUS le minimum d'ordre, donc INVENDABLE. Vu le 2026-08-24 sur une
 *      position de 2,15 $, puis a nouveau le 2026-08-26 sur une VENTE dont il
 *      est reste 1,74 part. D'ou la regle des deux fois `orderMinSize`, des
 *      DEUX cotes.
 *   3. Vente au-dessus du meilleur ask -- hors marche, jamais remplissable.
 *
 * ET CE QU'ELLE NE BLOQUE PLUS (2026-08-27)
 *
 * L'ordre qui traverse l'ecart etait refuse lui aussi. C'etait une faute, et
 * elle etait chere : notre bareme builder est maker 0 / taker 10 bps, donc on
 * n'encaisse QUE sur un ordre preneur. Refuser tous les preneurs garantissait
 * un revenu nul quel que soit le nombre d'utilisateurs -- pas faute d'audience,
 * faute de mecanisme. Il est desormais ANNONCE et CHIFFRE (`avertir`), puis
 * exige un second geste identique. On dit le prix, on ne decide pas a la place.
 * Meme lecon que le garde-fou de chemins du meme jour : une interdiction qu'on
 * n'a pas confrontee au trafic reel coute plus que le risque qu'elle imagine.
 *
 * Elle affiche aussi la PERSISTANCE d'un verdict (« mort depuis 6 releves »).
 * Ce chiffre ne se reconstitue pas retroactivement : il faut avoir mesure jour
 * apres jour. C'est la seule chose ici qu'un concurrent ne peut pas copier.
 *
 * ATTRIBUTION. Le corps de l'ordre ne porte aucune preuve de builder :
 * l'attribution passe par quatre en-tetes SIGNES avec un secret d'API, qui ne
 * peut pas vivre dans une page web. Il vit dans un Cloudflare Worker
 * (`signer/worker.js`) que le SDK appelle via `remoteBuilderSigning`. Le Worker
 * ne voit jamais la cle privee de l'utilisateur ; l'utilisateur ne voit jamais
 * notre secret.
 */

// PIEGE DE SURFACE, mesure le 2026-08-26 : toutes les operations ne sont pas
// des METHODES du client. `fetchBalanceAllowance` est une fonction AUTONOME qui
// prend le client en premier argument, alors que `listPositions`,
// `listOpenOrders`, `cancelOrder` et `placeLimitOrder` sont bien des methodes.
// Supposer l'uniformite rend « ... is not a function » a l'execution.
// Et elles vivent dans une AUTRE entree du paquet : `@polymarket/client/actions`,
// pas la racine. Importer depuis la racine rend « does not provide an export
// named ... » a l'INSTANCIATION du module, pas a l'appel.
import { createSecureClient, remoteBuilderSigning } from '@polymarket/client';
import { fetchBalanceAllowance } from '@polymarket/client/actions';
import { signerFrom } from '@polymarket/client/viem';
import { createWalletClient, custom } from 'viem';
import { polygon } from 'viem/chains';

const CONFIG_URL = './app-config.json';
const SANTE_URL = './health.json';
const CLOB = 'https://clob.polymarket.com';
const GAMMA = 'https://gamma-api.polymarket.com/markets';

/** Deux fois le minimum d'ordre : une execution a 50 % laisse de quoi sortir. */
export const MULTIPLE_MINIMUM = 2;

/** Verdicts sur lesquels on refuse d'engager de l'argent. */
const VERDICTS_REFUSES = new Set(['mort', 'piege']);

/** Jeu ferme de verdicts. Tout le reste est traite comme « inconnu ». */
const VERDICTS_CONNUS = new Set(['tradable', 'efficient', 'desequilibre', 'lent', 'piege', 'mort']);

/** Rafraichissement du carnet affiche, en millisecondes. */
const PERIODE_CARNET = 15000;

const etat = {
  config: null,
  sante: new Map(),
  lignes: [],
  client: null,
  builder: null,
  adresse: null,
  marche: null,
  minuteur: null,
  // Signature du dernier ordre preneur annonce mais pas encore confirme.
  confirme: null,
};

const $ = (id) => document.getElementById(id);
const fmt = (n, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : '—');

/**
 * Fabrique une cellule dont le contenu est du TEXTE, jamais du balisage.
 *
 * FAILLE TROUVEE ET FERMEE LE 2026-08-27. Les lignes de tableau etaient
 * construites par `tr.innerHTML = \`<td>${p.title}</td>\`` : le titre d'un
 * marche, l'issue d'une position et le verdict partaient BRUTS depuis l'API
 * dans du balisage. Le verdict atterrissait meme dans un ATTRIBUT `class`.
 *
 * Sur une page connectee a un portefeuille, ce n'est pas un defaut cosmetique.
 * Une charge utile dans un titre de marche pouvait reecrire le formulaire
 * d'ordre AVANT l'envoi -- echanger le `tokenId` par exemple -- et l'utilisateur
 * aurait signe dans son portefeuille un ordre qu'il ne voulait pas, en croyant
 * valider le sien. La signature est authentique ; c'est son CONTENU qui aurait
 * ete change.
 *
 * On ne corrige pas en echappant les chaines : on cesse d'utiliser `innerHTML`
 * pour des donnees. `textContent` ne peut pas produire de balisage, quelle que
 * soit l'entree. C'est une garantie du navigateur, pas une precaution de notre
 * part qu'un futur oubli pourrait defaire.
 */
function cellule(texte, classe) {
  const td = document.createElement('td');
  if (classe) td.className = classe;
  td.textContent = texte == null ? '' : String(texte);
  return td;
}

/** Ligne « rien a afficher », sans donnee interpolee. */
function ligneVide(corps, colonnes, message) {
  const tr = document.createElement('tr');
  const td = cellule(message, 'vide');
  td.colSpan = colonnes;
  tr.append(td);
  corps.replaceChildren(tr);
}

/**
 * Deroule la chaine des causes d'une erreur.
 *
 * Le SDK enveloppe : `SigningError.fromError(e, 'Could not authorize the
 * builder-authenticated request')` cache la vraie cause dans `.cause`. Afficher
 * seulement le message de surface, c'est afficher « ca a echoue » — ce qui a
 * fait perdre une heure le 2026-08-26 sur une liste blanche trop etroite.
 */
export function causes(erreur, profondeur = 6) {
  const vues = [];
  let e = erreur;
  while (e && vues.length < profondeur) {
    const m = e.message || String(e);
    if (m && !vues.includes(m)) vues.push(m);
    e = e.cause;
  }
  return vues.join(' ← ');
}

function dire(message, genre = 'info') {
  const zone = $('journal');
  if (!zone) return;
  const ligne = document.createElement('div');
  ligne.className = `ligne ${genre}`;
  ligne.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
  zone.prepend(ligne);
}

/* =============================================================== donnees == */

export async function carnet(tokenId, chercher = fetch) {
  const r = await chercher(`${CLOB}/book?token_id=${encodeURIComponent(tokenId)}`);
  if (!r.ok) throw new Error(`carnet illisible (${r.status})`);
  const b = await r.json();
  const bids = b.bids || [];
  const asks = b.asks || [];
  // LES CARNETS ARRIVENT PIRE PRIX EN PREMIER : le meilleur est en DERNIERE
  // position. Lire [0] donnerait systematiquement le pire prix.
  return {
    bid: bids.length ? Number(bids[bids.length - 1].price) : null,
    bidTaille: bids.length ? Number(bids[bids.length - 1].size) : 0,
    ask: asks.length ? Number(asks[asks.length - 1].price) : null,
    askTaille: asks.length ? Number(asks[asks.length - 1].size) : 0,
  };
}

async function marcheParSlug(slug) {
  const r = await fetch(`${GAMMA}?slug=${encodeURIComponent(slug)}`);
  const j = await r.json();
  if (!j || !j.length) throw new Error('marche introuvable');
  const m = j[0];
  return {
    slug,
    question: m.question,
    tokens: JSON.parse(m.clobTokenIds || '[]'),
    issues: JSON.parse(m.outcomes || '[]'),
    tick: Number(m.orderPriceMinTickSize || 0.01),
    minimum: Number(m.orderMinSize || 5),
    fin: m.endDate,
  };
}

/** Un `Paginator` du SDK est un iterateur ASYNCHRONE de PAGES, pas de lignes. */
async function toutes(paginator) {
  const out = [];
  for await (const page of paginator) {
    const lignes = Array.isArray(page) ? page : page?.items || [page];
    out.push(...lignes);
  }
  return out;
}

/* ============================================================ garde-fous == */

/**
 * Rend la liste des refus pour un ordre. Une liste VIDE veut dire « rien ne
 * s'y oppose », pas « c'est une bonne idee » : ces controles disent que
 * l'ordre peut s'executer, pas qu'il est rentable.
 */
/**
 * Notre bareme builder, MESURE sur `get_builder_fee_rates` le 2026-08-26 :
 * maker 0, taker 0,001 (10 bps), preleve EN PLUS et paye PAR LE TRADER.
 *
 * Consequence qu'on a mis dix jours a voir : on n'est paye QUE lorsqu'un ordre
 * traverse l'ecart. Tant que l'app refusait tout ordre preneur, son revenu
 * etait nul PAR CONSTRUCTION -- pas faute d'utilisateurs, faute de mecanisme.
 * C'est la meme lecon que le garde-fou de chemins du 27/08, un cran plus haut :
 * une interdiction qu'on n'a pas confrontee au trafic reel coute plus cher que
 * le risque qu'elle imagine.
 */
export const TAUX_PRENEUR = 0.001;

/** Cet ordre traverse-t-il l'ecart ? Sans carnet lisible, on ne l'affirme pas. */
export function estPreneur({ cote, prix, carnet: c }) {
  if (!Number.isFinite(prix)) return false;
  if (cote === 'BUY') return c.ask !== null && prix >= c.ask;
  if (cote === 'SELL') return c.bid !== null && prix <= c.bid;
  return false;
}

/** Ce que le passage en preneur coute au trader, en dollars. */
export function coutPreneur(parts, prix, taux = TAUX_PRENEUR) {
  return parts * prix * taux;
}

/**
 * Ce qui identifie un ordre pour la confirmation : si l'utilisateur change quoi
 * que ce soit entre les deux clics, la confirmation tombe. Deux clics identiques
 * plutot qu'une modale : une boite de dialogue bloque la page, et ne se teste pas.
 */
export function signatureOrdre({ cote, prix, parts, tokenId }) {
  return `${cote}|${prix}|${parts}|${tokenId}`;
}

/**
 * Ce qui merite d'etre DIT sans etre interdit : l'utilisateur decide, mais il
 * decide en connaissant le prix exact de son geste.
 */
export function avertir({ cote, prix, parts, carnet: c }) {
  const avis = [];
  if (!estPreneur({ cote, prix, carnet: c })) return avis;
  const reference = cote === 'BUY' ? c.ask : c.bid;
  const cout = coutPreneur(parts, prix);
  avis.push(
    `${cote === 'BUY' ? 'Buy' : 'Sell'} at ${prix}: this order crosses the spread ` +
      `(${cote === 'BUY' ? 'ask' : 'bid'} ${reference}) and fills immediately. ` +
      `You pay the taker fee: $${cout.toFixed(4)} ` +
      `on $${(parts * prix).toFixed(2)} committed.`,
  );
  return avis;
}

export function verifier({ cote, prix, parts, carnet: c, marche: m, verdict }) {
  const refus = [];

  if (verdict && VERDICTS_REFUSES.has(verdict.verdict)) {
    const depuis = verdict.persistance > 1 ? ` for ${verdict.persistance} runs` : '';
    refus.push(`Book "${verdict.verdict}"${depuis} : ${verdict.phrase}`);
  }
  if (!Number.isFinite(prix) || prix <= 0 || prix >= 1) {
    refus.push('The price must sit strictly between 0 and 1.');
  } else {
    const reste = Math.abs(prix / m.tick - Math.round(prix / m.tick));
    if (reste > 1e-6) refus.push(`The price must be a multiple of the tick (${m.tick}).`);
  }
  if (!Number.isFinite(parts) || parts <= 0) {
    refus.push('The size must be a positive number.');
  } else if (parts < MULTIPLE_MINIMUM * m.minimum) {
    refus.push(
      `Commit at least ${MULTIPLE_MINIMUM * m.minimum} shares ` +
        `(2 x the ${m.minimum} minimum): otherwise a 50 % fill ` +
        `strands a remainder below the minimum, which cannot be sold.`,
    );
  }
  if (cote === 'SELL' && c.ask !== null && prix > c.ask) {
    refus.push(
      `Sell at ${prix} above the best ask (${c.ask}): out of market, ` +
        `it cannot fill.`,
    );
  }
  return refus;
}

/**
 * Que peut-on vendre d'une position, sans creer de reliquat invendable ?
 *
 * LE PIEGE, PAYE DEUX FOIS. Vendre TOUT semble prudent, mais un remplissage
 * partiel laisse ce qui reste : le 2026-08-26, une vente de 25 parts remplie a
 * 23,3 a laisse 1,74 part sous un minimum de 5, donc definitivement bloquee.
 * On ne peut donc proposer que deux tailles sures : la position entiere si elle
 * peut se solder, ou une taille qui laisse au moins `minimum` derriere elle.
 */
/**
 * Une adresse EVM valide, ou `null`.
 *
 * Ce champ decide QUEL PORTEFEUILLE le client considere comme detenteur des
 * fonds. Une saisie fautive n'est pas benigne : elle fait accepter l'ordre puis
 * le rejeter pour solde insuffisant en pointant une adresse vide -- le piege du
 * 2026-08-18, qui avait casse cinq routes. On refuse donc AVANT de construire
 * le client, avec un message qui dit quoi corriger.
 */
export function adresseValide(saisie) {
  const t = String(saisie ?? '').trim();
  if (!t) return null;
  return /^0x[0-9a-fA-F]{40}$/.test(t) ? t : false;
}

export function vendable(detenu, minimum) {
  if (!Number.isFinite(detenu) || detenu <= 0) {
    return { max: 0, refus: 'aucune part detenue' };
  }
  if (detenu < minimum) {
    return {
      max: 0,
      refus:
        `${detenu.toFixed(4)} parts detenues, minimum d'ordre ${minimum} : ` +
        `INVENDABLE tel quel. Seule issue, completer la position jusqu'a ${minimum}.`,
    };
  }
  // Vendre tout est sur SI un remplissage partiel ne peut pas laisser moins que
  // le minimum... ce qu'on ne maitrise pas. On l'autorise, en l'annoncant.
  return {
    max: detenu,
    refus: null,
    avertissement:
      detenu < 2 * minimum
        ? `Un remplissage partiel laisserait moins de ${minimum} parts, donc invendables.`
        : null,
  };
}

/* ========================================================== portefeuille == */

/**
 * Memoire de session des identifiants CLOB.
 *
 * POURQUOI SESSIONSTORAGE ET PAS LOCALSTORAGE. Recharger la page redemandait
 * une signature : la friction se remarque dans les trente premieres secondes.
 * Conserver les identifiants la supprime, mais ce sont des identifiants -- s'ils
 * fuyaient, ils permettraient de POSER ET D'ANNULER des ordres sur le compte.
 * Pas d'en sortir les fonds : un transfert exige une signature du portefeuille.
 *
 * `sessionStorage` referme la fenetre d'exposition a la fermeture de l'onglet,
 * la ou `localStorage` la laisserait ouverte indefiniment. On vient de fermer
 * une XSS sur cette page ; garder la surface petite est le bon reflexe.
 *
 * Chaque acces est garde : navigation privee, stockage bloque par le navigateur
 * ou quota plein levent, et une page qui plante au demarrage pour un confort
 * serait un mauvais echange.
 */
const CLE_SESSION = 'donmarket:clob:';

function lireSession(adresse) {
  try {
    const brut = sessionStorage.getItem(CLE_SESSION + adresse.toLowerCase());
    return brut ? JSON.parse(brut) : null;
  } catch {
    return null;
  }
}

function ecrireSession(adresse, credentials) {
  try {
    sessionStorage.setItem(CLE_SESSION + adresse.toLowerCase(), JSON.stringify(credentials));
  } catch {
    // Sans stockage on resigne au prochain chargement : c'est degrade, pas casse.
  }
}

/**
 * Distingue une PANNE RESEAU d'un refus.
 *
 * Le 2026-08-27, trois tentatives de connexion ont echoue sur
 * « Request timed out » vers le relayer et le CLOB. Mesure faite dans la
 * minute : ces memes points d'acces repondaient en 0,4 a 2,4 s. C'etait donc
 * un pic de latence, pas un rejet -- mais le journal affichait « Connexion
 * refusee », ce qui fait chercher au mauvais endroit.
 *
 * La distinction compte pour l'utilisateur : un refus demande de corriger
 * quelque chose, un delai depasse demande seulement de recommencer.
 */
export function estPanneReseau(message) {
  return /timed out|timeout|network|fetch failed|failed to fetch|ECONN|ETIMEDOUT/i.test(
    String(message || ''),
  );
}

async function connecter() {
  if (!window.ethereum) {
    dire('No wallet detected. Install MetaMask or an equivalent.', 'erreur');
    return;
  }
  if (!etat.config) {
    dire("Pas de app-config.json : la pose d'ordre est desactivee.", 'erreur');
    return;
  }
  try {
    // DEUX CLIENTS, ET C'EST OBLIGATOIRE. `createWalletClient` sans `account`
    // rend un client « sans compte » : `requestAddresses()` donne bien
    // l'adresse mais ne l'attache pas, et `signerFrom()` refuse alors avec
    // « Wallet client with account is required ». Il faut donc demander
    // l'adresse avec un premier client, puis en construire un second qui la
    // porte.
    const demandeur = createWalletClient({ chain: polygon, transport: custom(window.ethereum) });
    const [adresse] = await demandeur.requestAddresses();
    const walletClient = createWalletClient({
      account: adresse,
      chain: polygon,
      transport: custom(window.ethereum),
    });
    etat.adresse = adresse;

    // `wallet` designe le PORTEFEUILLE QUI DETIENT LES FONDS, pas celui qui
    // signe. Sur Polymarket les depots vivent le plus souvent dans un proxy ;
    // confondre les deux fait accepter l'ordre puis le rejeter pour solde
    // insuffisant, en pointant une adresse vide. Laisse vide pour que le SDK
    // derive le Deposit Wallet standard.
    const proxy = adresseValide($('proxy')?.value);
    if (proxy === false) {
      dire(
        "L'adresse saisie n'est pas une adresse EVM valide (0x suivi de 40 " +
          'caracteres hexadecimaux). Laisse le champ vide pour que le portefeuille ' +
          'de depot soit derive automatiquement.',
        'erreur',
      );
      return;
    }
    // L'adresse est annoncee AVANT la signature. Le 2026-08-27, un autre
    // portefeuille a ete connecte sans qu'on le remarque : chaque adresse ouvre
    // un compte Polymarket DIFFERENT, avec son propre solde. Le dire avant
    // evite de chercher un bug la ou il y a un changement de compte.
    $('adresse').textContent = `${adresse.slice(0, 6)}…${adresse.slice(-4)}`;
    const memorises = lireSession(adresse);
    dire(
      memorises
        ? 'Identifiants CLOB repris de cette session, aucune signature requise…'
        : 'Signature demandee pour deriver les identifiants CLOB…',
    );

    // DEUX CLIENTS, ET C'EST LE COEUR DE L'AFFAIRE.
    //
    // `apiKey: remoteBuilderSigning(...)` ne change pas seulement la signature
    // des ORDRES : il fait partir TOUTE requete authentifiee avec les en-tetes
    // builder. Le CLOB scope alors `/data/orders` sur LE BUILDER, qui n'a aucun
    // ordre ouvert -- et rend une liste vide, sans erreur. Mesure du
    // 2026-08-26 : le CLI voyait quatre ordres, la page zero, sur le meme
    // compte et le meme point d'acces.
    //
    // `listPositions` et `/balance-allowance` marchaient malgre tout parce
    // qu'ils sont scopes par ADRESSE de portefeuille, pas par identite
    // authentifiee. C'est ce qui rendait le defaut si trompeur.
    //
    // On separe donc les roles : le client UTILISATEUR lit le compte avec les
    // identifiants derives de sa propre cle ; le client BUILDER ne sert qu'a
    // poser les ordres, ou l'attribution est justement ce qu'on veut.
    const commun = {
      signer: signerFrom(walletClient),
      ...(proxy ? { wallet: proxy } : {}),
    };
    etat.client = await createSecureClient(
      memorises ? { ...commun, credentials: memorises } : commun,
    );
    ecrireSession(adresse, etat.client.credentials);

    // UNE SEULE SIGNATURE, PAS DEUX. Deriver deux fois demandait deux fois la
    // signature de l'utilisateur pour le meme compte -- une friction qu'on
    // remarque dans les trente premieres secondes. Les identifiants CLOB sont
    // deterministes pour un signataire donne : le client builder reprend donc
    // ceux du premier, et ne differe que par l'autorisation appliquee aux
    // requetes. Rien n'est stocke, rien n'est affaibli ; on cesse simplement de
    // redemander ce qu'on a deja.
    etat.builder = await createSecureClient({
      ...commun,
      credentials: etat.client.credentials,
      apiKey: remoteBuilderSigning({
        url: etat.config.signeur,
        ...(etat.config.jeton ? { headers: { Authorization: `Bearer ${etat.config.jeton}` } } : {}),
      }),
    });
    $('adresse').textContent = `${adresse.slice(0, 6)}…${adresse.slice(-4)}`;
    $('connecter').disabled = true;
    dire(`Connected: ${adresse}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    const detail = causes(e);
    if (estPanneReseau(detail)) {
      dire(
        `Polymarket n'a pas repondu a temps — ce n'est pas un refus. Reessaie. (${detail})`,
        'erreur',
      );
    } else {
      dire(`Connection refused: ${detail}`, 'erreur');
    }
  }
}

async function rafraichirPortefeuille() {
  if (!etat.client) return;
  try {
    const solde = await fetchBalanceAllowance(etat.client, { assetType: 'COLLATERAL' });
    // Le solde arrive en unites de base a SIX decimales, comme l'USDC. L'afficher
    // brut donnerait « 10468585 $ » et ferait croire a une fortune.
    $('solde').textContent = `${fmt(Number(solde.balance) / 1e6)} $`;
  } catch (e) {
    dire(`Balance unreadable: ${causes(e)}`, 'erreur');
  }
  try {
    const positions = (await toutes(etat.client.listPositions({}))).filter(
      (p) => Number(p.size) > 0,
    );
    rendrePositions(positions);
  } catch (e) {
    dire(`Positions unreadable: ${causes(e)}`, 'erreur');
  }
  try {
    rendreOrdres(await toutes(etat.client.listOpenOrders()));
  } catch (e) {
    dire(`Orders unreadable: ${causes(e)}`, 'erreur');
  }
}

export function rendrePositions(positions) {
  const corps = $('positions');
  corps.replaceChildren();
  if (!positions.length) {
    ligneVide(corps, 5, 'aucune position');
    return;
  }
  for (const p of positions) {
    const taille = Number(p.size);
    const pnl = Number(p.cashPnl ?? p.cash_pnl ?? 0);
    const tr = document.createElement('tr');
    const titre = cellule(String(p.title || '').slice(0, 46), 'l');
    const issue = document.createElement('span');
    issue.className = 'note';
    issue.textContent = p.outcome || '';
    titre.append(document.createElement('br'), issue);
    tr.append(
      titre,
      cellule(fmt(taille, 2), 'num'),
      cellule(fmt(Number(p.avgPrice ?? p.avg_price), 3), 'num'),
      cellule(`${fmt(pnl)} $`, `num ${pnl >= 0 ? 'gain' : 'perte'}`),
    );
    const celluleAction = document.createElement('td');
    const bouton = document.createElement('button');
    bouton.className = 'mini';
    bouton.textContent = 'Vendre';
    bouton.addEventListener('click', () => preparerVente(p));
    celluleAction.append(bouton);
    tr.append(celluleAction);
    corps.append(tr);
  }
}

export function rendreOrdres(ordres) {
  const corps = $('ordres');
  corps.replaceChildren();
  if (!ordres.length) {
    ligneVide(corps, 5, 'aucun ordre au carnet');
    return;
  }
  for (const o of ordres) {
    const total = Number(o.originalSize ?? o.original_size);
    const rempli = Number(o.sizeMatched ?? o.size_matched ?? 0);
    const tr = document.createElement('tr');
    tr.append(
      cellule(o.side, 'l'),
      cellule(fmt(total, 2), 'num'),
      cellule(fmt(Number(o.price), 3), 'num'),
      cellule(`${fmt(rempli, 2)} / ${fmt(total, 2)}`, 'num'),
    );
    const celluleAction = document.createElement('td');
    const bouton = document.createElement('button');
    bouton.className = 'mini danger';
    bouton.textContent = 'Annuler';
    bouton.addEventListener('click', () => annuler(o, rempli, total));
    celluleAction.append(bouton);
    tr.append(celluleAction);
    corps.append(tr);
  }
}

async function annuler(ordre, rempli, total) {
  // ANNULER UNE VENTE PARTIELLEMENT REMPLIE PEUT PIEGER LE RELIQUAT : si ce qui
  // reste est sous le minimum d'ordre, on ne pourra jamais reposer de vente.
  if (ordre.side === 'SELL' && rempli > 0 && total - rempli < 5) {
    const reste = total - rempli;
    if (
      !window.confirm(
        `Cette vente est remplie a ${rempli.toFixed(2)} sur ${total}. ` +
          `L'annuler laisse ${reste.toFixed(2)} parts, probablement sous le minimum ` +
          `d'ordre : elles deviendraient invendables. Annuler quand meme ?`,
      )
    ) {
      dire('Cancellation abandoned — the remainder would be unsellable.', 'ok');
      return;
    }
  }
  try {
    await etat.client.cancelOrder({ orderId: ordre.id });
    dire(`Order cancelled: ${ordre.side} ${total} @ ${ordre.price}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    dire(`Cancellation refused: ${causes(e)}`, 'erreur');
  }
}

/**
 * Composition de l'univers mesure, en une bande et sa legende.
 *
 * TROIS categories, pas quatre. Une premiere version separait
 * « desequilibre » de « mort » : le validateur de palette a refuse la paire
 * rouge/jaune sur le plancher de VISION NORMALE (ΔE 13,0 < 15) -- deux couleurs
 * qu'un oeil sans deficience confond deja. Plutot que forcer une teinte, on a
 * fusionne : pour qui trade, « desequilibre », « piege » et « mort » disent la
 * meme chose, a savoir n'y va pas.
 *
 * L'avertissement daltonien restant (ΔE 6,5 protan) n'est admis QU'AVEC un
 * encodage secondaire. D'ou la legende, qui nomme et chiffre chaque segment :
 * la couleur ne porte jamais l'information seule.
 */
const COMPOSITION = [
  { cle: 'vivant', nom: 'cotables', couleur: 'var(--vivant)', verdicts: ['tradable'] },
  { cle: 'serre', nom: 'serres', couleur: 'var(--serre)', verdicts: ['efficient'] },
  {
    cle: 'eviter',
    nom: 'a eviter',
    couleur: 'var(--eviter)',
    verdicts: ['mort', 'piege', 'desequilibre', 'lent'],
  },
];

export function composition(lignes) {
  const total = lignes.length || 1;
  return COMPOSITION.map((c) => {
    const n = lignes.filter((l) => c.verdicts.includes(l.verdict)).length;
    return { ...c, n, part: n / total };
  });
}

export function rendreComposition(lignes) {
  const parts = composition(lignes);
  const bande = $('bande');
  const legende = $('legende');
  if (!bande || !legende) return;

  bande.replaceChildren(
    ...parts
      .filter((p) => p.n > 0)
      .map((p) => {
        const i = document.createElement('i');
        i.style.flex = `${p.part}`;
        i.style.background = p.couleur;
        return i;
      }),
  );

  legende.replaceChildren(
    ...parts.map((p) => {
      const li = document.createElement('li');
      const puce = document.createElement('span');
      puce.className = 'puce';
      puce.style.background = p.couleur;
      const compte = document.createElement('b');
      compte.className = 'n';
      compte.textContent = String(p.n);
      const nom = document.createElement('span');
      nom.textContent = `${p.nom} · ${Math.round(100 * p.part)} %`;
      li.append(puce, compte, nom);
      return li;
    }),
  );
}

/* =============================================================== marches == */

function rendreListe(filtre = '') {
  const corps = $('marches');
  corps.replaceChildren();
  const f = filtre.trim().toLowerCase();
  const choix = etat.lignes
    .filter((l) => l.verdict === 'tradable' || l.verdict === 'efficient')
    .filter((l) => !f || l.question.toLowerCase().includes(f) || l.slug.includes(f))
    .slice(0, 80);
  if (!choix.length) {
    ligneVide(corps, 5, 'aucun marche');
    return;
  }
  for (const l of choix) {
    const tr = document.createElement('tr');
    const pastille = document.createElement('span');
    // Le verdict vient de nos donnees mais atterrissait dans un ATTRIBUT class.
    // On le contraint a un jeu ferme : une valeur inattendue devient « inconnu »
    // au lieu de s'ecrire telle quelle dans le balisage.
    pastille.className = `pastille ${VERDICTS_CONNUS.has(l.verdict) ? l.verdict : 'inconnu'}`;
    pastille.textContent = l.verdict;
    const cVerdict = document.createElement('td');
    cVerdict.append(pastille);
    tr.append(
      cellule(String(l.question).slice(0, 52), 'l'),
      cellule(l.volume24h.toLocaleString('fr-FR', { maximumFractionDigits: 0 }), 'num'),
      cellule(`${fmt(l.bid, 3)} / ${fmt(l.ask, 3)}`, 'num'),
      cellule(l.persistance ?? 1, 'num'),
      cVerdict,
    );
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      $('slug').value = l.slug;
      rafraichirCarnet();
      $('ticket').scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    corps.append(tr);
  }
}

async function rafraichirCarnet() {
  const slug = $('slug').value.trim();
  if (!slug) return;
  try {
    const m = await marcheParSlug(slug);
    const issue = Number($('issue').value);
    const c = await carnet(m.tokens[issue]);
    etat.marche = { ...m, issue, carnet: c };
    const v = etat.sante.get(slug);
    $('carnet').textContent =
      `${m.question}\n` +
      `issue « ${m.issues[issue]} »   tick ${m.tick}   minimum ${m.minimum}   ` +
      `echeance ${String(m.fin).slice(0, 10)}\n` +
      `bid ${c.bid} (${c.bidTaille})   ask ${c.ask} (${c.askTaille})   ` +
      `ecart ${c.bid && c.ask ? fmt(100 * ((c.ask - c.bid) / c.bid), 1) : '—'} %\n` +
      (v
        ? `verdict : ${v.verdict} depuis ${v.persistance ?? 1} releve(s) — ${v.phrase}`
        : 'verdict : ce marche n a pas ete mesure');
    $('verdict').className = `pastille ${v ? v.verdict : 'inconnu'}`;
    $('verdict').textContent = v ? v.verdict : 'non mesure';
    if (!$('prix').value && c.bid) $('prix').value = (c.bid + m.tick).toFixed(4).replace(/0+$/, '');
    if (!$('parts').value) $('parts').value = String(MULTIPLE_MINIMUM * m.minimum);
    clearInterval(etat.minuteur);
    etat.minuteur = setInterval(rafraichirCarnetSilencieux, PERIODE_CARNET);
  } catch (e) {
    dire(`Could not read the book: ${e.message || e}`, 'erreur');
  }
}

async function rafraichirCarnetSilencieux() {
  if (!etat.marche) return;
  try {
    const c = await carnet(etat.marche.tokens[etat.marche.issue]);
    etat.marche.carnet = c;
    $('vif').textContent = `bid ${c.bid} / ask ${c.ask} · ${new Date().toLocaleTimeString()}`;
  } catch {
    // Un rafraichissement rate ne doit pas polluer le journal : il reessaiera.
  }
}

async function preparerVente(position) {
  const minimum = 5;
  const detenu = Number(position.size);
  const v = vendable(detenu, minimum);
  if (v.refus) {
    dire(`Cannot sell — ${v.refus}`, 'erreur');
    return;
  }
  if (v.avertissement) dire(`Warning — ${v.avertissement}`, 'erreur');
  $('slug').value = position.slug || '';
  $('cote').value = 'SELL';
  $('parts').value = String(v.max);
  await rafraichirCarnet();
  const c = etat.marche?.carnet;
  if (c?.ask) $('prix').value = String(Number((c.ask - etat.marche.tick).toFixed(4)));
  $('ticket').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

/* ================================================================= ordre == */

async function poser() {
  if (!etat.marche) return dire('Load a market first.', 'erreur');
  if (!etat.client) return dire('Connect a wallet.', 'erreur');

  const cote = $('cote').value;
  const prix = Number($('prix').value);
  const parts = Number($('parts').value);
  const m = etat.marche;

  // On RELIT le carnet juste avant : entre l'affichage et le clic il a pu
  // bouger. Le 2026-08-26, un ecart de 0,43/0,58 s'est referme a un tick en
  // dix minutes, et celui des Blue Jays est passe de 5 ticks a 1 en trois heures.
  let c;
  try {
    c = await carnet(m.tokens[m.issue]);
  } catch (e) {
    return dire(`Book unreadable, nothing sent: ${e.message || e}`, 'erreur');
  }

  const contexte = { cote, prix, parts, carnet: c, marche: m, verdict: etat.sante.get(m.slug) };
  const refus = verifier(contexte);
  if (refus.length) {
    for (const r of refus) dire(`REFUSED — ${r}`, 'erreur');
    etat.confirme = null;
    return;
  }

  // Un ordre qui traverse l'ecart n'est plus INTERDIT, il est ANNONCE : on dit
  // ce qu'il coute, et on demande le meme geste une seconde fois.
  const preneur = estPreneur({ cote, prix, carnet: c });
  const avis = avertir(contexte);
  const signature = signatureOrdre({ cote, prix, parts, tokenId: m.tokens[m.issue] });
  if (avis.length && etat.confirme !== signature) {
    for (const a of avis) dire(`WARNING — ${a}`, 'erreur');
    dire('Send exactly the same order again to confirm.', 'erreur');
    etat.confirme = signature;
    return;
  }
  etat.confirme = null;

  dire(`Sending: ${cote} ${parts} @ ${prix} (${fmt(parts * prix)} $)…`);
  try {
    const reponse = await (etat.builder || etat.client).placeLimitOrder({
      tokenId: m.tokens[m.issue],
      price: prix,
      size: parts,
      side: cote,
      ...(etat.config.builderCode ? { builderCode: etat.config.builderCode } : {}),
      // Teneur : `postOnly` protege d'un franchissement accidentel du carnet.
      // Preneur : le CLOB refuserait l'ordre, or c'est le seul type d'ordre qui
      // nous rapporte quoi que ce soit. Le franchissement est ici DELIBERE,
      // annonce et confirme -- il n'a plus a etre empeche.
      postOnly: !preneur,
    });
    dire(`Accepted — ${reponse.orderId || reponse.status || 'ok'}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    dire(`Refused by the CLOB: ${causes(e)}`, 'erreur');
  }
}

/* ============================================================= demarrage == */

async function chargerConfig() {
  try {
    const r = await fetch(CONFIG_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    return await r.json();
  } catch {
    return null;
  }
}

async function demarrer() {
  etat.config = await chargerConfig();
  if (!etat.config) dire('app-config.json missing: read-only mode.', 'erreur');
  try {
    const r = await fetch(SANTE_URL, { cache: 'no-store' });
    etat.lignes = await r.json();
    for (const l of etat.lignes) etat.sante.set(l.slug, l);
    // La date vient du RELEVE, pas de l'horloge du visiteur. Afficher la date
    // du jour donnerait au lecteur du 27 aout des chiffres du 26 presentes
    // comme frais -- sur une page dont la credibilite est la mesure, ce
    // decalage d'un jour suffit a la detruire.
    let quand = 'date inconnue';
    try {
      const meta = await (await fetch('./health-meta.json', { cache: 'no-store' })).json();
      quand = new Date(meta.mesure).toLocaleString('fr-FR', {
        day: 'numeric',
        month: 'long',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      // Sans le fichier de metadonnees, on ne DEVINE pas : on le dit.
    }
    $('mesure').textContent =
      `${etat.lignes.length} carnets sondes un a un sur le carnet reel — releve du ${quand}.`;
    rendreComposition(etat.lignes);
    rendreListe();
  } catch (e) {
    dire(`Verdicts unavailable: ${e.message || e}`, 'erreur');
  }
  $('connecter').addEventListener('click', connecter);

  // REPRISE SILENCIEUSE. `getAddresses()` interroge les comptes DEJA autorises
  // sans rien demander -- contrairement a `requestAddresses()`, qui ouvre le
  // portefeuille. Si le compte est deja autorise ET que cette session a garde
  // ses identifiants, on se rebranche sans un seul clic ni une seule signature.
  // Sinon on ne fait rien : surprendre l'utilisateur avec une fenetre de
  // portefeuille au chargement serait pire que le rechargement qu'on corrige.
  try {
    if (window.ethereum) {
      const [deja] = await createWalletClient({
        chain: polygon,
        transport: custom(window.ethereum),
      }).getAddresses();
      if (deja && lireSession(deja)) await connecter();
    }
  } catch {
    // Un portefeuille absent ou muet laisse simplement le bouton disponible.
  }
  $('charger').addEventListener('click', rafraichirCarnet);
  $('poser').addEventListener('click', poser);
  $('rafraichir').addEventListener('click', rafraichirPortefeuille);
  $('filtre').addEventListener('input', (e) => rendreListe(e.target.value));
  $('issue').addEventListener('change', rafraichirCarnet);
}

if (typeof document !== 'undefined') demarrer();
