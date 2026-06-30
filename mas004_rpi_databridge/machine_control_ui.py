def build_machine_control_ui_html(nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MAS-004 Machine Control</title>
  <style>
    :root{{
      --bg:#f4f6f9; --card:#fff; --ink:#17202a; --muted:#607086; --line:#d9e1ec;
      --blue:#005eb8; --green:#237a44; --yellow:#9b6700; --red:#b42318; --cyan:#087f8c;
      --soft-blue:#e8f1fb; --soft-green:#e4f6e9; --soft-yellow:#fff3cf; --soft-red:#fde7e7;
    }}
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
    .wrap{{max-width:1760px;margin:0 auto;padding:16px}}
    .topnav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
    .navbtn{{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);text-decoration:none;font-weight:700}}
    .navbtn.active{{background:var(--blue);color:#fff;border-color:var(--blue)}}
    .hero{{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;margin-bottom:14px}}
    .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
    .title{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:10px}}
    h1,h2,h3{{margin:0}} h1{{font-size:26px}} h2{{font-size:18px}} h3{{font-size:15px}}
    .muted{{color:var(--muted)}} .small{{font-size:12px}} .mono{{font-family:Consolas,Menlo,monospace}}
    .pill{{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;background:#eef3f8;padding:5px 9px;font-size:12px;font-weight:700}}
    .pill.ok{{background:var(--soft-green);color:var(--green);border-color:#a9dfb8}}
    .pill.warn{{background:var(--soft-yellow);color:var(--yellow);border-color:#e3c66c}}
    .pill.bad{{background:var(--soft-red);color:var(--red);border-color:#efaaa4}}
    .toolbar,.btnrow{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
    button,.btn{{min-height:38px;border:1px solid #abc3dc;border-radius:10px;background:#e8f0f8;color:#17324b;padding:8px 12px;font-weight:700;cursor:pointer;text-decoration:none}}
    button:hover,.btn:hover{{filter:brightness(.98)}} button:disabled{{cursor:not-allowed;opacity:.45;filter:grayscale(.4)}}
    .primary{{background:#d9ebff;border-color:#8dbce8;color:#08345f}} .danger{{background:var(--soft-red);border-color:#efaaa4;color:var(--red)}}
    .control{{min-height:54px;font-size:15px;border-width:2px}}
    .control.enabled{{background:var(--soft-green);border-color:#78c48e;color:#14542d}}
    .control.stop.enabled{{background:var(--soft-yellow);border-color:#e3c66c;color:#6d4800}}
    .control.reset.enabled{{background:var(--soft-red);border-color:#efaaa4;color:#8a1c15}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
    .kv{{display:grid;grid-template-columns:150px 1fr;gap:7px 12px;align-items:start}}
    .kv>div{{min-width:0}}
    .code-wrap{{white-space:normal;overflow-wrap:anywhere;word-break:break-word}}
    .reason-list{{display:flex;gap:6px;flex-wrap:wrap;align-items:flex-start}}
    .reason-chip{{display:inline-flex;flex-direction:column;gap:2px;max-width:100%;border:1px solid #efaaa4;border-radius:10px;background:var(--soft-red);color:#711912;padding:6px 8px;font-size:12px;font-weight:700;line-height:1.15}}
    .reason-chip .code{{font-family:Consolas,Menlo,monospace;font-size:11px;color:#9b1c14}}
    .reason-empty{{color:var(--muted)}}
    .audit-shell{{display:grid;grid-template-columns:330px 1fr;gap:14px;margin-top:14px}}
    .bypass-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}}
    .bypass-card{{border:1px solid var(--line);border-radius:12px;background:#fbfdff;padding:12px}}
    .bypass-card.active{{background:#fff9e6;border-color:#e3c66c}}
    .bypass-card h3{{display:flex;justify-content:space-between;gap:8px;align-items:center}}
    .bypass-fields{{display:grid;grid-template-columns:1fr 120px;gap:8px;align-items:center;margin-top:10px}}
    .audit-view{{height:64vh;min-height:520px;overflow:auto;border:1px solid var(--line);border-radius:14px;background:#fbfdff}}
    .entry{{display:grid;grid-template-columns:168px 112px 1fr;gap:10px;padding:10px 12px;border-bottom:1px solid #e7edf6;align-items:start}}
    .entry:last-child{{border-bottom:none}}
    .entry.communication{{background:#fff}} .entry.machine{{background:#f7fbff}} .entry.label{{background:#fbfff8}}
    .entry.level-warning{{background:var(--soft-yellow)!important;border-left:4px solid #e3c66c}}
    .entry.level-error{{background:var(--soft-red)!important;border-left:4px solid #efaaa4}}
    .summary{{font-weight:700}} .raw{{margin-top:4px;color:#4b5b6f;white-space:pre-wrap;word-break:break-word;font-size:12px}}
    input,select{{min-height:38px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:#fff}}
    label.check{{display:inline-flex;gap:7px;align-items:center}}
    .audit-filters{{margin:8px 0 12px;padding:8px;border:1px solid var(--line);border-radius:12px;background:#f8fbff}}
    .filter-chip{{display:inline-flex;gap:6px;align-items:center;border:1px solid var(--line);border-radius:999px;background:#fff;padding:6px 9px;font-size:12px;font-weight:700;color:#31445a}}
    .filter-chip input{{margin:0}}
    #audit_search{{min-width:220px;flex:1 1 280px}}
    .event-list{{max-height:280px;overflow:auto;border:1px solid var(--line);border-radius:12px;background:#fbfdff}}
    .event-row{{padding:8px 10px;border-bottom:1px solid #e7edf6}} .event-row:last-child{{border-bottom:none}}
    table{{width:100%;border-collapse:collapse}} th,td{{padding:7px 8px;border-bottom:1px solid #e7edf6;text-align:left;vertical-align:top;font-size:13px}}
    th{{color:#425466;background:#f7fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
    @media(max-width:1100px){{.hero,.audit-shell{{grid-template-columns:1fr}} .audit-view{{height:60vh}} .entry{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<div class="wrap">
  {nav_html}
  <div class="hero">
    <section class="card">
      <div class="title">
        <div>
          <h1>Machine Control</h1>
          <div class="muted">Geschuetzte Bedienkopie der physischen Tasten und aktueller Maschinenzustand.</div>
        </div>
        <span id="health_pill" class="pill">lade...</span>
      </div>
      <div class="grid">
        <div class="card" style="box-shadow:none">
          <h3>Aktueller Status</h3>
          <div id="state_kv" class="kv" style="margin-top:10px"></div>
        </div>
        <div class="card" style="box-shadow:none">
          <h3>Sicherheit / Freigaben</h3>
          <div id="safety_kv" class="kv" style="margin-top:10px"></div>
        </div>
      </div>
    </section>
    <section class="card">
      <div class="title">
        <div>
          <h2>Virtuelle Maschinentasten</h2>
          <div class="muted small">Verwendet dieselbe Zustands-/MAP0065-Freigabe wie die echten Taster.</div>
        </div>
      </div>
      <div class="btnrow" id="button_row"></div>
      <div id="button_msg" class="muted small" style="margin-top:10px">Bereit.</div>
    </section>
  </div>

  <section class="card" style="margin-bottom:14px">
    <div class="title">
      <div>
        <h2>Bypass / Simulation</h2>
        <div class="muted small">MAP0035-MAP0038 werden wie Microtom-Parameter geschrieben und zur ESP32-PLC gespiegelt.</div>
      </div>
      <span id="bypass_status" class="pill">lade...</span>
    </div>
    <div id="bypass_grid" class="bypass-grid"></div>
    <div class="toolbar" style="margin-top:12px">
      <button onclick="saveBypass()" class="primary">Bypass speichern</button>
      <button onclick="loadBypass()">Neu laden</button>
    </div>
  </section>

  <div class="audit-shell">
    <aside class="card">
      <div class="title">
        <div>
          <h2>Audit Log</h2>
          <div class="muted small">Lesbare Sicht auf Microtom, Raspi und Geraetekommunikation.</div>
        </div>
      </div>
      <div class="toolbar" style="margin-bottom:12px">
        <button onclick="loadAll()" class="primary">Aktualisieren</button>
        <label class="check"><input id="auto_refresh" type="checkbox" checked/>Auto</label>
      </div>
      <div class="kv">
        <div class="muted">Speichern</div>
        <div><input id="keep_hours" type="number" min="1" max="87600" style="width:92px"/> h</div>
        <div class="muted">Anzeige</div>
        <div><input id="view_hours" type="number" min="1" max="87600" style="width:92px"/> h</div>
        <div class="muted">Limit</div>
        <div><input id="entry_limit" type="number" min="50" max="5000" value="800" style="width:92px"/></div>
      </div>
      <div class="toolbar" style="margin:12px 0">
        <button onclick="saveRetention()">Retention speichern</button>
        <button onclick="downloadAudit()">Download</button>
      </div>
      <div id="audit_status" class="muted small">Audit wird geladen...</div>
      <hr style="border:none;border-top:1px solid var(--line);margin:14px 0"/>
      <h3>Letzte Maschinenereignisse</h3>
      <div id="events" class="event-list" style="margin-top:8px"></div>
    </aside>
    <main class="card">
      <div class="title">
        <div>
          <h2>Zentrale Kommunikationssicht</h2>
          <div class="muted small">Maschinencodes plus Klartext aus Masterdaten und Runtime-Ereignissen.</div>
        </div>
        <span id="audit_count" class="pill">0 Eintraege</span>
      </div>
      <div class="toolbar audit-filters">
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="direction" value="IN" checked/>IN</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="direction" value="OUT" checked/>OUT</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="level" value="error" checked/>Error</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="level" value="warning" checked/>Warning</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="level" value="info" checked/>Info</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="category" value="communication" checked/>Kommunikation</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="category" value="machine" checked/>Maschine</label>
        <label class="filter-chip"><input class="audit-filter" type="checkbox" data-filter="category" value="label" checked/>Label</label>
        <input id="audit_search" type="search" placeholder="Suche nach Code, Text, Quelle..."/>
        <button id="audit_clear_btn" type="button" onclick="toggleAuditDisplayClear()">Anzeige leeren</button>
      </div>
      <div id="audit_entries" class="audit-view"></div>
    </main>
  </div>
</div>
<script>
const TOKEN_KEY = "mas004_ui_token";
const AUDIT_PREF_KEY = "mas004_machine_audit_prefs";
const buttons = [
  ["start_pause", "Start/Pause"],
  ["stop", "Stop"],
  ["setup", "Einrichten"],
  ["sync", "Synchronisieren"],
  ["empty", "Leerfahren"],
  ["rewind", "Zurueckspulen"]
];
const bypassCards = [
  {{
    key:"MAP0036",
    title:"Material-Kontrollkamera",
    note:"Trigger bleibt aktiv, Kamera-IOs werden ignoriert und die Rueckmeldung wird simuliert.",
    fields:[["MAP0067", "Simulation", "0=alle gut, 1=alle schlecht, n=jede n-te schlecht"]]
  }},
  {{
    key:"MAP0035",
    title:"Drucksystem",
    note:"Laser/TTO wird nicht getriggert; Bereit/Fertig wird ueber die simulierte Druckdauer erzeugt.",
    fields:[["MAP0069", "Laser-Dauer ms", "Simulierte Laser-Druckdauer"], ["MAP0070", "TTO-Dauer ms", "Simulierte TTO-Druckdauer"]]
  }},
  {{
    key:"MAP0037",
    title:"Druck-Verifikationskamera",
    note:"Trigger bleibt aktiv, OCR-IOs werden ignoriert und die Rueckmeldung wird simuliert.",
    fields:[["MAP0068", "Simulation", "0=alle gut, 1=alle schlecht, n=jede n-te schlecht"]]
  }},
  {{
    key:"MAP0038",
    title:"Etiketten-Entnahmesensor",
    note:"Keine Entnahmekontrolle/Rueckspulung; Registerwerte bleiben trotzdem dokumentiert.",
    fields:[]
  }}
];
let refreshTimer = null;
function token(){{ try{{return localStorage.getItem(TOKEN_KEY)||"";}}catch(e){{return"";}} }}
function loadAuditPrefs(){{
  try{{return JSON.parse(localStorage.getItem(AUDIT_PREF_KEY) || "{{}}") || {{}};}}catch(e){{return {{}};}}
}}
function saveAuditPrefs(){{
  const prefs = loadAuditPrefs();
  Object.assign(prefs, {{
    keep_hours: num("keep_hours", 72, 1, 87600),
    view_hours: num("view_hours", 72, 1, 87600),
    entry_limit: num("entry_limit", 800, 50, 5000)
  }});
  try{{localStorage.setItem(AUDIT_PREF_KEY, JSON.stringify(prefs));}}catch(e){{}}
  return prefs;
}}
function applyAuditPrefs(){{
  const prefs = loadAuditPrefs();
  document.getElementById("view_hours").value = Number.isFinite(Number(prefs.view_hours)) ? prefs.view_hours : 72;
  document.getElementById("keep_hours").value = Number.isFinite(Number(prefs.keep_hours)) ? prefs.keep_hours : 72;
  document.getElementById("entry_limit").value = Number.isFinite(Number(prefs.entry_limit)) ? prefs.entry_limit : 800;
}}
function esc(v){{return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}}
async function api(path,opt={{}}){{
  opt.headers = opt.headers || {{}};
  const t = token(); if(t) opt.headers["X-Token"] = t;
  const r = await fetch(path,opt);
  const txt = await r.text();
  let j = null; try{{j=JSON.parse(txt);}}catch(e){{}}
  if(!r.ok){{throw new Error((j&&j.detail)?j.detail:(`HTTP ${{r.status}} ${{txt}}`));}}
  return j;
}}
function kv(id, pairs){{
  document.getElementById(id).innerHTML = pairs.map(([k,v])=>`<div class="muted">${{esc(k)}}</div><div>${{v}}</div>`).join("");
}}
function flag(v){{return v ? '<span class="pill bad">aktiv</span>' : '<span class="pill ok">ok</span>';}}
const reasonLabels = {{
  "notaus": "Not-Aus aktiv",
  "lichtgitter": "Lichtgitter unterbrochen",
  "usv_not_ok": "USV nicht OK",
  "bahnriss_einlauf": "Bahnriss Einlauf",
  "bahnriss_auswurf": "Bahnriss Auswurf",
  "MAE0008": "Etikettenfuehrung Einlauf gestoert",
  "MAE0009": "Etikettenfuehrung Auslauf gestoert",
  "MAE0025": "Label zu kurz",
  "MAE0026": "Label zu lang",
  "MAE0028": "Abwicklung Taenzerarm blockiert",
  "MAE0029": "Abwicklung Taenzerarm zu hoch",
  "MAE0030": "Abwicklung Taenzerarm zu tief",
  "MAE0032": "Aufwicklung Taenzerarm blockiert",
  "MAE0033": "Aufwicklung Taenzerarm zu hoch",
  "MAE0034": "Aufwicklung Taenzerarm zu tief",
  "MAE0048": "Etikettenantrieb Stopptoleranz nicht erreicht"
}};
function reasonHtml(reasons){{
  const items = Array.isArray(reasons) ? reasons : [];
  if(!items.length) return '<span class="reason-empty">-</span>';
  return `<div class="reason-list">${{items.map(r => {{
    const code = String(r || "");
    const label = reasonLabels[code] || code;
    return `<span class="reason-chip"><span>${{esc(label)}}</span><span class="code">${{esc(code)}}</span></span>`;
  }}).join("")}}</div>`;
}}
function ioDetailHtml(point){{
  if(!point) return '<span class="reason-empty">-</span>';
  const ok = !!point.active;
  const label = `${{point.device_code || ""}} ${{point.pin_label || ""}}`;
  const meta = `${{point.value ?? "-"}} / ${{point.quality || "-"}} / ${{point.source || "-"}}`;
  return `<span class="pill ${{ok ? "ok" : "bad"}}">${{ok ? "HIGH" : "LOW"}}</span> <span class="small muted">${{esc(label)}} · ${{esc(meta)}}</span>`;
}}
function formatTs(ts){{try{{return new Date(Number(ts||0)*1000).toLocaleString();}}catch(e){{return "-";}}}}
function actionForButton(button, state, resetContext){{
  if(button === "start_pause") {{
    if(resetContext) return "start";
    return Number(state) === 5 ? "pause" : "start";
  }}
  return button;
}}
function buttonLabel(button, state, resetContext){{
  if(button === "start_pause") {{
    if(resetContext) return "Reset";
    return Number(state) === 5 ? "Pause" : "Start";
  }}
  return buttons.find(x=>x[0]===button)?.[1] || button;
}}
function renderButtons(machine){{
  const info = machine.info || {{}};
  const state = Number(machine.current_state || 1);
  const safety = info.safety || {{}};
  const resetContext = state === 20 || state === 21 || !!machine.purge_active || !!safety.latched;
  const allowed = info.allowed_actions || {{}};
  const mask = info.button_mask || {{}};
  document.getElementById("button_row").innerHTML = buttons.map(([key]) => {{
    const action = actionForButton(key, state, resetContext);
    const enabled = resetContext
      ? (key === "start_pause")
      : (!!allowed[action] && !!mask[action]);
    const label = buttonLabel(key, state, resetContext);
    const cls = key === "stop" ? " stop" : (resetContext && key === "start_pause" ? " reset" : "");
    return `<button class="control${{enabled?" enabled":""}}${{cls}}" onclick="pressButton('${{key}}')" ${{enabled?"":"disabled"}}>${{esc(label)}}</button>`;
  }}).join("");
}}
async function pressButton(button){{
  const el = document.getElementById("button_msg");
  el.textContent = "sende...";
  try{{
    const j = await api("/api/machine/button", {{
      method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{button}})
    }});
    el.textContent = `OK: ${{j.button}} -> MAS0002=${{j.command}}`;
    await loadAll();
  }}catch(err){{
    el.textContent = `Fehler: ${{err.message}}`;
  }}
}}
function bypassParamMap(payload){{
  const map = {{}};
  (payload?.parameters || []).forEach(p => map[p.pkey] = p);
  return map;
}}
function paramNumber(map, key, fallback){{
  const value = Number(map[key]?.value ?? fallback);
  return Number.isFinite(value) ? value : fallback;
}}
function renderBypass(payload){{
  const map = bypassParamMap(payload || {{}});
  const grid = document.getElementById("bypass_grid");
  grid.innerHTML = bypassCards.map(card => {{
    const active = paramNumber(map, card.key, 0) ? 1 : 0;
    const fields = card.fields.map(([key,label,hint]) => {{
      const meta = map[key] || {{}};
      const min = meta.min_v ?? 0;
      const max = meta.max_v ?? 10000;
      return `<div class="muted small">${{esc(label)}}<br/><span class="small">${{esc(hint)}}</span></div>
        <input id="bypass_${{key}}" type="number" min="${{esc(min)}}" max="${{esc(max)}}" value="${{esc(paramNumber(map,key,0))}}"/>`;
    }}).join("");
    return `<div class="bypass-card ${{active?"active":""}}">
      <h3><span>${{esc(card.title)}}</span><label class="check"><input id="bypass_${{card.key}}" type="checkbox" ${{active?"checked":""}}>Bypass</label></h3>
      <div class="muted small" style="margin-top:6px">${{esc(card.note)}}</div>
      <div class="bypass-fields">${{fields}}</div>
    </div>`;
  }}).join("");
  document.getElementById("bypass_status").className = "pill ok";
  document.getElementById("bypass_status").textContent = "geladen";
}}
async function loadBypass(){{
  try{{
    const j = await api("/api/machine/bypass");
    renderBypass(j);
    window.bypassLoaded = true;
  }}catch(err){{
    const el = document.getElementById("bypass_status");
    el.className = "pill bad";
    el.textContent = err.message;
  }}
}}
async function saveBypass(){{
  const values = {{}};
  bypassCards.forEach(card => {{
    values[card.key] = document.getElementById(`bypass_${{card.key}}`)?.checked ? 1 : 0;
    card.fields.forEach(([key]) => {{
      const input = document.getElementById(`bypass_${{key}}`);
      values[key] = input ? Number(input.value || 0) : 0;
    }});
  }});
  const el = document.getElementById("bypass_status");
  el.className = "pill warn";
  el.textContent = "speichere...";
  try{{
    const j = await api("/api/machine/bypass", {{
      method:"POST",
      headers:{{"Content-Type":"application/json"}},
      body:JSON.stringify({{values}})
    }});
    renderBypass(j.bypass || j);
    el.className = "pill ok";
    el.textContent = "gespeichert";
    await loadAudit();
  }}catch(err){{
    el.className = "pill bad";
    el.textContent = err.message;
  }}
}}
function renderMachine(machine){{
  const info = machine.info || {{}};
  const safety = info.safety || {{}};
  const lamp = info.status_lamp || {{}};
  const safetyLatched = !!safety.latched || Number(machine.current_state || 0) === 21;
  document.getElementById("health_pill").className = (machine.purge_active || safetyLatched) ? "pill bad" : (machine.warning_active ? "pill warn" : "pill ok");
  document.getElementById("health_pill").textContent = machine.purge_active
    ? "Purge"
    : (safetyLatched ? "Safety/Reset" : (machine.warning_active ? "Warnung" : "bereit"));
  kv("state_kv", [
    ["Ist", `<span class="pill">${{esc(machine.current_state)}} - ${{esc(machine.current_state_label)}}</span>`],
    ["Soll", `<span class="pill">${{esc(machine.requested_state)}} - ${{esc(machine.requested_state_label)}}</span>`],
    ["Produktion", esc(machine.production_label || "-")],
    ["Letztes Label", esc(machine.last_label_no ?? "-")],
    ["Statusleuchte", `<span class="pill">${{esc(lamp.color || "-")}} ${{lamp.blink ? "blinkend" : ""}}</span>`]
  ]);
  kv("safety_kv", [
    ["Warnung", flag(!!machine.warning_active)],
    ["Purge", flag(!!machine.purge_active)],
    ["Safety-Latch", flag(!!safety.latched)],
    ["USV I0.6", ioDetailHtml((info.safety_status || {{}}).ups_input)],
    ["Kritische Gruende", reasonHtml(info.critical_reasons || [])],
    ["MAP0065", `<span class="mono code-wrap">${{esc(JSON.stringify(info.button_mask || {{}}))}}</span>`]
  ]);
  const rows = (machine.events || []).map(it => `<div class="event-row"><div class="small muted">${{esc(formatTs(it.ts))}} - ${{esc(it.event_type || "")}}</div><div>${{esc(it.message || "")}}</div></div>`);
  document.getElementById("events").innerHTML = rows.join("") || '<div class="event-row muted">Keine Ereignisse.</div>';
  renderButtons(machine);
}}
function renderAudit(items){{
  window.auditItems = Array.isArray(items) ? items : [];
  renderFilteredAudit();
}}
function auditTruthy(v){{
  const text = String(v ?? "").trim().toLowerCase();
  return !["", "0", "false", "off", "no", "none", "null"].includes(text);
}}
function auditLevel(it){{
  const dir = String(it.direction || "").toUpperCase();
  const pkey = String(it.pkey || "").toUpperCase();
  const value = String(it.value ?? "");
  if(/^MAE/.test(pkey)) return auditTruthy(value) ? "error" : "info";
  if(/^MAW/.test(pkey)) return auditTruthy(value) ? "warning" : "info";
  if(pkey === "MAS0028") return auditTruthy(value) ? "warning" : "info";
  const text = `${{it.direction||""}} ${{it.message||""}} ${{it.summary||""}} ${{it.description||""}}`.toLowerCase();
  if(/ignored duplicate|ignored .*clear|ack_[a-z]{{3}}\\d+=0/.test(text)) return "info";
  if(dir === "ERR" || dir === "ERROR" || /\\b(error|fehler|failed|failure|exception|traceback|nak_|nak-|timed out|timeout|störung|stoerung)\\b/.test(text)) return "error";
  if(dir === "WARN" || dir === "WARNING" || /\\b(warn|warning|warnung|skipped|retry|cooldown)\\b/.test(text)) return "warning";
  return "info";
}}
function checkedValues(group){{
  return new Set(Array.from(document.querySelectorAll(`.audit-filter[data-filter="${{group}}"]:checked`)).map(x=>String(x.value)));
}}
function auditClearAfterTs(){{
  const value = Number(loadAuditPrefs().audit_clear_after_ts || 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}}
function setAuditClearAfterTs(value){{
  const prefs = loadAuditPrefs();
  if(Number(value) > 0) prefs.audit_clear_after_ts = Number(value);
  else delete prefs.audit_clear_after_ts;
  try{{localStorage.setItem(AUDIT_PREF_KEY, JSON.stringify(prefs));}}catch(e){{}}
}}
function toggleAuditDisplayClear(){{
  const current = auditClearAfterTs();
  setAuditClearAfterTs(current > 0 ? 0 : Math.floor(Date.now() / 1000));
  renderFilteredAudit();
}}
function updateAuditClearButton(){{
  const button = document.getElementById("audit_clear_btn");
  if(!button) return;
  const cutoff = auditClearAfterTs();
  button.textContent = cutoff > 0 ? "Verlauf wieder anzeigen" : "Anzeige leeren";
  button.className = cutoff > 0 ? "btn primary" : "";
  button.title = cutoff > 0
    ? "Lokale Anzeige-Sperre aufheben; gespeicherte Auditdaten bleiben unveraendert."
    : "Nur das sichtbare Fenster leeren; gespeicherte Auditdaten bleiben gemaess Retention erhalten.";
}}
function auditMatches(it){{
  const cutoff = auditClearAfterTs();
  if(cutoff > 0 && Number(it.ts || 0) <= cutoff) return false;
  const directions = checkedValues("direction");
  const levels = checkedValues("level");
  const categories = checkedValues("category");
  const direction = String(it.direction || "INFO").toUpperCase();
  const category = String(it.category || "communication");
  const level = auditLevel(it);
  const directionOk = direction === "IN" || direction === "OUT" ? directions.has(direction) : true;
  const levelOk = levels.has(level);
  const categoryOk = categories.has(category);
  const search = String(document.getElementById("audit_search")?.value || "").trim().toLowerCase();
  const searchOk = !search || `${{it.ts_display||""}} ${{it.category||""}} ${{it.source||""}} ${{it.direction||""}} ${{it.device||""}} ${{it.pkey||""}} ${{it.value||""}} ${{it.summary||""}} ${{it.description||""}} ${{it.message||""}}`.toLowerCase().includes(search);
  return directionOk && levelOk && categoryOk && searchOk;
}}
function auditGroupKey(it){{
  return [
    auditLevel(it),
    String(it.category || ""),
    String(it.device || it.source || ""),
    String(it.direction || ""),
    String(it.pkey || ""),
    String(it.value ?? ""),
    String(it.summary || it.message || "")
  ].join("|");
}}
function groupAuditItems(items){{
  const grouped = [];
  for(const it of items){{
    const key = auditGroupKey(it);
    const ts = Number(it.ts || 0);
    const prev = grouped[grouped.length - 1];
    const prevTs = Number(prev?._lastTs || prev?.ts || 0);
    if(prev && prev._groupKey === key && ts > 0 && prevTs > 0 && Math.abs(ts - prevTs) <= 1.5){{
      prev._count = Number(prev._count || 1) + 1;
      prev._lastTs = ts;
      continue;
    }}
    grouped.push({{...it, _groupKey: key, _lastTs: ts, _count: 1}});
  }}
  return grouped;
}}
function renderFilteredAudit(){{
  updateAuditClearButton();
  const itemsAll = window.auditItems || [];
  const items = itemsAll.filter(auditMatches);
  const grouped = groupAuditItems(items);
  const box = document.getElementById("audit_entries");
  document.getElementById("audit_count").textContent = `${{grouped.length}} / ${{itemsAll.length}} Eintraege`;
  if(!items.length){{
    box.innerHTML = '<div class="entry"><div class="muted">Keine Audit-Eintraege im gewaehlten Zeitfenster.</div></div>';
    return;
  }}
  box.innerHTML = grouped.map(it => {{
    const cat = esc(it.category || "communication");
    const level = auditLevel(it);
    const code = it.pkey ? `<span class="pill">${{esc(it.pkey)}}${{it.value!==""?"="+esc(it.value):""}}</span>` : `<span class="pill">${{cat}}</span>`;
    const count = Number(it._count || 1) > 1 ? `<span class="pill">x${{Number(it._count || 1)}}</span>` : "";
    const source = esc(it.device || it.source || "-");
    const raw = it.message ? `<div class="raw">${{esc(it.message)}}</div>` : "";
    const desc = it.description ? `<div class="small muted">${{esc(it.description)}}</div>` : "";
    return `<div class="entry ${{cat}} level-${{level}}">
      <div><div class="mono small">${{esc(it.ts_display || formatTs(it.ts))}}</div><div class="small muted">${{esc(it.direction || "")}}</div></div>
      <div>${{code}}${{count}}<div class="small muted" style="margin-top:5px">${{source}}</div></div>
      <div><div class="summary">${{esc(it.summary || it.message || "")}}</div>${{desc}}${{raw}}</div>
    </div>`;
  }}).join("");
}}
function num(id, fallback, min, max){{
  const v = Number(document.getElementById(id).value);
  if(!Number.isFinite(v)) return fallback;
  return Math.max(min, Math.min(max, Math.round(v)));
}}
async function loadAudit(){{
  saveAuditPrefs();
  const hours = num("view_hours", 72, 1, 87600);
  const limit = num("entry_limit", 800, 50, 5000);
  const j = await api(`/api/machine/audit?hours=${{hours}}&limit=${{limit}}`);
  const prefs = loadAuditPrefs();
  document.getElementById("keep_hours").value = Number.isFinite(Number(prefs.keep_hours)) ? prefs.keep_hours : (j.keep_hours || hours);
  document.getElementById("view_hours").value = hours;
  renderAudit(j.entries || []);
  document.getElementById("audit_status").textContent = `Fenster: ${{hours}} h, Limit: ${{limit}}, Aufbewahrung: ${{j.keep_hours}} h`;
}}
async function loadAll(){{
  try{{
    const machine = await api("/api/machine/overview");
    renderMachine(machine);
    if(!window.bypassLoaded) await loadBypass();
    await loadAudit();
  }}catch(err){{
    document.getElementById("health_pill").className = "pill bad";
    document.getElementById("health_pill").textContent = err.message;
    document.getElementById("audit_status").textContent = err.message;
  }}
}}
async function saveRetention(){{
  saveAuditPrefs();
  const keep = num("keep_hours", 72, 1, 87600);
  const j = await api("/api/machine/audit/retention", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{keep_hours:keep}})
  }});
  document.getElementById("keep_hours").value = j.keep_hours || keep;
  saveAuditPrefs();
  document.getElementById("audit_status").textContent = `Aufbewahrung gespeichert: ${{j.keep_hours}} h`;
  await loadAudit();
}}
function downloadAudit(){{
  const hours = num("view_hours", 72, 1, 87600);
  const limit = num("entry_limit", 800, 50, 5000);
  window.location.href = `/api/machine/audit/download?hours=${{hours}}&limit=${{limit}}`;
}}
function schedule(){{
  if(refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(()=>{{ if(!document.hidden && document.getElementById("auto_refresh").checked) loadAll(); }}, 1000);
}}
document.getElementById("auto_refresh").addEventListener("change", schedule);
document.getElementById("keep_hours").addEventListener("change", saveAuditPrefs);
document.getElementById("view_hours").addEventListener("change", ()=>{{saveAuditPrefs(); loadAudit();}});
document.getElementById("entry_limit").addEventListener("change", ()=>{{saveAuditPrefs(); loadAudit();}});
document.querySelectorAll(".audit-filter").forEach(el => el.addEventListener("change", renderFilteredAudit));
document.getElementById("audit_search").addEventListener("input", renderFilteredAudit);
applyAuditPrefs();
schedule();
loadAll();
</script>
</body>
</html>
"""
