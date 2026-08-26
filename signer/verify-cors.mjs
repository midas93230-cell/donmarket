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

const { corsHeaders, ORIGINES_AUTORISEES, cheminAutorise } = _internals;
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

// 5. LES DEUX CHEMINS D'ENTREE.
// Une page d'origine connue n'a PAS a presenter le jeton fort : le publier
// dans une source consultable ne protegerait rien et exposerait le chemin CLI.
const navigateur = await worker.fetch(req("POST", BON, { Authorization: "Bearer faux" }),
                                      { AUTH_TOKEN: "vrai-jeton" });
verifie("origine connue -> le jeton fort n'est pas exige", navigateur.status !== 401);
// Hors de la liste, le jeton reste obligatoire.
const horsListe = await worker.fetch(req("POST", "https://mechant.example",
                                         { Authorization: "Bearer faux" }),
                                     { AUTH_TOKEN: "vrai-jeton" });
verifie("origine inconnue + mauvais jeton -> 401", horsListe.status === 401);
const sansOrigine = await worker.fetch(req("POST", null, { Authorization: "Bearer vrai-jeton" }),
                                       { AUTH_TOKEN: "vrai-jeton" });
verifie("CLI avec le bon jeton -> passe l'authentification", sansOrigine.status !== 401);

// 6. LE POINT QUI COUTE DES HEURES : un refus doit porter le CORS.
const refusCors = await worker.fetch(req("POST", BON), {});
verifie("un refus depuis une origine connue porte le CORS",
  refusCors.headers.get("access-control-allow-origin") === BON);

// 6. Un signeur non configure refuse aussi lisiblement.
const nonConfig = await worker.fetch(req("POST", BON), {});
verifie("503 -> le code est lisible par la page",
  nonConfig.status === 503 && nonConfig.headers.get("access-control-allow-origin") === BON);

// 7. Une requete sans Origin (curl, CLI) continue de marcher comme avant.
verifie("sans Origin -> aucun CORS, pas de plantage",
  Object.keys(corsHeaders(req("POST", null))).length === 0);

// 7. LES CHEMINS DANGEREUX SONT REFUSES, LE RESTE PASSE.
// Une liste BLANCHE avait ete tentee d'abord : elle etait fausse par
// construction, le SDK signant une trentaine de chemins. Le risque reel est
// concentre sur la gestion des cles, la seule chose irreversible ici.
verifie("signe /order", cheminAutorise("/order"));
verifie("signe /orders", cheminAutorise("/orders"));
verifie("signe /auth/derive-api-key (connexion)", cheminAutorise("/auth/derive-api-key"));
verifie("signe /auth/api-key", cheminAutorise("/auth/api-key"));
verifie("signe /balance-allowance", cheminAutorise("/balance-allowance"));
verifie("signe /data/orders", cheminAutorise("/data/orders"));
verifie("signe /cancel-all", cheminAutorise("/cancel-all"));
verifie("signe /closed-positions", cheminAutorise("/closed-positions"));
verifie("REFUSE la gestion de la cle builder", !cheminAutorise("/auth/builder-api-key"));
verifie("REFUSE un sous-chemin de la cle builder", !cheminAutorise("/auth/builder-api-key/revoke"));
verifie("REFUSE la liste des cles d'API", !cheminAutorise("/auth/api-keys"));
verifie("REFUSE un sous-chemin des cles", !cheminAutorise("/auth/api-keys/123"));
verifie("l'interdit resiste a la chaine de requete", !cheminAutorise("/auth/api-keys?x=1"));
verifie("l'interdit resiste a la barre finale", !cheminAutorise("/auth/api-keys/"));
// La frontiere est une BARRE : sans elle, /auth/api-key serait pris pour un
// prefixe de /auth/api-keys et la connexion resterait cassee.
verifie("/auth/api-key n'est PAS bloque par /auth/api-keys", cheminAutorise("/auth/api-key"));
verifie("refuse un chemin absent", !cheminAutorise(undefined));
verifie("refuse un chemin sans barre initiale", !cheminAutorise("auth/api-keys"));
const chemin = await worker.fetch(
  new Request("https://signeur.test/", {
    method: "POST",
    headers: { Origin: BON, "content-type": "application/json" },
    body: JSON.stringify({ method: "POST", path: "/auth/builder-api-key", body: "{}" }),
  }),
  { AUTH_TOKEN: "x", BUILDER_API_KEY: "k", BUILDER_API_SECRET: "cw", BUILDER_API_PASSPHRASE: "p" },
);
verifie("bout en bout : un chemin non signable est refuse en 403", chemin.status === 403);

console.log(echecs ? `\n${echecs} verification(s) en echec` : "\ntoutes les verifications passent");
process.exit(echecs ? 1 : 0);
