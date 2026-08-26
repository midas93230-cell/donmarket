/**
 * Signeur builder DONmarket — Cloudflare Worker.
 *
 * POURQUOI CE SERVICE EXISTE
 *
 * L'attribution du volume Polymarket passe à 100 % par quatre en-têtes SIGNÉS
 * avec le secret d'API builder ; le corps de l'ordre n'en porte aucune trace.
 * Un utilisateur tiers qui clone le dépôt public n'a donc aucun moyen de nous
 * attribuer son volume — et publier le secret pour y remédier le ferait
 * révoquer. Ce service est la sortie prévue par le SDK (`BuilderType.REMOTE`) :
 * il détient le secret, le tiers ne le voit jamais, et sa clé privée à lui ne
 * quitte jamais sa machine.
 *
 * CE QU'IL VOIT, DIT SANS DÉTOUR
 *
 * La signature couvre le corps de l'ordre : ce service reçoit donc chaque ordre
 * avant qu'il n'atteigne le carnet. C'est inhérent au protocole, pas un choix.
 * D'où les deux règles tenues ici : rien n'est journalisé (ni corps, ni
 * en-têtes), et le code est public pour que la promesse soit vérifiable plutôt
 * que crue sur parole.
 *
 * ALGORITHME — relevé sur `py_builder_signing_sdk.signing.hmac`, pas deviné :
 *   secret décodé en base64 URL-safe → clé HMAC-SHA256
 *   message = timestamp + method + path + body
 *   signature = base64 URL-safe du digest, padding compris
 *
 * Le remplacement `'` → `"` reproduit fidèlement l'implémentation d'origine :
 * elle existe pour que Python, Go et TypeScript produisent le MÊME message. S'en
 * écarter ne « corrigerait » rien, ça produirait une signature que le CLOB
 * rejette dès qu'un corps contient une apostrophe.
 *
 * DÉPLOIEMENT
 *   wrangler secret put BUILDER_API_KEY
 *   wrangler secret put BUILDER_API_SECRET
 *   wrangler secret put BUILDER_API_PASSPHRASE
 *   wrangler secret put AUTH_TOKEN
 *   wrangler deploy
 */

const HEADER_FIELDS = [
  "POLY_BUILDER_API_KEY",
  "POLY_BUILDER_TIMESTAMP",
  "POLY_BUILDER_PASSPHRASE",
  "POLY_BUILDER_SIGNATURE",
];

/** base64 URL-safe → octets. Le padding manquant est toléré, comme en Python. */
function base64UrlToBytes(value) {
  const normalised = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalised + "=".repeat((4 - (normalised.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Octets → base64 URL-SAFE **avec padding** : c'est ce que Python produit. */
function bytesToBase64Url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_");
}

/**
 * Comparaison à durée constante. Un `===` sur un jeton fuit sa longueur commune
 * par le temps de réponse, ce qui se mesure à distance et se remonte octet par
 * octet.
 */
function tokensMatch(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function buildSignature(secret, timestamp, method, path, body) {
  let message = String(timestamp) + String(method) + String(path);
  if (body) message += String(body).replace(/'/g, '"');

  const key = await crypto.subtle.importKey(
    "raw",
    base64UrlToBytes(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return bytesToBase64Url(digest);
}

/**
 * Origines autorisées à appeler ce signeur depuis un NAVIGATEUR.
 *
 * Sans ces en-têtes, une page web ne peut pas nous appeler du tout : le
 * navigateur bloque la réponse avant que le code de la page ne la voie. Le CLOB
 * Polymarket, lui, répond `Access-Control-Allow-Origin: *` — c'est ce qui rend
 * une application entièrement statique possible.
 *
 * On garde une LISTE plutôt que `*` : le jeton d'un client navigateur est
 * forcément public (il est dans la source de la page), donc la seule barrière
 * restante est l'origine. Un attaquant ne pourrait de toute façon rien nous
 * voler — les en-têtes obtenus attribuent le volume À NOUS — mais il pourrait
 * épuiser le quota du Worker.
 */
const ORIGINES_AUTORISEES = new Set([
  "https://midas93230-cell.github.io",
  "http://localhost:8000",
  "http://127.0.0.1:8000",
]);

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (!ORIGINES_AUTORISEES.has(origin)) return {};
  return {
    "access-control-allow-origin": origin,
    "access-control-allow-methods": "POST, OPTIONS",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-max-age": "86400",
    // L'origine varie la réponse : sans `Vary`, un cache pourrait servir
    // l'en-tête d'une origine à une autre.
    vary: "Origin",
  };
}

function refuse(status, message, request) {
  // Le message reste générique : dire « jeton invalide » plutôt que « absent »
  // renseignerait un visiteur sur ce qu'il doit corriger pour s'approcher.
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: {
      "content-type": "application/json",
      // Un refus SANS CORS s'affiche dans le navigateur comme une panne réseau
      // opaque : la page ne voit ni le code ni le message. Le client passerait
      // des heures à chercher une erreur de réseau là où il y a un 401.
      ...(request ? corsHeaders(request) : {}),
    },
  });
}

export default {
  async fetch(request, env) {
    // Le navigateur envoie un OPTIONS avant tout POST cross-origin. Y répondre
    // 405 fait échouer la requête réelle avant qu'elle ne parte.
    if (request.method === "OPTIONS") {
      const entetes = corsHeaders(request);
      if (!Object.keys(entetes).length) return refuse(403, "origine non autorisée", request);
      return new Response(null, { status: 204, headers: entetes });
    }
    if (request.method !== "POST") return refuse(405, "POST attendu", request);

    const expected = env.AUTH_TOKEN;
    if (expected) {
      const header = request.headers.get("Authorization") || "";
      const prefix = "Bearer ";
      const presented = header.startsWith(prefix) ? header.slice(prefix.length) : "";
      if (!tokensMatch(presented, expected)) return refuse(401, "non autorisé", request);
    }

    if (!env.BUILDER_API_SECRET || !env.BUILDER_API_KEY || !env.BUILDER_API_PASSPHRASE) {
      // Renvoyer 200 avec des en-têtes creux serait pire que refuser : le client
      // enverrait l'ordre avec une signature fausse et le CLOB le rejetterait,
      // très loin de la cause.
      return refuse(503, "signeur non configuré", request);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return refuse(400, "corps JSON illisible", request);
    }

    const method = payload && payload.method;
    const path = payload && payload.path;
    if (!method || !path) return refuse(400, "`method` et `path` sont requis", request);

    // Le client py-clob-client n'envoie PAS de timestamp : le signataire local
    // le fabrique lui-même, donc le signataire distant doit le faire aussi — et
    // le RENVOYER, sinon l'en-tête et le message signé divergeraient.
    const timestamp =
      payload.timestamp === null || payload.timestamp === undefined
        ? Math.floor(Date.now() / 1000)
        : payload.timestamp;

    const signature = await buildSignature(
      env.BUILDER_API_SECRET,
      timestamp,
      method,
      path,
      payload.body,
    );

    const headers = {
      POLY_BUILDER_API_KEY: env.BUILDER_API_KEY,
      POLY_BUILDER_TIMESTAMP: String(timestamp),
      POLY_BUILDER_PASSPHRASE: env.BUILDER_API_PASSPHRASE,
      POLY_BUILDER_SIGNATURE: signature,
    };

    // Aucune journalisation : ni corps d'ordre, ni en-têtes, ni jeton. La
    // promesse « je ne garde rien » ne vaut que si le code la tient.
    return new Response(JSON.stringify(headers), {
      status: 200,
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
        ...corsHeaders(request),
      },
    });
  },
};

export const _internals = { corsHeaders, ORIGINES_AUTORISEES, buildSignature, bytesToBase64Url, base64UrlToBytes, HEADER_FIELDS };
