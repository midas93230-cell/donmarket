/**
 * Les garde-fous de l'application, verifies sans navigateur ni reseau.
 *
 * Chaque cas ici correspond a une perte reelle datee. Ce ne sont pas des
 * hypotheses : ce sont les facons dont on a deja perdu de l'argent.
 */
import { verifier, carnet, MULTIPLE_MINIMUM } from './app.js';

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
  contient(verifier(base({ parts: 5 })), 'au moins 10 parts'),
);
verifie('accepte exactement 2 x le minimum', verifier(base({ parts: 10 })).length === 0);
verifie('refuse 9 parts, juste sous la regle', verifier(base({ parts: 9 })).length > 0);
verifie('la regle est bien de deux fois le minimum', MULTIPLE_MINIMUM === 2);

// --- 2026-08-25 : une vente au-dessus de l'ask ne se remplit jamais -------
verifie(
  'refuse une vente au-dessus du meilleur ask',
  contient(verifier(base({ cote: 'SELL', prix: 0.25 })), 'au-dessus du meilleur ask'),
);
verifie(
  'accepte une vente a 0,17, juste sous l ask',
  verifier(base({ cote: 'SELL', prix: 0.17 })).length === 0,
);

// --- Traverser l'ecart fait payer les frais de preneur --------------------
verifie('refuse un achat a l ask', contient(verifier(base({ prix: 0.18 })), "traverse l'ecart"));
verifie(
  'refuse une vente au bid',
  contient(verifier(base({ cote: 'SELL', prix: 0.13 })), "traverse l'ecart"),
);

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
verifie('refuse un prix hors tick', contient(verifier(base({ prix: 0.145 })), 'multiple du tick'));
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

console.log(echecs ? `\n${echecs} verification(s) en echec` : '\ntoutes les verifications passent');
process.exit(echecs ? 1 : 0);
