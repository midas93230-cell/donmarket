/**
 * Vérifie que le signeur JavaScript produit EXACTEMENT la signature du SDK Python.
 *
 * Un seul octet d'écart et le CLOB rejette l'ordre sans jamais dire pourquoi :
 * la parité ne se suppose pas, elle se mesure. Ce script signe les mêmes cas des
 * deux côtés et compare les chaînes.
 *
 *   node signer/verify-parity.mjs
 *
 * Les cas ne sont pas décoratifs. L'apostrophe et l'accent sont là parce que
 * l'implémentation d'origine remplace `'` par `"` avant de signer, et parce
 * qu'un corps non-ASCII se hache en UTF-8 : deux endroits où une réécriture
 * naïve diverge silencieusement.
 */

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { _internals } from "./worker.js";

const SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

const CAS = [
  { timestamp: 1755000000, method: "POST", path: "/order", body: '{"a":1}' },
  { timestamp: 1755000000, method: "POST", path: "/order", body: null },
  { timestamp: 1, method: "GET", path: "/orders", body: undefined },
  // l'apostrophe : remplacée par un guillemet avant signature, des deux côtés
  { timestamp: 1755000123, method: "POST", path: "/order", body: '{"q":"Trump\'s win"}' },
  // non-ASCII : le message est haché en UTF-8, pas en latin-1
  { timestamp: 1755000123, method: "POST", path: "/order", body: '{"q":"Élection à 60 %"}' },
  { timestamp: 1755000123, method: "DELETE", path: "/order/0xabc", body: "" },
];

// Chemin résolu depuis CE fichier, pas depuis le répertoire courant : le script
// est aussi appelé par deploy-api.sh, qui s'exécute depuis n'importe où. Un
// garde-fou qui plante selon d'où on l'invoque n'en est pas un.
const RACINE = fileURLToPath(new URL("..", import.meta.url));
const python = process.platform === "win32"
  ? join(RACINE, ".venv", "Scripts", "python.exe")
  : join(RACINE, ".venv", "bin", "python");

let echecs = 0;
for (const cas of CAS) {
  const attendu = execFileSync(
    python,
    [
      "-c",
      [
        "import sys, json",
        "from py_builder_signing_sdk.signing.hmac import build_hmac_signature",
        "c = json.loads(sys.argv[1])",
        "print(build_hmac_signature(sys.argv[2], str(c['timestamp']), c['method'], c['path'], c.get('body')))",
      ].join("\n"),
      JSON.stringify(cas),
      SECRET,
    ],
    { encoding: "utf8" },
  ).trim();

  const obtenu = await _internals.buildSignature(
    SECRET,
    cas.timestamp,
    cas.method,
    cas.path,
    cas.body,
  );

  const ok = obtenu === attendu;
  if (!ok) echecs += 1;
  const corps = cas.body === undefined ? "undefined" : JSON.stringify(cas.body);
  console.log(`${ok ? "OK   " : "ECHEC"} ${cas.method} ${cas.path} body=${corps}`);
  if (!ok) console.log(`      python=${attendu}\n      worker=${obtenu}`);
}

console.log(
  echecs === 0
    ? `\nParité vérifiée sur ${CAS.length} cas — le Worker signe comme le SDK.`
    : `\n${echecs}/${CAS.length} cas DIVERGENT : ne pas déployer.`,
);
process.exit(echecs === 0 ? 0 : 1);
