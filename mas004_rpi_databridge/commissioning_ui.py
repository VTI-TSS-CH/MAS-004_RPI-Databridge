from __future__ import annotations


def build_commissioning_ui_html(nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Machine Commissioning</title>
  <style>
    :root {{
      --bg:#f4f6f9; --card:#fff; --text:#1f2933; --muted:#5f6b7a; --border:#d6dde7; --blue:#005eb8;
      --good:#2e7d32; --warn:#ed6c02; --bad:#c62828;
    }}
    *{{box-sizing:border-box}}
    body{{margin:0; font-family:Segoe UI,Arial,sans-serif; background:var(--bg); color:var(--text)}}
    .wrap{{max-width:1700px; margin:0 auto; padding:16px}}
    .toolbar,.row,.actions{{display:flex; gap:10px; align-items:center; flex-wrap:wrap}}
    .toolbar{{margin-bottom:12px}}
    .grid{{display:grid; gap:12px}}
    .hero{{grid-template-columns:1.4fr 1fr}}
    .two{{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}}
    .card{{background:#fff; border:1px solid var(--border); border-radius:12px; padding:14px}}
    .btn{{min-height:38px; padding:8px 12px; border:1px solid #aec4db; border-radius:10px; background:#e8f0f8; color:#17324b; font-weight:600; cursor:pointer}}
    .btn.primary{{background:#005eb8; color:#fff; border-color:#005eb8}}
    .btn.small{{min-height:30px; padding:4px 8px; font-size:12px}}
    .muted{{color:var(--muted)}} .ok{{color:var(--good)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
    .pill{{display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; border:1px solid var(--border); background:#eef3f8; font-size:12px}}
    .field{{display:flex; flex-direction:column; gap:4px; min-width:180px}}
    .field label{{font-size:12px; color:var(--muted); font-weight:700}}
    input,textarea,select{{width:100%; min-height:38px; padding:9px 10px; border:1px solid var(--border); border-radius:10px; background:#fff}}
    textarea{{min-height:84px; resize:vertical}}
    table{{width:100%; border-collapse:collapse}}
    th,td{{padding:8px 10px; border-top:1px solid #e7edf6; text-align:left; vertical-align:top}}
    th{{background:#f7fafc; color:#425466; font-size:12px; text-transform:uppercase; letter-spacing:.04em}}
    tr:first-child td{{border-top:none}}
    .section{{font-size:12px; text-transform:uppercase; color:#5f6b7a; font-weight:800; letter-spacing:.05em}}
    .step-actions{{display:flex; gap:6px; flex-wrap:wrap}}
    .status-success{{color:var(--good); font-weight:700}}
    .status-reused{{color:#2563eb; font-weight:700}}
    .status-failed{{color:var(--bad); font-weight:700}}
    .status-skipped{{color:var(--warn); font-weight:700}}
    .status-pending{{color:#6b7280; font-weight:700}}
    code{{white-space:pre-wrap; word-break:break-word}}
    @media(max-width:1100px){{ .hero{{grid-template-columns:1fr}} }}
  </style>
</head>
<body>
  <div class="wrap">
    {nav_html}
    <div class="toolbar">
      <button class="btn" onclick="reloadAll()">Reload</button>
      <button class="btn primary" onclick="startRun('full')">Assistent komplett neu</button>
      <button class="btn" onclick="startRun('incomplete_only')">Nur offene Punkte</button>
      <span id="status" class="muted">loading...</span>
    </div>

    <div class="grid hero">
      <div class="card">
        <div class="section">Maschinenidentitaet</div>
        <div class="row" style="margin-top:10px">
          <div class="field">
            <label>Seriennummer</label>
            <input id="machine_serial_number" placeholder="z.B. MAS004-001"/>
          </div>
          <div class="field" style="flex:1 1 320px">
            <label>Maschinenname</label>
            <input id="machine_name" placeholder="z.B. Roche TEST Anlage"/>
          </div>
          <div class="actions" style="padding-top:20px">
            <button class="btn" onclick="saveIdentity()">Speichern</button>
          </div>
        </div>
        <div class="muted" style="margin-top:8px">Der Bootstrap-Helfer fuer frische Raspis liegt unter <code>scripts/mas004_machine_bootstrap.py</code>. Von dort kann eine erste Grundinstallation oder das Einspielen eines Vollbackups gestartet werden.</div>
        <div id="bootstrapHints" style="margin-top:10px"></div>
      </div>
      <div class="card">
        <div class="section">Aktiver Lauf</div>
        <div id="runSummary" style="margin-top:10px" class="muted">Noch kein Lauf gestartet.</div>
        <div id="runMeta" style="margin-top:10px"></div>
      </div>
    </div>

    <div class="grid two" style="margin-top:12px">
      <div class="card">
        <div class="section">Schritte</div>
        <table style="margin-top:10px">
          <thead>
            <tr><th>Abschnitt</th><th>Schritt</th><th>Status</th><th>Hinweis</th><th>Aktion</th></tr>
          </thead>
          <tbody id="steps"></tbody>
        </table>
      </div>
      <div class="card">
        <div class="section">Letzte Laeufe</div>
        <table style="margin-top:10px">
          <thead>
            <tr><th>#</th><th>Modus</th><th>Status</th><th>Seriennr.</th><th>Zusammenfassung</th></tr>
          </thead>
          <tbody id="runs"></tbody>
        </table>
      </div>
    </div>
  </div>
<script>
const TOKEN_KEY = "mas004_ui_token";
let CURRENT_RUN_ID = null;

function token(){{ try {{ return localStorage.getItem(TOKEN_KEY) || ""; }} catch(e) {{ return ""; }} }}
function esc(v){{ return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;"); }}

async function api(path, opt={{}}){{
  opt.headers = opt.headers || {{}};
  const t = token();
  if(t) opt.headers["X-Token"] = t;
  const r = await fetch(path, opt);
  const txt = await r.text();
  let j = null; try {{ j = JSON.parse(txt); }} catch(e) {{}}
  if(!r.ok) throw new Error((j && j.detail) ? j.detail : (`HTTP ${{r.status}} ${{txt}}`));
  return j;
}}

function setStatus(text){{ document.getElementById("status").textContent = text || ""; }}
function statusClass(status){{ return `status-${{String(status || "pending").toLowerCase()}}`; }}

function renderOverview(data){{
  document.getElementById("machine_serial_number").value = data.machine_serial_number || "";
  document.getElementById("machine_name").value = data.machine_name || "";
  const hints = data.bootstrap_script || {{}};
  document.getElementById("bootstrapHints").innerHTML =
    `<div class="pill">${{esc(hints.path || "-")}}</div>
     <div class="muted" style="margin-top:8px"><code>${{esc(hints.example_discover || "")}}</code></div>
     <div class="muted" style="margin-top:4px"><code>${{esc(hints.example_clone || "")}}</code></div>`;

  const latest = data.latest_run || null;
  CURRENT_RUN_ID = latest ? latest.run_id : null;
  if(latest){{
    const s = latest.summary || {{}};
    document.getElementById("runSummary").innerHTML =
      `<div class="pill">Run #${{esc(latest.run_id)}}</div>
       <div class="pill">${{esc(latest.mode)}}</div>
       <div class="pill">${{esc(latest.status)}}</div>
       <div class="muted" style="margin-top:8px">Success-like: ${{esc(s.successful_like || 0)}} / ${{esc(s.total || 0)}} | Pending: ${{esc(s.pending || 0)}} | In progress: ${{esc(s.in_progress || 0)}} | Failed: ${{esc(s.failed || 0)}} | Skipped: ${{esc(s.skipped || 0)}}</div>`;
    document.getElementById("runMeta").innerHTML = "";
  }} else {{
    document.getElementById("runSummary").textContent = "Noch kein Lauf gestartet.";
    document.getElementById("runMeta").innerHTML = "";
  }}

  document.getElementById("runs").innerHTML = (data.runs || []).map(run => {{
    const s = run.summary || {{}};
    return `<tr>
      <td>${{esc(run.run_id)}}</td>
      <td>${{esc(run.mode)}}</td>
      <td class="${{statusClass(run.status)}}">${{esc(run.status)}}</td>
      <td>${{esc(run.machine_serial || "-")}}</td>
      <td>${{esc(`ok=${{s.successful_like || 0}} pending=${{s.pending || 0}} in_progress=${{s.in_progress || 0}} failed=${{s.failed || 0}} skipped=${{s.skipped || 0}}`)}}</td>
    </tr>`;
  }}).join("") || '<tr><td colspan="5" class="muted">Noch keine Laeufe</td></tr>';
}}

function renderSteps(run){{
  const rows = (run?.steps || []).map(step => {{
    const resultJson = step.result ? `<div class="muted"><code>${{esc(JSON.stringify(step.result, null, 2))}}</code></div>` : "";
    const linkBtn = step.href ? `<a class="btn small" href="${{step.href}}">Seite</a>` : "";
    const autoBtn = step.kind === "auto" ? `<button class="btn small" onclick="autoCheck('${{step.step_id}}')">Auto-Check</button>` : "";
    const note = step.note ? `${{esc(step.note)}}<br/>` : "";
    return `<tr>
      <td><div class="section">${{esc(step.section_id)}}</div></td>
      <td><strong>${{esc(step.title)}}</strong><div class="muted">${{esc(step.description || "")}}</div></td>
      <td class="${{statusClass(step.status)}}">${{esc(step.status)}}</td>
      <td>${{note}}${{resultJson}}</td>
      <td>
        <div class="step-actions">
          ${{autoBtn}}
          ${{linkBtn}}
          <button class="btn small" onclick="markStep('${{step.step_id}}','in_progress')">In Arbeit</button>
          <button class="btn small" onclick="markStep('${{step.step_id}}','success')">Erfolgreich</button>
          <button class="btn small" onclick="markStep('${{step.step_id}}','failed')">Fehlgeschlagen</button>
          <button class="btn small" onclick="markStep('${{step.step_id}}','skipped')">Ueberspringen</button>
          <button class="btn small" onclick="markStep('${{step.step_id}}','pending')">Zuruecksetzen</button>
        </div>
      </td>
    </tr>`;
  }}).join("");
  document.getElementById("steps").innerHTML = rows || '<tr><td colspan="5" class="muted">Noch kein Lauf gestartet.</td></tr>';
}}

async function loadRun(runId){{
  if(!runId){{ renderSteps(null); return; }}
  const run = await api(`/api/commissioning/run/${{runId}}`);
  renderSteps(run);
}}

async function reloadAll(){{
  setStatus("loading...");
  const data = await api("/api/commissioning/overview");
  renderOverview(data);
  await loadRun(CURRENT_RUN_ID);
  setStatus("ok");
}}

async function saveIdentity(){{
  setStatus("speichere Maschinenidentitaet...");
  await api("/api/backups/identity", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{
      machine_serial_number: document.getElementById("machine_serial_number").value.trim(),
      machine_name: document.getElementById("machine_name").value.trim()
    }})
  }});
  await reloadAll();
}}

async function startRun(mode){{
  setStatus(`starte Run (${{mode}})...`);
  const run = await api("/api/commissioning/run/start", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{mode}})
  }});
  CURRENT_RUN_ID = run.run_id;
  await reloadAll();
}}

async function autoCheck(stepId){{
  if(!CURRENT_RUN_ID) return;
  setStatus(`pruefe ${{stepId}}...`);
  await api(`/api/commissioning/run/${{CURRENT_RUN_ID}}/step/${{encodeURIComponent(stepId)}}/check`, {{method:"POST"}});
  await reloadAll();
}}

async function markStep(stepId, status){{
  if(!CURRENT_RUN_ID) return;
  const note = prompt(`Notiz fuer ${{stepId}} (${{status}})`, "");
  if(note === null && status !== "pending") return;
  setStatus(`schreibe ${{stepId}} -> ${{status}}...`);
  await api(`/api/commissioning/run/${{CURRENT_RUN_ID}}/step/${{encodeURIComponent(stepId)}}`, {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{status, note: note || ""}})
  }});
  await reloadAll();
}}

reloadAll().catch(err => setStatus(err.message));
</script>
</body>
</html>
"""
