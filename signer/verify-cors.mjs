/**
 * Verifie la couche CORS du signeur SANS reseau ni secrets.
 *
 * Une page web ne peut appeler ce Worker que si trois choses tiennent :
 * le preflight OPTIONS repond 204 avec les bons en-tetes, une origine inconnue
 * est refusee, et un REFUS porte lui aussi le CORS -- sinon le navigateur cache
 * le code d'erreur derriere une panne reseau opaque et le client cherche au
 * mauvais endroit.
 */
import worker, { _internals } from "./worker.js";

const { corsHeaders, ORIGINES_AUTORISEES } = _internals;
const BON = "https://midas93230-cell.github.io";
let echecs = 0;
const verifie = (nom, condition) => {
  console.log(`${condition ? "  ok  " : "ECHEC "} ${nom}`);
  if (!condition) echecs += 1;
};

const req = (methode, origine, entetes = {}) =>
  new Request("https://signeur.test/", {
    method: methode,
    headers: { ...(origine ? { Origin: origine } : {}), ...entetes },
    ...(methode === "POST" ? { body: "{}" } : {}),
  });

// 1. Une origine connue recoit bien les en-tetes.
const h = corsHeaders(req("POST", BON));
verifie("origine autorisee -> allow-origin renvoye", h["access-control-allow-origin"] === BON);
verifie("l'en-tete Vary est pose", h.vary === "Origin");
verifie("authorization est autorise", (h["access-control-allow-headers"] || "").includes("authorization"));

// 2. Une origine inconnue n'en recoit aucun.
verifie("origine inconnue -> aucun en-tete",
  Object.keys(corsHeaders(req("POST", "https://mechant.example"))).length === 0);
verifie("localhost de dev est autorise", ORIGINES_AUTORISEES.has("http://localhost:8000"));

// 3. Le preflight repond 204 sans corps.
const pre = await worker.fetch(req("OPTIONS", BON), {});
verifie("preflight -> 204", pre.status === 204);
verifie("preflight -> allow-origin", pre.headers.get("access-control-allow-origin") === BON);

// 4. Un preflight d'origine inconnue est refuse, mais proprement.
const preMechant = await worker.fetch(req("OPTIONS", "https://mechant.example"), {});
verifie("preflight inconnu -> 403", preMechant.status === 403);

// 5. LE POINT QUI COUTE DES HEURES : un refus doit porter le CORS.
const refus = await worker.fetch(req("POST", BON, { Authorization: "Bearer faux" }),
                                 { AUTH_TOKEN: "vrai-jeton" });
verifie("401 -> le code est lisible par la page",
  refus.status === 401 && refus.headers.get("access-control-allow-origin") === BON);

// 6. Un signeur non configure refuse aussi lisiblement.
const nonConfig = await worker.fetch(req("POST", BON), {});
verifie("503 -> le code est lisible par la page",
  nonConfig.status === 503 && nonConfig.headers.get("access-control-allow-origin") === BON);

// 7. Une requete sans Origin (curl, CLI) continue de marcher comme avant.
verifie("sans Origin -> aucun CORS, pas de plantage",
  Object.keys(corsHeaders(req("POST", null))).length === 0);

console.log(echecs ? `\n${echecs} verification(s) en echec` : "\ntoutes les verifications passent");
process.exit(echecs ? 1 : 0);
