"""La page unique du terminal, servie telle quelle.

Aucune ressource externe : ni police, ni script, ni feuille de style distante.
Le poste est derrière un blocage FAI et la page doit s'afficher sans dépendre
de quoi que ce soit d'autre que le serveur local. Seul l'avatar vient du
serveur, sur `/avatar.png`.

Le langage visuel est celui d'un terminal de trading : fond quasi noir, tout en
chasse fixe, panneaux denses cernés d'un filet ambre, étiquettes minuscules en
capitales, et les nombres qui décident en très gros. C'est repris du terminal
montré en référence.

Une règle tenue partout, et c'est elle qui compte : **rien n'est affiché qui ne
soit mesuré**. Pas de PnL cumulé, pas de compteur de trades, pas de taux de
réussite — tant qu'aucun ordre n'a été posé, ces cases seraient du décor, et du
décor qui ressemble à un résultat est un mensonge. Les panneaux qui attendent
un moteur d'exécution le disent en toutes lettres.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DONMARKET</title>
<style>
  :root {
    --bg:#050607; --panel:#0a0c0d; --line:#2b2416;
    --edge:#4a3d1f; --text:#d6d3c4; --dim:#6b6656; --faint:#413d33;
    --pos:#2ee66b; --neg:#ff3b30; --amber:#ffb000; --cyan:#3fd0d6;
  }
  * { box-sizing:border-box; }
  html,body { background:var(--bg); }
  body {
    margin:0; padding:10px; color:var(--text);
    font:11px/1.45 ui-monospace,"Cascadia Mono",Consolas,"Courier New",monospace;
    letter-spacing:.02em;
  }
  .wrap { max-width:1500px; margin:0 auto; }

  /* ---- bandeau ---- */
  .top {
    display:flex; align-items:center; gap:12px; padding:6px 10px;
    border:1px solid var(--edge); background:linear-gradient(180deg,#12100a,#0a0c0d);
  }
  /* 34 px et un filtre désaturant rendaient la photo méconnaissable sur fond
     sombre — elle passait pour une icône cassée. */
  .ava {
    width:46px; height:46px; object-fit:cover; border:1px solid var(--gold);
    flex:none; border-radius:3px;
  }
  .brand { font-size:13px; font-weight:700; letter-spacing:.12em; white-space:nowrap; }
  .brand small { color:var(--dim); font-weight:400; letter-spacing:.08em; }
  .topstats { display:flex; gap:0; margin-left:auto; flex-wrap:wrap; }
  .ts { padding:0 12px; border-left:1px solid var(--line); text-align:right; }
  .ts b { display:block; font-size:13px; }
  .ts span { color:var(--dim); font-size:9px; letter-spacing:.14em; }
  .pill {
    padding:3px 9px; border:1px solid var(--edge); font-size:9px;
    letter-spacing:.14em; white-space:nowrap;
  }
  .pill.on { color:var(--pos); border-color:var(--pos); }
  .pill.off { color:var(--dim); }
  .pill.hot { color:var(--amber); border-color:var(--amber); }

  /* ---- bande défilante ---- */
  .tape {
    border:1px solid var(--edge); border-top:0; background:#080a0a;
    padding:4px 10px; overflow:hidden; white-space:nowrap; font-size:10px;
  }
  .tape span { margin-right:22px; }

  /* ---- grille ---- */
  .grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-top:8px; }
  .panel { border:1px solid var(--edge); background:var(--panel); padding:9px 11px 11px; }
  .panel.wide { grid-column:1 / -1; }
  .ph {
    display:flex; align-items:baseline; gap:8px; margin-bottom:9px;
    padding-bottom:5px; border-bottom:1px solid var(--line);
  }
  .ph h2 { margin:0; font-size:10px; letter-spacing:.16em; font-weight:600; color:var(--amber); }
  .ph em { font-style:normal; color:var(--dim); font-size:9px; letter-spacing:.1em; margin-left:auto; }

  .huge { font-size:40px; line-height:1.05; font-weight:700; letter-spacing:-.01em; }
  .sub { color:var(--dim); font-size:10px; margin-top:4px; }

  .kv { display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px dotted var(--faint); }
  .kv span { color:var(--dim); }
  .kv b { font-weight:600; }

  .bars { margin-top:2px; }
  .bar { display:flex; align-items:center; gap:8px; padding:2px 0; }
  .bar i { font-style:normal; color:var(--dim); width:92px; font-size:10px; }
  .bar u { text-decoration:none; flex:1; height:7px; background:#141310; display:block; }
  .bar u b { display:block; height:100%; background:var(--amber); }
  .bar em { font-style:normal; width:58px; text-align:right; font-size:10px; }

  /* ---- tableau ---- */
  .tablewrap { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; min-width:1000px; }
  th,td { text-align:right; padding:5px 8px; border-bottom:1px solid var(--faint); white-space:nowrap; }
  th { color:var(--dim); font-weight:500; font-size:9px; letter-spacing:.13em; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  td.q { max-width:400px; overflow:hidden; text-overflow:ellipsis; }
  tr.notheld td { opacity:.38; }
  tr:hover td { background:#0f1112; }
  .net { font-size:15px; font-weight:700; }
  .tag { font-size:8px; padding:1px 5px; border:1px solid var(--faint); color:var(--dim); margin-left:6px; letter-spacing:.1em; }
  .tag.held { color:var(--pos); border-color:var(--pos); }
  .basis { font-size:8px; letter-spacing:.12em; }

  .pos{color:var(--pos)} .neg{color:var(--neg)} .amber{color:var(--amber)}
  .dim{color:var(--dim)} .cyan{color:var(--cyan)}
  .empty { color:var(--dim); padding:16px 0; }

  .note { color:var(--dim); font-size:10px; line-height:1.6; }
  .note b { color:var(--amber); font-weight:600; }
  .void { color:var(--faint); font-size:10px; letter-spacing:.1em; padding:14px 0; text-align:center; }
  button {
    background:transparent; color:var(--amber); border:1px solid var(--amber);
    padding:5px 12px; font:inherit; font-size:10px; letter-spacing:.14em; cursor:pointer;
  }
  button:hover:not(:disabled) { background:var(--amber); color:#000; }
  button:disabled { opacity:.3; cursor:not-allowed; }
  input {
    background:#0d0f10; color:var(--text); border:1px solid var(--line);
    padding:5px 7px; font:inherit; width:84px; text-align:right;
  }
  a { color:var(--cyan); text-decoration:none; }
  a:hover { text-decoration:underline; }
  @media (max-width:1100px){ .grid{grid-template-columns:1fr;} }
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <img class="ava" src="/avatar.png" alt="">
    <div class="brand">DONMARKET</div>
    <span class="pill off" id="wallet">PORTEFEUILLE NON CONNECTÉ</span>
    <span class="pill off" id="engine">MOTEUR D'ORDRES ABSENT</span>
    <div class="topstats">
      <div class="ts"><b id="t-held">—</b><span>POSITIONS</span></div>
      <div class="ts"><b id="t-eng">—</b><span>ENGAGÉ</span></div>
      <div class="ts"><b id="t-mkt">—</b><span>MARCHÉS LUS</span></div>
      <div class="ts"><b id="t-clock">--:--:--</b><span>UTC</span></div>
    </div>
  </div>
  <div class="tape" id="tape"><span class="dim">EN ATTENTE DU PREMIER BALAYAGE…</span></div>

  <div class="grid">

    <div class="panel">
      <div class="ph"><h2>RÉSULTAT RÉEL</h2><em id="basis-note">—</em></div>
      <div class="huge" id="total">—</div>
      <div class="sub" id="totalsub">Aucun balayage terminé.</div>
      <div class="sub" id="projection" style="margin-top:6px"></div>
      <div class="bars" style="margin-top:12px">
        <div class="kv"><span>capital déclaré</span><b id="k-bank">—</b></div>
        <div class="kv"><span>engagé simultanément</span><b id="k-eng">—</b></div>
        <div class="kv"><span>positions tenables</span><b id="k-held">—</b></div>
        <div class="kv"><span>candidats retenus</span><b id="k-found">—</b></div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button id="go">LANCER UN BALAYAGE</button>
        <input id="bankroll" type="number" min="1" step="1" placeholder="capital">
        <span class="dim">$</span>
      </div>
    </div>

    <div class="panel">
      <div class="ph"><h2>ENTONNOIR</h2><em>DE L'UNIVERS AUX CANDIDATS</em></div>
      <div class="bars" id="funnel">
        <div class="void">L'ENTONNOIR DIT OÙ L'UNIVERS SE VIDE</div>
      </div>
      <div class="sub" id="scanline">—</div>
    </div>

    <div class="panel">
      <div class="ph"><h2>FLUX TEMPS RÉEL</h2><em id="feedpill">—</em></div>
      <div class="kv"><span>jetons suivis</span><b id="f-tok">—</b></div>
      <div class="kv"><span>carnets reçus</span><b id="f-books">—</b></div>
      <div class="kv"><span>mises à jour</span><b id="f-upd">—</b></div>
      <div class="kv"><span>âge du dernier message</span><b id="f-age">—</b></div>
      <div class="kv"><span>lignes sur moyenne fiable</span><b id="f-avg">—</b></div>
      <div class="note" style="margin-top:10px">
        La concurrence oscille du simple au triple en secondes. La colonne NET
        retient la <b>moyenne pondérée par le temps</b>, jamais l'instantané.
      </div>
    </div>

    <div class="panel wide">
      <div class="ph"><h2>CANDIDATS</h2><em id="sorted">TRIÉS PAR GAIN NET QUOTIDIEN</em></div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>MARCHÉ</th><th>NET %/J</th><th>INSTANT</th><th>SCAN</th>
            <th>$/JOUR</th><th>TICKET</th><th>BRUT %/J</th><th>DÉRIVE %</th>
            <th>POOL $/J</th><th>CONCURRENCE PTS</th><th>FIN</th>
          </tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
      <div class="empty" id="empty">Rien à afficher tant qu'aucun balayage n'a tourné.</div>
    </div>

    <div class="panel wide">
      <div class="ph"><h2>PLAN D'ORDRES</h2><em id="planhead">RIEN N'EST ENVOYÉ</em></div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>MARCHÉ</th><th>JETON</th><th>SENS</th><th>PRIX</th>
            <th>PARTS</th><th>COÛT</th><th>MOTIF DU PRIX</th>
          </tr></thead>
          <tbody id="orders"></tbody>
        </table>
      </div>
      <div class="void" id="noplan">
        AUCUN ORDRE N'A JAMAIS ÉTÉ POSÉ<br><br>
        PAS DE PNL · PAS DE TAUX DE RÉUSSITE · PAS DE COMPTEUR DE TRADES
      </div>
      <div class="note" id="plannote" style="margin-top:8px">
        Ce tableau montre ce que le bot poserait, prix par prix. Il ne pose
        rien : la signature des ordres n'existe pas, et <b>l'armement appartient
        au propriétaire du compte</b>. Tant qu'aucun ordre n'est parti, aucun
        PnL ne sera affiché ici — un chiffre sans ordre derrière n'est pas un
        résultat, c'est <b>du décor qui ressemble à un résultat</b>.
      </div>
    </div>

    <div class="panel">
      <div class="ph"><h2>REJETÉS DE PEU</h2><em>ET POURQUOI</em></div>
      <div id="near"><div class="void">—</div></div>
    </div>

    <div class="panel">
      <div class="ph"><h2>CONNEXION DU COMPTE</h2><em id="conn-note">NON CONNECTÉ</em></div>
      <div class="note">
        Les identifiants ne se saisissent <b>pas dans cette page</b> : un secret
        tapé dans un navigateur transite par la mémoire de l'onglet, l'historique
        et parfois le presse-papiers. Ils se mettent dans le fichier
        <b>.env</b> à la racine du projet, que git ignore.<br><br>
        <code style="color:var(--gold);font-size:11px;line-height:1.9">
        POLYMARKET_ADDRESS=0x…<br>
        POLYMARKET_PRIVATE_KEY=…<br>
        POLYMARKET_API_KEY=…<br>
        POLYMARKET_API_SECRET=…<br>
        POLYMARKET_API_PASSPHRASE=…
        </code><br><br>
        La clé privée est celle du portefeuille Polygon qui détient l'USDC. Les
        trois identifiants API se dérivent d'elle et se régénèrent ; c'est la
        clé privée qui est irremplaçable.<br><br>
        <b>Redémarrer le serveur après modification</b> — le .env est lu au
        démarrage. Le voyant ci-dessus passera au vert.<br><br>
        <b>Une clé présente n'arme rien.</b> Aucun ordre ne peut partir tant que
        le moteur d'exécution n'est pas écrit et explicitement armé.
      </div>
      <div class="bars" style="margin-top:10px">
        <div class="kv"><span>adresse</span><b id="c-addr">—</b></div>
        <div class="kv"><span>clé privée</span><b id="c-key">—</b></div>
        <div class="kv"><span>identifiants API</span><b id="c-api">—</b></div>
        <div class="kv"><span>exécution</span><b id="c-exec">—</b></div>
      </div>
    </div>

    <div class="panel">
      <div class="ph"><h2>À LIRE AVANT D'Y CROIRE</h2><em>LIMITES ASSUMÉES</em></div>
      <div class="note">
        <b>Le net n'est pas un plancher.</b> Le risque retenu est le pire de
        deux mesures sur 24 h — la dérive bout-à-bout et le rejeu de la
        cotation — parce qu'aucune ne majore l'autre : la dérive rate les
        allers-retours, le rejeu rate les tendances lentes. Mesuré le
        01/08/2026, l'ancien majorant cédait sur 6 marchés cotés sur 17, avec
        jusqu'à 31 points d'écart par jour.<br><br>
        <b>Part au prorata linéaire.</b> Polymarket pondère par la proximité au
        milieu du carnet : la part réelle sera différente.<br><br>
        <b>Risque de saut non modélisé.</b> 24 h d'historique ne disent rien
        d'un marché qui se résout sur une publication ponctuelle.<br><br>
        <b>Une moyenne sur 45 s reste une moyenne sur 45 s.</b>
      </div>
    </div>

  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const nf = (v,d=2) => (v===null||v===undefined) ? "—" : v.toFixed(d);
const ni = (v) => (v===null||v===undefined) ? "—" : v.toLocaleString("fr-FR");
const sign = (v) => (v>=0 ? "pos" : "neg");
// Passe à vrai dès que l'utilisateur écrit dans la case capital. Tant qu'elle
// est fausse, la case suit le serveur ; ensuite elle ne bouge plus, sinon un
// balayage qui se termine effacerait le chiffre en cours de saisie.
let bankTouched = false;
const esc = (s) => String(s).replace(/[&<>"']/g,(c)=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// La part du pool qui nous revient, en pourcent. Un score de concurrence isolé
// n'a pas d'échelle absolue : 400 pts sur un carnet désert et 400 pts sur un
// carnet dense veulent dire des choses opposées. C'est le RAPPORT qui informe.
const share = (c) => {
  const own = c.own_q, comp = c.competing_q;
  if (!own || own <= 0) return 0;
  return 100 * own / (comp + own);
};

setInterval(() => {
  $("t-clock").textContent = new Date().toISOString().slice(11,19);
}, 1000);

function funnel(f, bankroll) {
  const steps = [
    [f.markets_seen,"MARCHÉS LUS"],
    [f.rewarded,"RÉCOMPENSÉS"],
    [f.alive,"> 24 H"],
    [f.affordable,"FINANÇABLES"],
  ];
  const top = Math.max(1, f.markets_seen);
  return steps.map(([n,label]) =>
    `<div class="bar"><i>${label}</i><u><b style="width:${(n/top*100).toFixed(1)}%"></b></u>`
    + `<em>${ni(n)}</em></div>`).join("");
}

function row(c) {
  const cls = c.held ? "" : "notheld";
  const tag = c.held ? '<span class="tag held">TENUE</span>'
                     : '<span class="tag">HORS BUDGET</span>';
  const basis = c.averaged
    ? `<span class="basis pos">MOY ${nf(c.average_seconds,0)}S</span>`
    : (c.live ? '<span class="basis amber">INSTANT</span>'
              : '<span class="basis dim">SCAN</span>');
  const hours = c.hours_left===null ? "—" : nf(c.hours_left,0)+" H";
  const q = esc(c.question);
  const link = c.slug
    ? `<a href="https://polymarket.com/event/${encodeURIComponent(c.slug)}" target="_blank" rel="noopener noreferrer">${q}</a>`
    : q;
  return `<tr class="${cls}">
    <td class="q" title="${q}">${link}${tag} ${basis}</td>
    <td class="net ${sign(c.net_yield)}">${c.net_yield>=0?"+":""}${nf(c.net_yield)}</td>
    <td class="${sign(c.instant_net_yield)}">${nf(c.instant_net_yield)}</td>
    <td class="dim">${nf(c.scan_net_yield)}</td>
    <td class="${sign(c.daily_usd)}">${c.daily_usd>=0?"+":""}${nf(c.daily_usd)}</td>
    <td>${nf(c.engaged_usd,0)} $</td>
    <td>${nf(c.gross_yield)}</td>
    <td>${nf(c.drift)}</td>
    <td>${ni(Math.round(c.daily_pool))} $</td>
    <td title="notre score ${nf(c.own_q,1)} pts — part ${nf(share(c),1)} % du pool">${ni(Math.round(c.competing_q))}</td>
    <td class="dim">${hours}</td>
  </tr>`;
}

function connection(c) {
  const w = $("wallet"), e = $("engine");
  if (c && c.connected) {
    w.className = "pill on";
    w.textContent = "PORTEFEUILLE " + (c.address || "CONNECTÉ");
  } else {
    w.className = "pill off";
    w.textContent = "PORTEFEUILLE NON CONNECTÉ";
    w.title = c && c.missing.length ? "manque : " + c.missing.join(", ") : "";
  }
  // Cette pastille ne passe au vert que le jour où un ordre peut réellement
  // partir. Elle ne doit jamais mentir dans le sens rassurant.
  e.className = "pill off";
  e.textContent = (c && c.can_execute) ? "MOTEUR ARMÉ" : "MOTEUR D'ORDRES ABSENT";

  // Le détail, pour que « non connecté » dise CE QUI manque : sans ça, le
  // seul recours est de relire le code pour deviner le nom des variables.
  const ok  = t => `<span class="pos">${t}</span>`;
  const nok = t => `<span class="neg">${t}</span>`;
  $("c-addr").innerHTML = c && c.address ? ok(c.address) : nok("absente");
  $("c-key").innerHTML  = c && c.connected ? ok("présente") : nok("absente");
  $("c-api").innerHTML  = c && c.has_api_credentials ? ok("présents") : nok("absents");
  $("c-exec").innerHTML = c && c.can_execute ? ok("armée") : nok("non armée");
  $("conn-note").innerHTML = c && c.connected
    ? '<span class="pos">CLÉ PRÉSENTE</span>'
    : '<span class="dim">NON CONNECTÉ</span>';
}

function orderRow(o) {
  return `<tr>
    <td class="q" title="${esc(o.question)}">${esc(o.question)}</td>
    <td class="dim">${esc(o.token_id).slice(0,10)}…</td>
    <td class="cyan">${esc(o.side)}</td>
    <td class="amber">${nf(o.price,3)}</td>
    <td>${nf(o.size,0)}</td>
    <td>${nf(o.notional,2)} $</td>
    <td class="dim" style="text-align:left">${esc(o.reason)}</td>
  </tr>`;
}

function render(s) {
  const scan = s.scan, L = s.live || {}, running = s.status === "running";
  $("go").disabled = running;
  connection(s.connection);

  // --- flux ---
  const age = L.age_seconds;
  let pill = '<span class="dim">HORS SERVICE</span>';
  if (L.tokens && age !== null && age !== undefined) {
    pill = age < 30 ? '<span class="pos">● ACTIF</span>'
                    : `<span class="amber">● MUET ${nf(age,0)} S</span>`;
  } else if (L.tokens) { pill = '<span class="amber">● CONNEXION…</span>'; }
  $("feedpill").innerHTML = pill;
  $("f-tok").textContent = ni(L.tokens || 0);
  $("f-books").textContent = ni(L.books || 0);
  $("f-upd").textContent = ni(L.updates || 0);
  $("f-age").textContent = (age===null||age===undefined) ? "—" : nf(age,1)+" s";

  if (!scan) {
    $("scanline").textContent = running
      ? "Balayage en cours… ~2 min (2 100 marchés, ~950 carnets, 60 historiques)."
      : (s.status === "error" ? "Échec : " + s.error : "Aucun balayage lancé.");
    return;
  }

  // --- entêtes ---
  const p = scan.portfolio;
  $("t-held").textContent = p.held_count;
  $("t-eng").textContent = nf(p.engaged_usd,0)+" $";
  $("t-mkt").textContent = ni(scan.funnel.markets_seen);
  $("f-avg").textContent = `${scan.averaged_count} / ${scan.found}`;

  // Demandé explicitement par le propriétaire du compte : le grand chiffre
  // affiche le CAPITAL DÉMO, pas le résultat encaissé. Tant que le moteur n'est
  // pas armé, ce nombre ne bouge donc jamais — il ne mesure rien, il rappelle
  // la mise. Dès qu'un ordre part, il devient capital + encaissé, sans quoi
  // l'écran cacherait le seul chiffre qui compte à ce moment-là.
  const executed = scan.executed_pnl_usd;
  $("total").innerHTML = `$${nf(scan.bankroll + (executed || 0), 2)}`;
  $("totalsub").textContent = executed == null
    ? "aucun ordre passé — le moteur d'exécution n'est pas armé"
    : "encaissé depuis le début de la session";
  $("projection").innerHTML =
    `<span class="dim">projection non mesurée&nbsp;:</span> ` +
    `<span class="${sign(p.daily_usd)}">${p.daily_usd>=0?"+":""}$${nf(p.daily_usd,2)}</span>` +
    `<span class="dim">/jour si les rendements estimés tenaient 24&nbsp;h</span>`;
  $("basis-note").innerHTML = scan.averaged_count === scan.found && scan.found > 0
    ? '<span class="pos">MOYENNE TEMPORELLE</span>'
    : (scan.live_count ? '<span class="amber">INSTANTANÉ</span>'
                       : '<span class="dim">BALAYAGE</span>');

  // La case de saisie reflète le capital du serveur tant que personne n'y a
  // touché. Une valeur écrite en dur dans la page annulait en silence le
  // `--bankroll` de la ligne de commande au premier clic sur « LANCER ».
  if (!bankTouched) $("bankroll").value = scan.bankroll;

  $("k-bank").textContent = nf(scan.bankroll,2)+" $";
  $("k-eng").textContent = nf(p.engaged_usd,2)+" $";
  $("k-held").textContent = `${p.held_count} / ${scan.found}`;
  $("k-found").textContent = scan.found;

  $("funnel").innerHTML = funnel(scan.funnel, scan.bankroll);
  $("scanline").textContent =
    `${scan.found} retenu(s) en ${nf(scan.duration_seconds,1)} s · mode ${scan.mode} · `
    + `${ni(scan.books_fetched)} carnets · ${scan.histories_fetched} historiques`;

  // --- bande ---
  $("tape").innerHTML = scan.candidates.length
    ? scan.candidates.map(c =>
        `<span>${esc(c.question).slice(0,34)} <b class="${sign(c.net_yield)}">`
        + `${c.net_yield>=0?"+":""}${nf(c.net_yield)}%/J</b></span>`).join("")
    : '<span class="dim">AUCUN CANDIDAT NE FRANCHIT LES SEUILS</span>';

  // --- tableau ---
  if (scan.candidates.length) {
    $("rows").innerHTML = scan.candidates.map(row).join("");
    $("empty").hidden = true;
  } else {
    $("rows").innerHTML = "";
    $("empty").hidden = false;
    $("empty").textContent =
      "Aucun marché récompensé ne franchit les seuils. Ce n'est pas une panne : "
      + "c'est le résultat de la mesure. L'entonnoir dit où l'univers s'est vidé.";
  }

  // --- plan d'ordres ---
  const plan = scan.plan || { orders: [], skipped: [] };
  if (plan.orders.length) {
    $("orders").innerHTML = plan.orders.map(orderRow).join("");
    $("noplan").hidden = true;
    $("planhead").innerHTML =
      `<span class="amber">${plan.orders.length} ORDRES · ${plan.markets} MARCHÉS · `
      + `${nf(plan.notional,2)} $</span>`
      + (plan.fits ? '' : ' <span class="neg">DÉPASSE LE CAPITAL</span>')
      + ' <span class="dim">· RIEN N\\'EST ENVOYÉ</span>';
  } else {
    $("orders").innerHTML = "";
    $("noplan").hidden = false;
    $("planhead").innerHTML = '<span class="dim">RIEN N\\'EST ENVOYÉ</span>';
  }

  $("near").innerHTML = scan.near_misses.length
    ? scan.near_misses.map(c =>
        `<div class="kv"><span>${esc(c.question).slice(0,40)}</span>`
        + `<b class="${sign(c.net_yield)}">${nf(c.net_yield)}</b></div>`
        + `<div class="note" style="margin:-2px 0 6px">${esc(c.rejected_by.join(" · "))}</div>`
      ).join("")
    : '<div class="void">—</div>';
}

async function poll() {
  try { render(await (await fetch("/api/state")).json()); }
  catch (e) { $("scanline").textContent = "Serveur local injoignable."; }
}

$("bankroll").addEventListener("input", () => { bankTouched = true; });

$("go").addEventListener("click", async () => {
  $("go").disabled = true;
  await fetch("/api/scan", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ bankroll: parseFloat($("bankroll").value) }),
  });
  poll();
});

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""
