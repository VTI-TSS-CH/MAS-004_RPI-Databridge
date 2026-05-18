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
    .audit-view{{height:64vh;min-height:520px;overflow:auto;border:1px solid var(--line);border-radius:14px;background:#fbfdff}}
    .entry{{display:grid;grid-template-columns:168px 112px 1fr;gap:10px;padding:10px 12px;border-bottom:1px solid #e7edf6;align-items:start}}
    .entry:last-child{{border-bottom:none}}
    .entry.communication{{background:#fff}} .entry.machine{{background:#f7fbff}} .entry.label{{background:#fbfff8}}
    .summary{{font-weight:700}} .raw{{margin-top:4px;color:#4b5b6f;white-space:pre-wrap;word-break:break-word;font-size:12px}}
    input,select{{min-height:38px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:#fff}}
    label.check{{display:inline-flex;gap:7px;align-items:center}}
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
      <div id="audit_entries" class="audit-view"></div>
    </main>
  </div>
</div>
<script>
const TOKEN_KEY = "mas004_ui_token";
const buttons = [
  ["start_pause", "Start/Pause"],
  ["stop", "Stop"],
  ["setup", "Einrichten"],
  ["sync", "Synchronisieren"],
  ["empty", "Leerfahren"],
  ["rewind", "Zurueckspulen"]
];
let refreshTimer = null;
function token(){{ try{{return localStorage.getItem(TOKEN_KEY)||"";}}catch(e){{return"";}} }}
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
function formatTs(ts){{try{{return new Date(Number(ts||0)*1000).toLocaleString();}}catch(e){{return "-";}}}}
function actionForButton(button, state, resetContext){{
  if(button === "start_pause") {{
    if(resetContext) return "stop";
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
    const enabled = (resetContext && key === "start_pause") || (!!allowed[action] && !!mask[action]);
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
    ["Kritische Gruende", reasonHtml(info.critical_reasons || [])],
    ["MAP0065", `<span class="mono code-wrap">${{esc(JSON.stringify(info.button_mask || {{}}))}}</span>`]
  ]);
  const rows = (machine.events || []).map(it => `<div class="event-row"><div class="small muted">${{esc(formatTs(it.ts))}} - ${{esc(it.event_type || "")}}</div><div>${{esc(it.message || "")}}</div></div>`);
  document.getElementById("events").innerHTML = rows.join("") || '<div class="event-row muted">Keine Ereignisse.</div>';
  renderButtons(machine);
}}
function renderAudit(items){{
  const box = document.getElementById("audit_entries");
  document.getElementById("audit_count").textContent = `${{items.length}} Eintraege`;
  if(!items.length){{
    box.innerHTML = '<div class="entry"><div class="muted">Keine Audit-Eintraege im gewaehlten Zeitfenster.</div></div>';
    return;
  }}
  box.innerHTML = items.map(it => {{
    const cat = esc(it.category || "communication");
    const code = it.pkey ? `<span class="pill">${{esc(it.pkey)}}${{it.value!==""?"="+esc(it.value):""}}</span>` : `<span class="pill">${{cat}}</span>`;
    const source = esc(it.device || it.source || "-");
    const raw = it.message ? `<div class="raw">${{esc(it.message)}}</div>` : "";
    const desc = it.description ? `<div class="small muted">${{esc(it.description)}}</div>` : "";
    return `<div class="entry ${{cat}}">
      <div><div class="mono small">${{esc(it.ts_display || formatTs(it.ts))}}</div><div class="small muted">${{esc(it.direction || "")}}</div></div>
      <div>${{code}}<div class="small muted" style="margin-top:5px">${{source}}</div></div>
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
  const hours = num("view_hours", 72, 1, 87600);
  const limit = num("entry_limit", 800, 50, 5000);
  const j = await api(`/api/machine/audit?hours=${{hours}}&limit=${{limit}}`);
  document.getElementById("keep_hours").value = j.keep_hours || hours;
  document.getElementById("view_hours").value = hours;
  renderAudit(j.entries || []);
  document.getElementById("audit_status").textContent = `Fenster: ${{hours}} h, Aufbewahrung: ${{j.keep_hours}} h`;
}}
async function loadAll(){{
  try{{
    const machine = await api("/api/machine/overview");
    renderMachine(machine);
    await loadAudit();
  }}catch(err){{
    document.getElementById("health_pill").className = "pill bad";
    document.getElementById("health_pill").textContent = err.message;
    document.getElementById("audit_status").textContent = err.message;
  }}
}}
async function saveRetention(){{
  const keep = num("keep_hours", 72, 1, 87600);
  const j = await api("/api/machine/audit/retention", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{keep_hours:keep}})
  }});
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
  refreshTimer = setInterval(()=>{{ if(!document.hidden && document.getElementById("auto_refresh").checked) loadAll(); }}, 3000);
}}
document.getElementById("auto_refresh").addEventListener("change", schedule);
document.getElementById("view_hours").addEventListener("change", loadAudit);
document.getElementById("entry_limit").addEventListener("change", loadAudit);
document.getElementById("view_hours").value = 72;
document.getElementById("keep_hours").value = 72;
schedule();
loadAll();
</script>
</body>
</html>
"""
