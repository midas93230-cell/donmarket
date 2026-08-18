#!/usr/bin/env bash
# Déploiement du signeur builder SANS wrangler ni npm.
#
# Pourquoi ce script existe : `npm install wrangler` échoue sur cette machine
# (EIDLETIMEOUT sur registry.npmjs.org — les métadonnées passent, les archives
# expirent). L'API REST Cloudflare fait exactement le même travail en curl, qui
# est déjà installé.
#
# Prérequis, une seule fois, sur dash.cloudflare.com :
#   1. My Profile > API Tokens > Create Token > modèle « Edit Cloudflare Workers »
#   2. relever l'Account ID (barre latérale de n'importe quel domaine, ou
#      Workers & Pages > Overview)
#
# Puis, DANS LE TERMINAL (jamais dans un fichier versionné) :
#   export CF_API_TOKEN=...    # le jeton créé à l'étape 1
#   export CF_ACCOUNT_ID=...   # l'identifiant de compte
#   bash signer/deploy-api.sh
#
# Le script ne journalise aucune valeur secrète : ni le jeton Cloudflare, ni les
# identifiants builder, ni le jeton porteur distribué aux clients.
set -euo pipefail

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$ICI/../.env"
NOM="donmarket-signer"
API="https://api.cloudflare.com/client/v4"
DATE_COMPAT="2026-08-17"

: "${CF_API_TOKEN:?CF_API_TOKEN manquant — voir l'en-tête de ce script}"
: "${CF_ACCOUNT_ID:?CF_ACCOUNT_ID manquant — voir l'en-tête de ce script}"

# --- Lecture des identifiants builder depuis le .env du projet -----------------
# Le .env est ignoré par git. Les valeurs ne transitent que par des variables du
# shell : aucune n'apparaît sur une ligne de commande, qui serait lisible par
# tout le système.
lire_env() {
  local cle="$1" val
  val="$(grep -E "^${cle}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
  [ -n "$val" ] || { echo "ERREUR : ${cle} vide ou absent dans $ENV_FILE" >&2; exit 1; }
  case "$val" in
    dpapi:*) echo "ERREUR : ${cle} est scellé DPAPI. Le desceller d'abord :" >&2
             echo "  .venv/Scripts/python -m donmarket unseal ${cle}" >&2; exit 1 ;;
  esac
  printf '%s' "$val"
}

BUILDER_API_KEY="$(lire_env POLYMARKET_BUILDER_API_KEY)"
BUILDER_API_SECRET="$(lire_env POLYMARKET_BUILDER_API_SECRET)"
BUILDER_API_PASSPHRASE="$(lire_env POLYMARKET_BUILDER_API_PASSPHRASE)"

# Le jeton porteur exigé des clients. Fourni par l'appelant, sinon engendré.
# 32 octets aléatoires : deviner ce jeton est le seul moyen de faire signer des
# ordres à nos frais.
if [ -z "${AUTH_TOKEN:-}" ]; then
  AUTH_TOKEN="$(node -e 'process.stdout.write(require("crypto").randomBytes(32).toString("base64url"))')"
  JETON_ENGENDRE=1
else
  JETON_ENGENDRE=0
fi

# --- Garde-fou : parité de signature avant toute mise en ligne -----------------
# Un octet d'écart entre le Worker et le SDK Python et le CLOB rejette l'ordre
# sans dire pourquoi. Déployer sans cette vérification n'a aucun sens.
echo "== Vérification de parité (Worker vs SDK Python)"
node "$ICI/verify-parity.mjs"

# --- Envoi du script -----------------------------------------------------------
# keep_bindings préserve les secrets déjà posés : sans lui, un redéploiement les
# effacerait en silence et le signeur rendrait 503 à tous les clients.
# curl est ici une compilation MinGW : elle ne sait pas ouvrir un chemin POSIX
# (/c/Users/...) et rend « Failed to open/read local data ». cygpath rend la
# forme Windows que curl attend.
CHEMIN_WORKER="$ICI/worker.js"
if command -v cygpath >/dev/null 2>&1; then
  CHEMIN_WORKER="$(cygpath -w "$CHEMIN_WORKER")"
fi

echo "== Envoi de worker.js"
reponse="$(curl -sS -X PUT "$API/accounts/$CF_ACCOUNT_ID/workers/scripts/$NOM" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  -F "metadata={\"main_module\":\"worker.js\",\"compatibility_date\":\"$DATE_COMPAT\",\"keep_bindings\":[\"secret_text\"],\"observability\":{\"enabled\":true}};type=application/json" \
  -F "worker.js=@$CHEMIN_WORKER;type=application/javascript+module")"
node -e '
  const r = JSON.parse(process.argv[1]);
  if (!r.success) { console.error("ECHEC envoi :", JSON.stringify(r.errors)); process.exit(1); }
' "$reponse"

# --- Pose des quatre secrets ---------------------------------------------------
poser_secret() {
  local nom="$1" valeur="$2" corps rep
  corps="$(VAL="$valeur" NOM="$nom" node -e '
    process.stdout.write(JSON.stringify({
      name: process.env.NOM, text: process.env.VAL, type: "secret_text"
    }))')"
  rep="$(printf '%s' "$corps" | curl -sS -X PUT \
    "$API/accounts/$CF_ACCOUNT_ID/workers/scripts/$NOM/secrets" \
    -H "Authorization: Bearer $CF_API_TOKEN" \
    -H "Content-Type: application/json" --data @-)"
  node -e '
    const r = JSON.parse(process.argv[1]);
    if (!r.success) { console.error("ECHEC secret " + process.argv[2] + " :", JSON.stringify(r.errors)); process.exit(1); }
  ' "$rep" "$nom"
  echo "   $nom posé"
}

echo "== Pose des secrets"
poser_secret BUILDER_API_KEY        "$BUILDER_API_KEY"
poser_secret BUILDER_API_SECRET     "$BUILDER_API_SECRET"
poser_secret BUILDER_API_PASSPHRASE "$BUILDER_API_PASSPHRASE"
poser_secret AUTH_TOKEN             "$AUTH_TOKEN"

# --- Exposition sur workers.dev ------------------------------------------------
echo "== Activation de l'adresse publique"
printf '{"enabled":true,"previews_enabled":false}' | curl -sS -X POST \
  "$API/accounts/$CF_ACCOUNT_ID/workers/scripts/$NOM/subdomain" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  -H "Content-Type: application/json" --data @- >/dev/null

sous_domaine="$(curl -sS "$API/accounts/$CF_ACCOUNT_ID/workers/subdomain" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{
      const r=JSON.parse(d); process.stdout.write(r.success ? r.result.subdomain : "");})')"

URL="https://$NOM.$sous_domaine.workers.dev"
echo
echo "== En ligne : $URL"
echo
echo "À poser dans le .env de chaque utilisateur tiers :"
echo "  POLYMARKET_BUILDER_REMOTE_URL=$URL"
if [ "$JETON_ENGENDRE" = "1" ]; then
  echo "  POLYMARKET_BUILDER_REMOTE_TOKEN=$AUTH_TOKEN"
  echo
  echo "Ce jeton n'est affiché QU'ICI et n'est plus relisible côté Cloudflare."
  echo "Le noter maintenant. Le perdre oblige à redéployer avec un nouveau."
else
  echo "  POLYMARKET_BUILDER_REMOTE_TOKEN=<le jeton que tu as fourni>"
fi
