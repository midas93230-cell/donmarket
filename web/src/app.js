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
 *   4. Ordre qui traverse l'ecart -- on devient preneur et on paie les frais
 *      qu'un teneur evite.
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
export function verifier({ cote, prix, parts, carnet: c, marche: m, verdict }) {
  const refus = [];

  if (verdict && VERDICTS_REFUSES.has(verdict.verdict)) {
    const depuis = verdict.persistance > 1 ? ` depuis ${verdict.persistance} releves` : '';
    refus.push(`Carnet « ${verdict.verdict} »${depuis} : ${verdict.phrase}`);
  }
  if (!Number.isFinite(prix) || prix <= 0 || prix >= 1) {
    refus.push('Le prix doit tenir strictement entre 0 et 1.');
  } else {
    const reste = Math.abs(prix / m.tick - Math.round(prix / m.tick));
    if (reste > 1e-6) refus.push(`Le prix doit etre un multiple du tick (${m.tick}).`);
  }
  if (!Number.isFinite(parts) || parts <= 0) {
    refus.push('La taille doit etre un nombre positif.');
  } else if (parts < MULTIPLE_MINIMUM * m.minimum) {
    refus.push(
      `Engager au moins ${MULTIPLE_MINIMUM * m.minimum} parts ` +
        `(2 x le minimum de ${m.minimum}) : sinon un remplissage a 50 % ` +
        `laisse un reliquat sous le minimum, donc invendable.`,
    );
  }
  if (cote === 'SELL' && c.ask !== null && prix > c.ask) {
    refus.push(
      `Vente a ${prix} au-dessus du meilleur ask (${c.ask}) : hors marche, ` +
        `elle ne peut pas se remplir.`,
    );
  }
  if (cote === 'BUY' && c.ask !== null && prix >= c.ask) {
    refus.push(
      `Achat a ${prix} au niveau ou au-dessus de l'ask (${c.ask}) : ` +
        `l'ordre traverse l'ecart et paie les frais de preneur.`,
    );
  }
  if (cote === 'SELL' && c.bid !== null && prix <= c.bid) {
    refus.push(
      `Vente a ${prix} au niveau ou sous le bid (${c.bid}) : ` +
        `l'ordre traverse l'ecart et paie les frais de preneur.`,
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

async function connecter() {
  if (!window.ethereum) {
    dire('Aucun portefeuille detecte. Installe MetaMask ou equivalent.', 'erreur');
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
    dire('Signature demandee pour deriver les identifiants CLOB…');

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
    etat.client = await createSecureClient(commun);
    etat.builder = await createSecureClient({
      ...commun,
      apiKey: remoteBuilderSigning({
        url: etat.config.signeur,
        ...(etat.config.jeton ? { headers: { Authorization: `Bearer ${etat.config.jeton}` } } : {}),
      }),
    });
    $('adresse').textContent = `${adresse.slice(0, 6)}…${adresse.slice(-4)}`;
    $('connecter').disabled = true;
    dire(`Connecte : ${adresse}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    dire(`Connexion refusee : ${causes(e)}`, "erreur");
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
    dire(`Solde illisible : ${causes(e)}`, 'erreur');
  }
  try {
    const positions = (await toutes(etat.client.listPositions({}))).filter(
      (p) => Number(p.size) > 0,
    );
    rendrePositions(positions);
  } catch (e) {
    dire(`Positions illisibles : ${causes(e)}`, 'erreur');
  }
  try {
    rendreOrdres(await toutes(etat.client.listOpenOrders()));
  } catch (e) {
    dire(`Ordres illisibles : ${causes(e)}`, 'erreur');
  }
}

function rendrePositions(positions) {
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

function rendreOrdres(ordres) {
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
    cellule.append(bouton);
    tr.append(cellule);
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
      dire('Annulation abandonnee — le reliquat serait invendable.', 'ok');
      return;
    }
  }
  try {
    await etat.client.cancelOrder({ orderId: ordre.id });
    dire(`Ordre annule : ${ordre.side} ${total} @ ${ordre.price}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    dire(`Annulation refusee : ${causes(e)}`, 'erreur');
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

function rendreComposition(lignes) {
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
    dire(`Lecture du carnet impossible : ${e.message || e}`, 'erreur');
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
    dire(`Vente impossible — ${v.refus}`, 'erreur');
    return;
  }
  if (v.avertissement) dire(`Attention — ${v.avertissement}`, 'erreur');
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
  if (!etat.marche) return dire("Charge d'abord un marche.", 'erreur');
  if (!etat.client) return dire('Connecte un portefeuille.', 'erreur');

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
    return dire(`Carnet illisible, rien envoye : ${e.message || e}`, 'erreur');
  }

  const refus = verifier({ cote, prix, parts, carnet: c, marche: m, verdict: etat.sante.get(m.slug) });
  if (refus.length) {
    for (const r of refus) dire(`REFUSE — ${r}`, 'erreur');
    return;
  }

  dire(`Envoi : ${cote} ${parts} @ ${prix} (${fmt(parts * prix)} $)…`);
  try {
    const reponse = await (etat.builder || etat.client).placeLimitOrder({
      tokenId: m.tokens[m.issue],
      price: prix,
      size: parts,
      side: cote,
      ...(etat.config.builderCode ? { builderCode: etat.config.builderCode } : {}),
      // Refuse l'ordre plutot que de traverser l'ecart et devenir preneur.
      postOnly: true,
    });
    dire(`Accepte — ${reponse.orderId || reponse.status || 'ok'}`, 'ok');
    await rafraichirPortefeuille();
  } catch (e) {
    dire(`Refuse par le CLOB : ${causes(e)}`, 'erreur');
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
  if (!etat.config) dire('app-config.json absent : consultation seule.', 'erreur');
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
    dire(`Verdicts indisponibles : ${e.message || e}`, 'erreur');
  }
  $('connecter').addEventListener('click', connecter);
  $('charger').addEventListener('click', rafraichirCarnet);
  $('poser').addEventListener('click', poser);
  $('rafraichir').addEventListener('click', rafraichirPortefeuille);
  $('filtre').addEventListener('input', (e) => rendreListe(e.target.value));
  $('issue').addEventListener('change', rafraichirCarnet);
}

if (typeof document !== 'undefined') demarrer();
