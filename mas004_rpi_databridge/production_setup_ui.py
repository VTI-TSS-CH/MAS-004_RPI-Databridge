def build_production_setup_ui_html(nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MAS-004 Produktion</title>
  <style>
    :root{{
      --bg:#f5f7fb; --card:#fff; --ink:#17202a; --muted:#607086; --line:#d9e1ec;
      --blue:#005eb8; --green:#237a44; --yellow:#9b6700; --red:#b42318;
      --soft-blue:#e8f1fb; --soft-green:#e4f6e9; --soft-yellow:#fff3cf; --soft-red:#fde7e7;
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
    .wrap{{max-width:1760px;margin:0 auto;padding:16px}}
    .hero{{display:grid;grid-template-columns:1fr 360px;gap:14px;margin-bottom:14px}}
    .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
    .title{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:10px}}
    h1,h2,h3{{margin:0}} h1{{font-size:26px}} h2{{font-size:18px}} h3{{font-size:15px}}
    .muted{{color:var(--muted)}} .small{{font-size:12px}} .mono{{font-family:Consolas,Menlo,monospace}}
    .pill{{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;background:#eef3f8;padding:5px 9px;font-size:12px;font-weight:700}}
    .ok{{background:var(--soft-green);color:var(--green);border-color:#a9dfb8}}
    .warn{{background:var(--soft-yellow);color:var(--yellow);border-color:#e3c66c}}
    .bad{{background:var(--soft-red);color:var(--red);border-color:#efaaa4}}
    .toolbar,.btnrow{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
    button,.btn{{min-height:38px;border:1px solid #abc3dc;border-radius:10px;background:#e8f0f8;color:#17324b;padding:8px 12px;font-weight:700;cursor:pointer;text-decoration:none}}
    button:hover,.btn:hover{{filter:brightness(.98)}} button:disabled{{cursor:not-allowed;opacity:.45;filter:grayscale(.4)}}
    .primary{{background:#d9ebff;border-color:#8dbce8;color:#08345f}} .danger{{background:var(--soft-red);border-color:#efaaa4;color:var(--red)}}
    input,select,textarea{{min-height:38px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:#fff;width:100%}}
    textarea{{min-height:78px;resize:vertical}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}}
    .kv{{display:grid;grid-template-columns:150px 1fr;gap:7px 12px;align-items:start}}
    .param-table{{width:100%;border-collapse:collapse}}
    th,td{{padding:8px;border-bottom:1px solid #e7edf6;text-align:left;vertical-align:top;font-size:13px}}
    th{{color:#425466;background:#f7fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;z-index:1}}
    .param-scroll{{max-height:68vh;overflow:auto;border:1px solid var(--line);border-radius:14px;background:#fbfdff}}
    .row-muted{{color:var(--muted)}}
    .status-box{{white-space:pre-wrap;word-break:break-word;border:1px solid var(--line);border-radius:12px;background:#fbfdff;padding:10px;min-height:80px;max-height:280px;overflow:auto}}
    .profiles{{display:flex;flex-direction:column;gap:8px;max-height:330px;overflow:auto}}
    .profile{{border:1px solid var(--line);border-radius:12px;padding:9px;background:#fbfdff;cursor:pointer}}
    .profile:hover{{border-color:#8dbce8;background:#f2f8ff}}
    .profile.active{{border-color:#005eb8;background:#e8f1fb}}
    .loglist{{display:flex;flex-direction:column;gap:8px;max-height:220px;overflow:auto}}
    .logitem{{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;border:1px solid var(--line);border-radius:12px;background:#fbfdff;padding:8px}}
    @media(max-width:1100px){{.hero{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<div class="wrap">
  {nav_html}
  <div class="hero">
    <section class="card">
      <div class="title">
        <div>
          <h1>Produktion</h1>
          <div class="muted">Formatprofile verwalten, Formatrelevante Parameter setzen und produktionsrelevante Werte beobachten.</div>
        </div>
        <span id="state_pill" class="pill">lade...</span>
      </div>
      <div class="grid" id="prod_cards"></div>
    </section>
    <aside class="card">
      <h2>Formatprofile</h2>
      <div class="muted small">Profile werden lokal auf dem Raspi gespeichert und koennen spaeter an die Maschine gesendet werden.</div>
      <div style="margin-top:10px"><input id="profile_name" placeholder="Formatname"/></div>
      <div style="margin-top:8px"><textarea id="profile_note" placeholder="Notiz optional"></textarea></div>
      <div class="toolbar" style="margin-top:10px">
        <button class="primary" onclick="saveProfile()">Speichern</button>
        <button onclick="loadCurrentValues()">Istwerte laden</button>
        <button class="danger" onclick="deleteProfile()">Loeschen</button>
      </div>
      <div id="profile_list" class="profiles" style="margin-top:12px"></div>
    </aside>
  </div>

  <section class="card" style="margin-bottom:14px">
    <div class="title">
      <div>
        <h2>Microtom-Simulation Neue Produktion</h2>
        <div class="muted small">Bereitet eine neue Produktion wie ein Microtom-Auftrag vor: Produktionsname senden, optional Formatwerte senden, lokale/ESP-Produktionsreste loeschen.</div>
      </div>
      <div class="toolbar">
        <button onclick="loadProductionLogs()">Logfiles laden</button>
        <button onclick="ackProductionLogs()">Logfiles quittieren</button>
      </div>
    </div>
    <div class="grid">
      <div>
        <label class="small muted" for="new_production_name">Produktionsname / Auftrag</label>
        <input id="new_production_name" placeholder="z.B. 001000544055"/>
      </div>
      <div>
        <label class="small muted" for="new_production_mode">Aktion</label>
        <select id="new_production_mode">
          <option value="format">Name + aktuelle Formatwerte senden, Register resetten</option>
          <option value="name">Nur Name senden und Register resetten</option>
        </select>
      </div>
      <div style="display:flex;align-items:end">
        <button class="primary" onclick="prepareNewProduction()">Neue Produktion vorbereiten</button>
      </div>
    </div>
    <div class="grid" style="margin-top:12px">
      <div>
        <h3>Bereite Production-Logfiles</h3>
        <div id="production_logfiles" class="loglist" style="margin-top:8px"><div class="muted small">Noch nicht geladen.</div></div>
      </div>
      <div>
        <h3>Microtom-Ablauf</h3>
        <div class="status-box small" style="min-height:160px">1. Logfiles laden/herunterladen/quittieren
2. Produktionsname setzen
3. Formatrelevante Parameter senden
4. Raspi-/ESP-Schieberegister fuer neue Produktion leeren
5. Danach Einrichten und Produktion wie gewohnt starten</div>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="title">
      <div>
        <h2>Formatrelevante Parameter</h2>
        <div class="muted small">Senden verwendet denselben Raspi-Router wie Microtom/Testtool. Dadurch gelten dieselben Rechte, Mappings und ACK/NAK-Antworten.</div>
      </div>
      <div class="toolbar">
        <input id="filter" placeholder="Filter: MAP0014, Laenge, TTO..." style="width:260px" oninput="renderParams()"/>
        <button onclick="selectWritableOnly()">Nur schreibbare Werte behalten</button>
        <button class="primary" onclick="sendFormat()">Format an Maschine senden</button>
      </div>
    </div>
    <div class="param-scroll">
      <table class="param-table">
        <thead><tr><th>Code</th><th>Name / Beschreibung</th><th>Aktuell</th><th>Formatwert</th><th>Einheit</th><th>Rechte</th></tr></thead>
        <tbody id="param_rows"></tbody>
      </table>
    </div>
  </section>

  <section class="card" style="margin-top:14px">
    <h2>Rueckmeldungen</h2>
    <div id="send_status" class="status-box">Bereit.</div>
  </section>
</div>
<script>
let params = [];
let values = {{}};
let profiles = [];
let selectedProfile = "";
function esc(v){{return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}}
async function api(path,opt={{}}){{
  opt.headers = opt.headers || {{}};
  const r = await fetch(path,opt);
  const txt = await r.text();
  let j = null; try{{j=JSON.parse(txt);}}catch(e){{}}
  if(!r.ok){{throw new Error((j&&j.detail)?j.detail:(`HTTP ${{r.status}} ${{txt}}`));}}
  return j;
}}
function valueFor(p){{ return values[p.pkey] ?? p.value ?? p.default_v ?? ""; }}
function collectValues(){{
  const out = {{}};
  for(const p of params){{
    const el = document.getElementById(`v_${{p.pkey}}`);
    if(el) out[p.pkey] = el.value;
  }}
  values = out;
  return out;
}}
function canWrite(p){{ return ["W","R/W"].includes(String(p.rw || "").toUpperCase()); }}
function collectWritableValues(){{
  const all = collectValues();
  const writable = new Set(params.filter(canWrite).map(p => p.pkey));
  return Object.fromEntries(Object.entries(all).filter(([k]) => writable.has(k)));
}}
function renderParams(){{
  const f = String(document.getElementById("filter").value || "").toLowerCase();
  const rows = params.filter(p => !f || JSON.stringify(p).toLowerCase().includes(f)).map(p => {{
    const write = canWrite(p);
    const range = [p.min_v,p.max_v].filter(x => x !== null && x !== undefined && x !== "").join(" .. ");
    return `<tr class="${{write ? "" : "row-muted"}}">
      <td><span class="pill">${{esc(p.pkey)}}</span></td>
      <td><b>${{esc(p.name || "-")}}</b><div class="small muted">${{esc(p.message || "")}}</div>${{range ? `<div class="small muted">Range: ${{esc(range)}}</div>` : ""}}</td>
      <td class="mono">${{esc(p.value ?? "")}}</td>
      <td><input id="v_${{esc(p.pkey)}}" value="${{esc(valueFor(p))}}" ${{write ? "" : "title='Microtom read-only: Senden fuehrt zu NAK_ReadOnly'"}}/></td>
      <td>${{esc(p.unit || "")}}</td>
      <td><span class="pill ${{write ? "ok" : "warn"}}">Microtom ${{esc(p.rw || "-")}}</span><br/><span class="small muted">ESP ${{esc(p.esp_rw || "-")}}</span></td>
    </tr>`;
  }});
  document.getElementById("param_rows").innerHTML = rows.join("") || '<tr><td colspan="6" class="muted">Keine Parameter gefunden.</td></tr>';
}}
function renderProfiles(){{
  document.getElementById("profile_list").innerHTML = profiles.map(p => `
    <div class="profile ${{p.name === selectedProfile ? "active" : ""}}" onclick="loadProfile(decodeURIComponent('${{encodeURIComponent(p.name)}}'))">
      <b>${{esc(p.name)}}</b>
      <div class="small muted">${{esc(p.param_count)}} Parameter · ${{new Date((p.updated_ts||0)*1000).toLocaleString()}}</div>
      ${{p.note ? `<div class="small">${{esc(p.note)}}</div>` : ""}}
    </div>`).join("") || '<div class="muted small">Noch keine Profile gespeichert.</div>';
}}
function renderProductionStatus(data){{
  const m = data.machine || {{}};
  const status = data.production_status || [];
  document.getElementById("state_pill").className = m.purge_active ? "pill bad" : (m.warning_active ? "pill warn" : "pill ok");
  document.getElementById("state_pill").textContent = `${{m.current_state ?? "-"}} - ${{m.current_state_label || "Status"}}`;
  document.getElementById("prod_cards").innerHTML = status.map(it => `
    <div class="card" style="box-shadow:none">
      <div class="small muted">${{esc(it.pkey)}}</div>
      <h3>${{esc(it.value ?? "-")}}</h3>
      <div class="small">${{esc(it.name || "")}}</div>
    </div>`).join("");
  const nameEl = document.getElementById("new_production_name");
  const mas0029 = status.find(it => it.pkey === "MAS0029");
  if(nameEl && !nameEl.value && mas0029 && mas0029.value) nameEl.value = mas0029.value;
}}
async function loadCurrentValues(){{
  const j = await api("/api/production-setup/parameters");
  params = j.parameters || [];
  values = Object.fromEntries(params.map(p => [p.pkey, p.value ?? p.default_v ?? ""]));
  renderProductionStatus(j);
  renderParams();
}}
async function refreshProductionStatus(){{
  const j = await api("/api/production-setup/status");
  renderProductionStatus(j);
}}
async function loadProfiles(){{
  const j = await api("/api/production-setup/profiles");
  profiles = j.profiles || [];
  renderProfiles();
}}
async function loadProfile(name){{
  const j = await api(`/api/production-setup/profiles/${{encodeURIComponent(name)}}`);
  selectedProfile = j.profile.name;
  document.getElementById("profile_name").value = j.profile.name;
  document.getElementById("profile_note").value = j.profile.note || "";
  values = j.profile.values || {{}};
  renderProfiles();
  renderParams();
  document.getElementById("send_status").textContent = `Format geladen: ${{j.profile.name}}`;
}}
async function saveProfile(){{
  collectValues();
  const name = document.getElementById("profile_name").value;
  const note = document.getElementById("profile_note").value;
  const j = await api("/api/production-setup/profiles", {{
    method:"POST", headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{name, note, values}})
  }});
  selectedProfile = j.profile.name;
  document.getElementById("send_status").textContent = `Format gespeichert: ${{j.profile.name}}`;
  await loadProfiles();
}}
async function deleteProfile(){{
  const name = document.getElementById("profile_name").value || selectedProfile;
  if(!name) return;
  if(!confirm(`Format '${{name}}' wirklich loeschen?`)) return;
  await api(`/api/production-setup/profiles/${{encodeURIComponent(name)}}`, {{method:"DELETE"}});
  selectedProfile = "";
  document.getElementById("profile_name").value = "";
  document.getElementById("profile_note").value = "";
  document.getElementById("send_status").textContent = `Format geloescht: ${{name}}`;
  await loadProfiles();
}}
function selectWritableOnly(){{
  collectValues();
  for(const p of params) if(!canWrite(p)) delete values[p.pkey];
  renderParams();
}}
async function sendFormat(){{
  const name = document.getElementById("profile_name").value || selectedProfile || "unsaved-format";
  const sendValues = collectWritableValues();
  document.getElementById("send_status").textContent = "Sende Format...";
  const j = await api("/api/production-setup/send", {{
    method:"POST", headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{name, values: sendValues}})
  }});
  const lines = (j.results || []).map(r => `${{r.skipped ? "SKIP" : (r.ok ? "OK " : "NAK")}} ${{r.line}} -> ${{r.response || r.error || ""}}`);
  document.getElementById("send_status").textContent = lines.join("\\n") || "Keine Werte gesendet.";
  await loadCurrentValues();
}}
function renderProductionLogs(j){{
  const box = document.getElementById("production_logfiles");
  const items = j.items || j.files || [];
  if(!items.length){{
    box.innerHTML = '<div class="muted small">Keine bereiten Production-Logfiles.</div>';
    return;
  }}
  box.innerHTML = items.map(it => {{
    const name = it.name || it;
    const size = it.size_bytes ? `${{Math.round(it.size_bytes/1024)}} kB` : "";
    return `<div class="logitem">
      <div><b>${{esc(name)}}</b><div class="small muted">${{esc(size)}}</div></div>
      <a class="btn" href="/api/production-setup/logfiles/download?name=${{encodeURIComponent(name)}}">Download</a>
    </div>`;
  }}).join("");
}}
async function loadProductionLogs(){{
  const j = await api("/api/production-setup/logfiles");
  renderProductionLogs(j);
  document.getElementById("send_status").textContent = "Production-Logfiles geladen.";
}}
async function ackProductionLogs(){{
  const j = await api("/api/production-setup/logfiles/ack", {{method:"POST"}});
  document.getElementById("send_status").textContent = `Production-Logfiles quittiert: ${{JSON.stringify(j)}}`;
  await loadProductionLogs();
}}
async function prepareNewProduction(){{
  const name = document.getElementById("new_production_name").value.trim();
  if(!name){{ document.getElementById("send_status").textContent = "Produktionsname fehlt."; return; }}
  if(!confirm(`Neue Produktion '${{name}}' vorbereiten und alte Registerreste loeschen?`)) return;
  const send_format = document.getElementById("new_production_mode").value === "format";
  const payload = {{
    name,
    send_format,
    clear_previous:true,
    values: send_format ? collectWritableValues() : {{}}
  }};
  document.getElementById("send_status").textContent = "Bereite neue Produktion vor...";
  const j = await api("/api/production-setup/new-production", {{
    method:"POST", headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify(payload)
  }});
  const formatLines = ((j.format || {{}}).results || []).map(r => `${{r.skipped ? "SKIP" : (r.ok ? "OK " : "NAK")}} ${{r.line}} -> ${{r.response || r.error || ""}}`);
  const reset = j.reset || {{}};
  const espLines = (reset.esp_commands || []).map(r => `${{r.ok ? "OK " : "ERR"}} ${{r.command}} -> ${{r.response || r.error || r.skipped || ""}}`);
  document.getElementById("send_status").textContent = [
    `Neue Produktion vorbereitet: ${{j.production_label}}`,
    `Register geloescht: ${{reset.deleted_label_register || 0}}, Events geloescht: ${{reset.deleted_label_events || 0}}`,
    "Format:",
    ...(formatLines.length ? formatLines : ["SKIP"]),
    "ESP:",
    ...(espLines.length ? espLines : ["SKIP"])
  ].join("\\n");
  await loadCurrentValues();
  await loadProductionLogs();
}}
async function init(){{
  try{{ await loadCurrentValues(); await loadProfiles(); await loadProductionLogs(); }}
  catch(err){{ document.getElementById("send_status").textContent = err.message; }}
}}
init();
setInterval(()=>{{ if(!document.hidden) refreshProductionStatus().catch(()=>{{}}); }}, 1500);
</script>
</body>
</html>
"""
