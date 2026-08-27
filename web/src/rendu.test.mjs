/**
 * Les fonctions de rendu, exercees contre un DOM minimal.
 *
 * POURQUOI CE FICHIER EXISTE. Le 2026-08-27, la page a livre en production avec
 * « Ordres illisibles : tr.append is not a function ». La cause : deux lignes de
 * `rendreOrdres` appelaient `cellule` -- le nom de la FONCTION globale -- la ou
 * il fallait `celluleAction`, la cellule locale. Un renommage incomplet.
 *
 * Les 50 verifications existantes n'ont rien vu, et elles ne pouvaient rien
 * voir : elles testent des fonctions pures, sans DOM. Le rendu, lui, n'etait
 * verifie que par mes yeux dans un navigateur -- ce qui marche jusqu'au jour ou
 * on ne regarde pas la bonne moitie de l'ecran. Les positions s'affichaient ;
 * les ordres, non.
 *
 * Le talon ci-dessous ne simule pas un navigateur : il fournit juste assez pour
 * qu'une erreur de nom, d'appel ou de type EXPLOSE ici plutot qu'a l'ecran. Un
 * noeud qui recoit autre chose qu'un noeud leve, exactement comme le ferait le
 * vrai DOM.
 */
import { rendrePositions, rendreOrdres, rendreComposition } from './app.js';

let echecs = 0;
const verifie = (nom, condition) => {
  console.log(`${condition ? '  ok  ' : 'ECHEC '} ${nom}`);
  if (!condition) echecs += 1;
};

/* ------------------------------------------------------------- talon DOM -- */

class Noeud {
  constructor(nom) {
    this.nom = nom;
    this.enfants = [];
    this.className = '';
    this.style = {};
    this._texte = '';
    this.ecouteurs = 0;
  }
  set textContent(v) {
    this._texte = String(v);
    this.enfants = [];
  }
  get textContent() {
    // Le vrai DOM CONCATENE : poser `textContent` remplace les enfants par un
    // noeud texte, et ce qu'on ajoute ensuite s'y ajoute. Ne rendre que les
    // enfants ferait disparaitre le titre d'une cellule des qu'on lui accroche
    // un sous-element -- et le test mentirait sur ce que voit l'utilisateur.
    return this._texte + this.enfants.map((e) => e.textContent).join('');
  }
  append(...n) {
    for (const x of n) {
      // LE VRAI DOM REFUSE AUSSI : sans ce controle, passer une fonction au lieu
      // d'un noeud serait silencieusement converti en texte, et le bug du
      // 2026-08-27 repasserait.
      if (!(x instanceof Noeud)) {
        throw new TypeError(`append a recu ${typeof x}, pas un noeud`);
      }
      this.enfants.push(x);
    }
  }
  prepend(...n) {
    this.append(...n);
  }
  replaceChildren(...n) {
    this.enfants = [];
    if (n.length) this.append(...n);
  }
  addEventListener() {
    this.ecouteurs += 1;
  }
  get texteComplet() {
    return this.textContent;
  }
  trouver(nom) {
    if (this.nom === nom) return this;
    for (const e of this.enfants) {
      const t = e.trouver(nom);
      if (t) return t;
    }
    return null;
  }
  compter(nom) {
    return (this.nom === nom ? 1 : 0) + this.enfants.reduce((s, e) => s + e.compter(nom), 0);
  }
}

const table = new Map();
globalThis.document = {
  createElement: (nom) => new Noeud(nom),
  getElementById: (id) => table.get(id) || null,
};
const monter = (id) => {
  const n = new Noeud(`#${id}`);
  table.set(id, n);
  return n;
};

/* ------------------------------------------------------------- positions -- */

const positions = monter('positions');
rendrePositions([
  { title: 'Dota 2: Team Yandex vs Nigma Galaxy', outcome: 'Nigma Galaxy', size: 25, avgPrice: 0.11, cashPnl: -2.75 },
  { title: 'Will Lighter reach $3 before 2027?', outcome: 'No', size: 21, avgPrice: 0.13, cashPnl: 1.4 },
]);
verifie('deux positions rendues', positions.enfants.length === 2);
verifie('cinq cellules par ligne', positions.enfants[0].compter('td') === 5);
verifie('un bouton Vendre par ligne', positions.enfants[0].compter('button') === 1);
verifie('le bouton porte un ecouteur', positions.enfants[0].trouver('button').ecouteurs === 1);
verifie('une perte est marquee perte',
  positions.enfants[0].enfants[3].className.includes('perte'));
verifie('un gain est marque gain',
  positions.enfants[1].enfants[3].className.includes('gain'));
verifie('le titre est du texte, pas du balisage',
  positions.enfants[0].enfants[0].textContent.includes('Dota 2'));

const vides = monter('positions');
rendrePositions([]);
verifie('aucune position -> une ligne de message', vides.enfants.length === 1);

/* ---------------------------------------------------------------- ordres -- */
// LE BUG DU 2026-08-27 : cette fonction jetait en production.

const ordres = monter('ordres');
let plante = null;
try {
  rendreOrdres([
    { id: '0x1', side: 'BUY', originalSize: 35, price: 0.06, sizeMatched: 0 },
    { id: '0x2', side: 'SELL', originalSize: 25, price: 0.086, sizeMatched: 23.26 },
  ]);
} catch (e) {
  plante = e;
}
verifie('rendreOrdres ne jette pas', plante === null);
verifie('deux ordres rendus', ordres.enfants.length === 2);
verifie('cinq cellules par ordre', ordres.enfants[0].compter('td') === 5);
verifie('un bouton Annuler par ordre', ordres.enfants[1].compter('button') === 1);
verifie('le remplissage partiel est affiche',
  ordres.enfants[1].textContent.includes('23.26 / 25.00'));

/* ---------------------------------------------------------- composition -- */

const bande = monter('bande');
const legende = monter('legende');
rendreComposition([
  { verdict: 'tradable' }, { verdict: 'tradable' },
  { verdict: 'efficient' },
  { verdict: 'mort' },
]);
verifie('trois segments dans la bande', bande.enfants.length === 3);
verifie('trois entrees de legende', legende.enfants.length === 3);
verifie('chaque entree nomme sa categorie ET son compte',
  legende.enfants.every((li) => /\d/.test(li.textContent) && /[a-z]/.test(li.textContent)));
verifie('un univers vide ne rend aucun segment',
  (rendreComposition([]), bande.enfants.length === 0));

console.log(echecs ? `\n${echecs} verification(s) en echec` : '\ntoutes les verifications passent');
process.exit(echecs ? 1 : 0);
