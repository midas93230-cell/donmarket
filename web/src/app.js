/**
 * DONmarket — passer un ordre Polymarket sans rien installer.
 *
 * CE QUE CETTE PAGE FAIT QUE L'INTERFACE OFFICIELLE NE FAIT PAS
 *
 * Elle REFUSE des ordres. Avant d'envoyer, elle confronte l'ordre au carnet et
 * bloque les quatre pieges qui nous ont coute cinq jours et de l'argent reel :
 *
 *   1. Carnet mort ou piege -- un ecart large sur un marche sans volume paie
 *      sur le papier et ne se remplit jamais. Mesure du 2026-08-26 : sur les
 *      173 marches vivants et finançables, l'ecart vaut EXACTEMENT un tick.
 *      Un ecart plus large signale l'absence de contrepartie, pas une occasion.
 *   2. Taille au minimum exact -- un remplissage partiel laisse alors un
 *      reliquat SOUS le minimum d'ordre, donc INVENDABLE. Le 2026-08-24, une
 *      position de 2,15 $ est morte ainsi, pour six milliemes de part.
 *      D'ou la regle : au moins DEUX FOIS `orderMinSize`.
 *   3. Vente au-dessus du meilleur ask -- hors marche, jamais remplissable.
 *      Le 2026-08-25 un ordre a passe 22 h a 0,139 quand l'ask etait a 0,086.
 *   4. Achat qui traverse l'ecart -- on devient preneur et on paie les frais
 *      qu'un teneur evite. `postOnly` fait refuser l'ordre plutot que cela.
 *
 * ATTRIBUTION
 *
 * Le corps de l'ordre ne porte aucune preuve de builder : l'attribution passe
 * par quatre en-tetes SIGNES avec un secret d'API. Ce secret ne peut pas vivre
 * dans une page web. Il vit dans un Cloudflare Worker (`signer/worker.js`) que
 * le SDK appelle via `remoteBuilderSigning` : le Worker ne voit jamais la cle
 * privee de l'utilisateur, et l'utilisateur ne voit jamais notre secret.
 */

import { createSecureClient, remoteBuilderSigning } from '@polymarket/client';
import { signerFrom } from '@polymarket/client/viem';
import { createWalletClient, custom } from 'viem';
import { polygon } from 'viem/chains';

const CONFIG_URL = './app-config.json';
const SANTE_URL = './health.json';

/** Deux fois le minimum d'ordre : une execution a 50 % laisse de quoi sortir. */
export const MULTIPLE_MINIMUM = 2;

/** Verdicts sur lesquels on refuse d'engager de l'argent. */
const VERDICTS_REFUSES = new Set(['mort', 'piege']);

const etat = {
  config: null,
  sante: new Map(), // slug -> ligne de verdict
  client: null,
  adresse: null,
  marche: null,
};

const $ = (id) => document.getElementById(id);

function dire(message, genre = 'info') {
  const zone = $('journal');
  const ligne = document.createElement('div');
  ligne.className = `ligne ${genre}`;
  ligne.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
  zone.prepend(ligne);
}

/* ---------------------------------------------------------------- donnees */

async function chargerConfig() {
  try {
    const r = await fetch(CONFIG_URL, { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    return await r.json();
  } catch {
    // Sans configuration la page reste utile en LECTURE : on peut consulter
    // les verdicts. Seule la pose d'ordre exige le signeur.
    return null;
  }
}

async function chargerSante() {
  const r = await fetch(SANTE_URL, { cache: 'no-store' });
  if (!r.ok) throw new Error(`sante illisible (${r.status})`);
  const lignes = await r.json();
  for (const l of lignes) etat.sante.set(l.slug, l);
  return lignes;
}

export async function carnet(tokenId, chercher = fetch) {
  const r = await chercher(
    `https://clob.polymarket.com/book?token_id=${encodeURIComponent(tokenId)}`,
  );
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
  const r = await fetch(
    `https://gamma-api.polymarket.com/markets?slug=${encodeURIComponent(slug)}`,
  );
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

/* ------------------------------------------------------------ garde-fous */

/**
 * Rend la liste des refus. Une liste VIDE veut dire « rien ne s'y oppose »,
 * pas « c'est une bonne idee » : ces controles disent que l'ordre peut
 * s'executer, pas qu'il est rentable.
 */
export function verifier({ cote, prix, parts, carnet: c, marche: m, verdict }) {
  const refus = [];

  if (verdict && VERDICTS_REFUSES.has(verdict.verdict)) {
    refus.push(`Carnet « ${verdict.verdict} » : ${verdict.phrase}`);
  }
  if (!Number.isFinite(prix) || prix <= 0 || prix >= 1) {
    refus.push('Le prix doit tenir strictement entre 0 et 1.');
  } else {
    const reste = Math.abs(prix / m.tick - Math.round(prix / m.tick));
    if (reste > 1e-6) {
      refus.push(`Le prix doit etre un multiple du tick (${m.tick}).`);
    }
  }
  if (!Number.isFinite(parts) || parts <= 0) {
    refus.push('La taille doit etre un nombre positif.');
  } else if (parts < MULTIPLE_MINIMUM * m.minimum) {
    // LE PIEGE DU 2026-08-24, et le plus couteux : acheter le minimum EXACT
    // rend toute execution partielle irreversible.
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

/* ---------------------------------------------------------- portefeuille */

async function connecter() {
  if (!window.ethereum) {
    dire('Aucun portefeuille detecte. Installe MetaMask ou equivalent.', 'erreur');
    return;
  }
  if (!etat.config) {
    dire(
      "Pas de app-config.json : la pose d'ordre est desactivee, " +
        'la consultation reste possible.',
      'erreur',
    );
    return;
  }
  try {
    const walletClient = createWalletClient({
      chain: polygon,
      transport: custom(window.ethereum),
    });
    const [adresse] = await walletClient.requestAddresses();
    etat.adresse = adresse;

    etat.client = await createSecureClient({
      signer: signerFrom(walletClient),
      // L'attribution passe par notre Worker : il detient le secret builder,
      // cette page ne le voit jamais.
      apiKey: remoteBuilderSigning({
        url: etat.config.signeur,
        headers: etat.config.jeton
          ? { Authorization: `Bearer ${etat.config.jeton}` }
          : undefined,
      }),
    });
    $('adresse').textContent = `${adresse.slice(0, 6)}…${adresse.slice(-4)}`;
    $('connecter').disabled = true;
    dire(`Portefeuille connecte : ${adresse}`, 'ok');
  } catch (e) {
    dire(`Connexion refusee : ${e.message || e}`, 'erreur');
  }
}

/* ------------------------------------------------------------------ ordre */

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
      `issue « ${m.issues[issue]} »   tick ${m.tick}   minimum ${m.minimum}\n` +
      `bid ${c.bid} (${c.bidTaille})   ask ${c.ask} (${c.askTaille})\n` +
      (v ? `verdict : ${v.verdict} — ${v.phrase}` : 'verdict : non mesure');
    $('verdict').className = `pastille ${v ? v.verdict : 'inconnu'}`;
    $('verdict').textContent = v ? v.verdict : 'non mesure';
  } catch (e) {
    dire(`Lecture du carnet impossible : ${e.message || e}`, 'erreur');
  }
}

async function poser() {
  if (!etat.marche) {
    dire("Charge d'abord un marche.", 'erreur');
    return;
  }
  if (!etat.client) {
    dire('Connecte un portefeuille avant de poser un ordre.', 'erreur');
    return;
  }
  const cote = $('cote').value;
  const prix = Number($('prix').value);
  const parts = Number($('parts').value);
  const m = etat.marche;

  // On RELIT le carnet juste avant : entre l'affichage et le clic, il a pu
  // bouger. Le 2026-08-26, un ecart de 0,43/0,58 s'est referme a un tick en
  // dix minutes.
  let c;
  try {
    c = await carnet(m.tokens[m.issue]);
  } catch (e) {
    dire(`Carnet illisible, rien envoye : ${e.message || e}`, 'erreur');
    return;
  }

  const refus = verifier({
    cote,
    prix,
    parts,
    carnet: c,
    marche: m,
    verdict: etat.sante.get(m.slug),
  });
  if (refus.length) {
    for (const r of refus) dire(`REFUSE — ${r}`, 'erreur');
    return;
  }

  dire(`Envoi : ${cote} ${parts} @ ${prix} (${(parts * prix).toFixed(2)} $)…`);
  try {
    const reponse = await etat.client.placeLimitOrder({
      tokenId: m.tokens[m.issue],
      price: prix,
      size: parts,
      side: cote,
      builderCode: etat.config.builderCode,
      // Refuse l'ordre plutot que de traverser l'ecart et devenir preneur.
      postOnly: true,
    });
    dire(`Accepte : ${JSON.stringify(reponse)}`, 'ok');
  } catch (e) {
    dire(`Refuse par le CLOB : ${e.message || e}`, 'erreur');
  }
}

/* -------------------------------------------------------------- demarrage */

async function demarrer() {
  etat.config = await chargerConfig();
  if (!etat.config) {
    dire('app-config.json absent : consultation seule.', 'erreur');
  }
  try {
    const lignes = await chargerSante();
    const vivants = lignes.filter((l) => l.verdict === 'tradable').length;
    dire(`${lignes.length} carnets mesures, ${vivants} cotables.`, 'ok');
    const liste = $('suggestions');
    for (const l of lignes.filter((x) => x.verdict === 'tradable').slice(0, 60)) {
      const o = document.createElement('option');
      o.value = l.slug;
      o.textContent = `${l.question} — ecart ${l.ecart_pct.toFixed(1)} %`;
      liste.append(o);
    }
  } catch (e) {
    dire(`Verdicts indisponibles : ${e.message || e}`, 'erreur');
  }
  $('connecter').addEventListener('click', connecter);
  $('charger').addEventListener('click', rafraichirCarnet);
  $('poser').addEventListener('click', poser);
}

if (typeof document !== 'undefined') demarrer();
