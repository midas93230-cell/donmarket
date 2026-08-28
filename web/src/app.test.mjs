/**
 * Les garde-fous de l'application, verifies sans navigateur ni reseau.
 *
 * Chaque cas ici correspond a une perte reelle datee. Ce ne sont pas des
 * hypotheses : ce sont les facons dont on a deja perdu de l'argent.
 */
import {
  verifier, avertir, estPreneur, coutPreneur, carnet, vendable, adresseValide,
  composition, estPanneReseau, MULTIPLE_MINIMUM, TAUX_PRENEUR, signatureOrdre,
} from './app.js';

let echecs = 0;
const verifie = (nom, condition) => {
  console.log(`${condition ? '  ok  ' : 'ECHEC '} ${nom}`);
  if (!condition) echecs += 1;
};
const contient = (liste, bout) => liste.some((r) => r.includes(bout));

const MARCHE = { tick: 0.01, minimum: 5 };
const CARNET = { bid: 0.13, bidTaille: 22, ask: 0.18, askTaille: 59 };
const base = (o) => ({ cote: 'BUY', prix: 0.14, parts: 25, carnet: CARNET, marche: MARCHE, ...o });

// --- Le cas nominal passe, sinon tout le reste ne prouve rien -------------
verifie('un achat sain a 0,14 pour 25 parts est accepte', verifier(base()).length === 0);

// --- 2026-08-24 : le minimum EXACT rend l'execution partielle irreversible
verifie(
  'refuse 5 parts quand le minimum est 5',
  contient(verifier(base({ parts: 5 })), 'at least 10 shares'),
);
verifie('accepte exactement 2 x le minimum', verifier(base({ parts: 10 })).length === 0);
verifie('refuse 9 parts, juste sous la regle', verifier(base({ parts: 9 })).length > 0);
verifie('la regle est bien de deux fois le minimum', MULTIPLE_MINIMUM === 2);

// --- 2026-08-25 : une vente au-dessus de l'ask ne se remplit jamais -------
verifie(
  'refuse une vente au-dessus du meilleur ask',
  contient(verifier(base({ cote: 'SELL', prix: 0.25 })), 'above the best ask'),
);
verifie(
  'accepte une vente a 0,17, juste sous l ask',
  verifier(base({ cote: 'SELL', prix: 0.17 })).length === 0,
);

// --- Traverser l'ecart fait payer les frais de preneur --------------------
// --- 2026-08-27 : LE MUR QUI RENDAIT NOTRE REVENU IMPOSSIBLE ---------------
// Notre barieme builder est maker 0, taker 10 bps : on n'est paye QUE sur un
// ordre qui traverse l'ecart. Refuser tout preneur, c'est garantir zero dollar
// quel que soit le nombre d'utilisateurs. On AVERTIT desormais, on ne bloque
// plus -- l'utilisateur decide en connaissant le prix exact.
verifie('un achat a l ask n est plus REFUSE', verifier(base({ prix: 0.18 })).length === 0);
verifie('une vente au bid n est plus REFUSEE',
  verifier(base({ cote: 'SELL', prix: 0.13 })).length === 0);

verifie('un achat a l ask est reconnu preneur',
  estPreneur({ cote: 'BUY', prix: 0.18, carnet: CARNET }) === true);
verifie('un achat au-dessus de l ask est preneur',
  estPreneur({ cote: 'BUY', prix: 0.19, carnet: CARNET }) === true);
verifie('un achat sous l ask reste teneur',
  estPreneur({ cote: 'BUY', prix: 0.17, carnet: CARNET }) === false);
verifie('une vente au bid est preneur',
  estPreneur({ cote: 'SELL', prix: 0.13, carnet: CARNET }) === true);
verifie('une vente au-dessus du bid reste teneuse',
  estPreneur({ cote: 'SELL', prix: 0.14, carnet: CARNET }) === false);
verifie('sans carnet lisible on ne declare pas preneur',
  estPreneur({ cote: 'BUY', prix: 0.18, carnet: { bid: null, ask: null } }) === false);

verifie('le taux preneur mesure est bien 10 bps', TAUX_PRENEUR === 0.001);
verifie('25 parts a 0,18 coutent 0,0045 $ de frais',
  Math.abs(coutPreneur(25, 0.18) - 0.0045) < 1e-9);

verifie('un achat preneur est AVERTI', contient(avertir(base({ prix: 0.18 })), 'crosses the spread'));
verifie('l avertissement chiffre les frais',
  contient(avertir(base({ prix: 0.18 })), '$0.0045'));
verifie('un ordre teneur ne declenche aucun avertissement',
  avertir(base()).length === 0);
verifie('une vente preneuse est AVERTIE',
  contient(avertir(base({ cote: 'SELL', prix: 0.13 })), 'crosses the spread'));

// La confirmation d'un ordre preneur : deux clics identiques, pas de modale
// (une boite de dialogue bloque la page et n'est pas testable).
verifie('deux ordres identiques ont la meme signature',
  signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'abc' })
  === signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'abc' }));
verifie('changer le prix invalide la confirmation',
  signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'abc' })
  !== signatureOrdre({ cote: 'BUY', prix: 0.19, parts: 25, tokenId: 'abc' }));
verifie('changer la taille invalide la confirmation',
  signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'abc' })
  !== signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 26, tokenId: 'abc' }));
verifie('changer de marche invalide la confirmation',
  signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'abc' })
  !== signatureOrdre({ cote: 'BUY', prix: 0.18, parts: 25, tokenId: 'xyz' }));

// Les refus qui restent des refus : ils ne coutent aucun revenu.
verifie('une vente AU-DESSUS de l ask reste refusee',
  contient(verifier(base({ cote: 'SELL', prix: 0.25 })), 'above the best ask'));
verifie('un carnet mort reste un refus, meme pour un preneur',
  verifier(base({ prix: 0.18, verdict: { verdict: 'mort', phrase: 'x' } })).length > 0);
verifie('une taille sous le minimum reste refusee, meme pour un preneur',
  verifier(base({ prix: 0.18, parts: 5 })).length > 0);

// --- 2026-08-26 : un carnet mort ou piege est refuse d'office -------------
verifie(
  'refuse un carnet piege',
  contient(
    verifier(base({ verdict: { verdict: 'piege', phrase: 'personne pour vous servir' } })),
    'piege',
  ),
);
verifie(
  'refuse un carnet mort',
  verifier(base({ verdict: { verdict: 'mort', phrase: 'aucune contrepartie' } })).length > 0,
);
verifie(
  'laisse passer un carnet tradable',
  verifier(base({ verdict: { verdict: 'tradable', phrase: 'ecart avec volume' } })).length === 0,
);

// --- Prix hors bande et hors tick ----------------------------------------
verifie('refuse un prix a 0', verifier(base({ prix: 0 })).length > 0);
verifie('refuse un prix a 1', verifier(base({ prix: 1 })).length > 0);
verifie('refuse un prix hors tick', contient(verifier(base({ prix: 0.145 })), 'multiple of the tick'));
verifie(
  'un tick de 0,001 accepte 0,085',
  verifier(base({ prix: 0.085, marche: { tick: 0.001, minimum: 5 } })).length === 0,
);
verifie('refuse une taille nulle', verifier(base({ parts: 0 })).length > 0);
verifie('refuse un prix non numerique', verifier(base({ prix: Number.NaN })).length > 0);

// --- LE PIEGE DE LECTURE : le meilleur prix est en DERNIERE position ------
const fausseReponse = {
  ok: true,
  json: async () => ({
    // pire prix en premier, comme le CLOB les rend vraiment
    bids: [
      { price: '0.10', size: '5' },
      { price: '0.13', size: '22' },
    ],
    asks: [
      { price: '0.30', size: '9' },
      { price: '0.18', size: '59' },
    ],
  }),
};
const lu = await carnet('jeton', async () => fausseReponse);
verifie('le meilleur bid est lu en derniere position', lu.bid === 0.13);
verifie('le meilleur ask est lu en derniere position', lu.ask === 0.18);

// --- Un carnet a une face vide ne doit pas planter -----------------------
const vide = await carnet('jeton', async () => ({
  ok: true,
  json: async () => ({ bids: [], asks: [] }),
}));
verifie('carnet vide -> bid et ask nuls, sans exception', vide.bid === null && vide.ask === null);
verifie(
  'sans ask connu, un achat reste jugeable sur les autres regles',
  verifier(base({ carnet: { bid: null, ask: null, bidTaille: 0, askTaille: 0 } })).length === 0,
);

// --- LE PIEGE PAYE DEUX FOIS : le reliquat sous le minimum ---------------
// 2026-08-24 a l'ACHAT (2,15 $ bloques), 2026-08-26 a la VENTE (1,74 part
// restee sous un minimum de 5, definitivement invendable).
verifie(
  'refuse de vendre 1,74 part quand le minimum est 5',
  vendable(1.74, 5).refus?.includes('INVENDABLE'),
);
verifie('le refus nomme la seule issue', vendable(1.74, 5).refus?.includes('completer'));
verifie('refuse une position vide', vendable(0, 5).max === 0);
verifie('refuse une taille non numerique', vendable(Number.NaN, 5).max === 0);
verifie('autorise une position de 25 parts', vendable(25, 5).max === 25 && !vendable(25, 5).refus);
verifie(
  'avertit quand la position est sous deux fois le minimum',
  vendable(7, 5).avertissement?.includes('invendables'),
);
verifie("n'avertit pas au-dela de deux fois le minimum", vendable(25, 5).avertissement === null);
verifie('accepte exactement le minimum, en avertissant', vendable(5, 5).max === 5);

// --- La persistance apparait dans le motif de refus ----------------------
verifie(
  'un carnet mort depuis 6 releves le dit',
  verifier(base({ verdict: { verdict: 'mort', phrase: 'aucune contrepartie', persistance: 6 } }))
    .some((r) => r.includes('for 6 runs')),
);

// --- Le champ qui decide OU sont les fonds -------------------------------
verifie('vide -> derivation automatique', adresseValide('') === null);
verifie('espaces seuls -> derivation automatique', adresseValide('   ') === null);
verifie('adresse valide acceptee et nettoyee',
  adresseValide('  0xa53f836A69eB09D48160D2A992c209d2e164F0F4 ') === '0xa53f836A69eB09D48160D2A992c209d2e164F0F4');
verifie('trop courte refusee', adresseValide('0xa53f836A') === false);
verifie('sans prefixe 0x refusee', adresseValide('a53f836A69eB09D48160D2A992c209d2e164F0F4') === false);
verifie('caractere non hexadecimal refuse',
  adresseValide('0xZ53f836A69eB09D48160D2A992c209d2e164F0F4') === false);
verifie('trop longue refusee', adresseValide('0xa53f836A69eB09D48160D2A992c209d2e164F0F4aa') === false);

// --- Composition de l'univers mesure -------------------------------------
// Trois categories, pas quatre : la paire rouge/jaune echouait le plancher de
// VISION NORMALE du validateur de palette. « desequilibre », « piege » et
// « mort » disent la meme chose au trader.
const univers = [
  { verdict: 'tradable' }, { verdict: 'tradable' }, { verdict: 'tradable' },
  { verdict: 'efficient' },
  { verdict: 'mort' }, { verdict: 'piege' }, { verdict: 'desequilibre' }, { verdict: 'lent' },
];
const comp = composition(univers);
verifie('trois categories exactement', comp.length === 3);
verifie('les cotables sont comptes', comp[0].n === 3 && comp[0].nom === 'cotables');
verifie('les serres sont comptes', comp[1].n === 1);
verifie('les quatre verdicts nuisibles sont fusionnes', comp[2].n === 4);
verifie('les parts totalisent 1', Math.abs(comp.reduce((s, c) => s + c.part, 0) - 1) < 1e-9);
verifie('chaque segment porte un nom, pas seulement une couleur',
  comp.every((c) => typeof c.nom === 'string' && c.nom.length > 0));
verifie('un univers vide ne divise pas par zero',
  composition([]).every((c) => c.n === 0 && Number.isFinite(c.part)));
verifie('un verdict inconnu ne fausse aucun compte',
  composition([{ verdict: 'inattendu' }]).reduce((s, c) => s + c.n, 0) === 0);

// --- Panne reseau contre refus -------------------------------------------
// Le 2026-08-27 trois connexions ont echoue sur « Request timed out » ; les
// memes points d'acces repondaient en 0,4 a 2,4 s dans la minute. Le journal
// disait « Connexion refusee », ce qui fait chercher au mauvais endroit.
verifie('un delai depasse est une panne reseau',
  estPanneReseau('Request timed out: POST https://clob.polymarket.com/auth/api-key'));
verifie('fetch failed aussi', estPanneReseau('fetch failed'));
verifie('Failed to fetch aussi', estPanneReseau('TypeError: Failed to fetch'));
verifie('un refus de signature en est exclu',
  !estPanneReseau('User rejected the request. Details: Request Signature'));
verifie('un 403 du signeur en est exclu',
  !estPanneReseau('Remote signer rejected request with status 403'));
verifie('un message vide ne plante pas', estPanneReseau(undefined) === false);

// --- FAILLE XSS FERMEE LE 2026-08-27, verrouillee ici ---------------------
// Les lignes de tableau etaient construites par `tr.innerHTML = ...` avec des
// titres de marche venus de l'API. Sur une page connectee a un portefeuille,
// une charge utile pouvait reecrire le formulaire d'ordre avant l'envoi : la
// signature de l'utilisateur serait authentique, son CONTENU falsifie.
//
// Ce controle est STATIQUE a dessein. Un test de comportement passerait encore
// si quelqu'un reintroduisait un `innerHTML` ailleurs dans le fichier ; ici on
// interdit la construction elle-meme.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const source = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
const sansCommentaires = source
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^[ \t]*\/\/.*$/gm, '');

verifie(
  'aucune affectation innerHTML dans le code',
  !/\.innerHTML\s*=/.test(sansCommentaires),
);
verifie(
  'aucun insertAdjacentHTML ni document.write',
  !/insertAdjacentHTML|document\.write/.test(sansCommentaires),
);
verifie('aucun eval ni new Function', !/\beval\s*\(|new Function\s*\(/.test(sansCommentaires));
verifie(
  'le rendu passe par textContent',
  /textContent\s*=/.test(sansCommentaires),
);

console.log(echecs ? `\n${echecs} verification(s) en echec` : '\ntoutes les verifications passent');
process.exit(echecs ? 1 : 0);
