from __future__ import annotations


def build_backup_ui_html(nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Machine Backups</title>
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
    .hero{{grid-template-columns:1.2fr 1fr}}
    .two{{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}}
    .card{{background:#fff; border:1px solid var(--border); border-radius:12px; padding:14px}}
    .btn{{min-height:38px; padding:8px 12px; border:1px solid #aec4db; border-radius:10px; background:#e8f0f8; color:#17324b; font-weight:600; cursor:pointer}}
    .btn.primary{{background:#005eb8; color:#fff; border-color:#005eb8}}
    .btn.small{{min-height:30px; padding:4px 8px; font-size:12px}}
    .field{{display:flex; flex-direction:column; gap:4px; min-width:180px}}
    .field label{{font-size:12px; color:#5f6b7a; font-weight:700}}
    input,textarea{{width:100%; min-height:38px; padding:9px 10px; border:1px solid var(--border); border-radius:10px; background:#fff}}
    textarea{{min-height:84px; resize:vertical}}
    .muted{{color:var(--muted)}} .ok{{color:var(--good)}} .warn{{color:var(--warn)}} .bad{{color:var(--bad)}}
    .pill{{display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; border:1px solid var(--border); background:#eef3f8; font-size:12px}}
    table{{width:100%; border-collapse:collapse}}
    th,td{{padding:8px 10px; border-top:1px solid #e7edf6; text-align:left; vertical-align:top}}
    th{{background:#f7fafc; color:#425466; font-size:12px; text-transform:uppercase; letter-spacing:.04em}}
    tr:first-child td{{border-top:none}}
    code{{white-space:pre-wrap; word-break:break-word}}
    @media(max-width:1100px){{ .hero{{grid-template-columns:1fr}} }}
  </style>
</head>
<body>
  <div class="wrap">
    {nav_html}
    <div class="toolbar">
      <button class="btn" onclick="reloadAll()">Reload</button>
      <label class="btn" for="backup_file">Backup importieren</label>
      <input id="backup_file" type="file" accept=".zip" style="display:none" onchange="importBackup(this.files[0])"/>
      <span id="status" class="muted">loading...</span>
    </div>

    <div class="grid hero">
      <div class="card">
        <h3 style="margin-top:0">Maschinenidentitaet / Registry</h3>
        <div class="row">
          <div class="field">
            <label>Seriennummer</label>
            <input id="machine_serial_number" placeholder="z.B. MAS004-001"/>
          </div>
          <div class="field" style="flex:1 1 320px">
            <label>Maschinenname</label>
            <input id="machine_name" placeholder="z.B. Roche Linie 1"/>
          </div>
          <div class="actions" style="padding-top:20px">
            <button class="btn" onclick="saveIdentity()">Speichern</button>
          </div>
        </div>
        <div id="pathInfo" class="muted" style="margin-top:10px"></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Neues Backup</h3>
        <div class="row">
          <div class="field">
            <label>Name</label>
            <input id="backup_name" placeholder="z.B. Formatfreigabe_2026-04-18"/>
          </div>
          <div class="field" style="flex:1 1 320px">
            <label>Notiz</label>
            <textarea id="backup_note" placeholder="Freitext fuer Zweck / Freigabe / Bemerkungen"></textarea>
          </div>
        </div>
        <div class="actions">
          <button class="btn primary" onclick="createBackup('settings')">Settings-Backup erstellen</button>
          <button class="btn" onclick="createBackup('full')">Vollbackup / Klonpaket erstellen</button>
        </div>
        <div class="muted" style="margin-top:10px">Vollbackups enthalten zusaetzlich Repo-/Software-Staende der gefundenen MAS-004-Repos als portables Klonpaket.</div>
      </div>
    </div>

    <div class="grid two" style="margin-top:12px">
      <div class="card">
        <h3 style="margin-top:0">Bestand</h3>
        <div id="countInfo" class="muted"></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Hinweis Klon / Bootstrap</h3>
        <div class="muted">Frische Maschinen ohne erreichbare Raspi-UI koennen ueber <code>scripts/mas004_machine_bootstrap.py</code> vorbereitet werden. Das Skript kann Vollbackups per SSH auf eine Zielmaschine uebertragen und dort als Startbasis ablegen.</div>
      </div>
    </div>

    <div class="card" style="margin-top:12px">
      <h3 style="margin-top:0">Backups</h3>
      <table>
        <thead>
          <tr><th>Typ</th><th>Name</th><th>Seriennr.</th><th>Zeit</th><th>Quelle</th><th>Groesse</th><th>Aktion</th></tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
<script>
const TOKEN_KEY = "mas004_ui_token";
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
function formatBytes(bytes){{ const v = Number(bytes || 0); if(v < 1024) return `${{v}} B`; if(v < 1024*1024) return `${{(v/1024).toFixed(1)}} KB`; return `${{(v/1024/1024).toFixed(2)}} MB`; }}

function renderOverview(data){{
  const id = data.identity || {{}};
  const counts = data.counts || {{}};
  document.getElementById("machine_serial_number").value = id.machine_serial_number || "";
  document.getElementById("machine_name").value = id.machine_name || "";
  document.getElementById("pathInfo").innerHTML = `<code>${{esc(data.paths?.backup_root || "-")}}</code>`;
  document.getElementById("countInfo").innerHTML = `Settings: <strong>${{esc(counts.settings || 0)}}</strong> | Full: <strong>${{esc(counts.full || 0)}}</strong>`;
  document.getElementById("rows").innerHTML = (data.backups || []).map(item => {{
    const restoreBtn = (item.backup_type === "settings" || item.backup_type === "full")
      ? `<button class="btn small" onclick="restoreBackup('${{item.backup_id}}')">Restore Settings</button>`
      : "";
    return `<tr>
      <td><span class="pill">${{esc(item.backup_type)}}</span></td>
      <td><strong>${{esc(item.name)}}</strong><div class="muted">${{esc(item.note || "")}}</div></td>
      <td>${{esc(item.machine_serial || "-")}}<div class="muted">${{esc(item.machine_name || "")}}</div></td>
      <td>${{esc(new Date((item.created_ts||0)*1000).toLocaleString())}}</td>
      <td>${{esc(item.source || "local")}}</td>
      <td>${{esc(formatBytes(item.size_bytes || 0))}}</td>
      <td>
        <div class="actions">
          <button class="btn small" onclick="downloadBackup('${{item.backup_id}}')">Download</button>
          ${{restoreBtn}}
          <button class="btn small" onclick="deleteBackup('${{item.backup_id}}')">Delete</button>
        </div>
      </td>
    </tr>`;
  }}).join("") || '<tr><td colspan="7" class="muted">Noch keine Backups</td></tr>';
}}

async function reloadAll(){{
  setStatus("loading...");
  const data = await api("/api/backups/overview");
  renderOverview(data);
  setStatus("ok");
}}

async function saveIdentity(){{
  setStatus("speichere Identitaet...");
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

async function createBackup(type){{
  const name = document.getElementById("backup_name").value.trim();
  if(!name){{ alert("Bitte zuerst einen Backup-Namen eingeben."); return; }}
  setStatus(`erstelle ${{type}} backup...`);
  await api("/api/backups/create", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{
      backup_type: type,
      name,
      note: document.getElementById("backup_note").value.trim()
    }})
  }});
  await reloadAll();
}}

function downloadBackup(backupId){{
  window.location.href = `/api/backups/${{encodeURIComponent(backupId)}}/download`;
}}

async function importBackup(file){{
  if(!file) return;
  setStatus("importiere Backup...");
  const fd = new FormData();
  fd.append("file", file);
  await api("/api/backups/import", {{method:"POST", body: fd}});
  document.getElementById("backup_file").value = "";
  await reloadAll();
}}

async function restoreBackup(backupId){{
  if(!confirm("Settings aus diesem Backup auf diese Maschine zurueckschreiben? Die aktuellen Werte werden zuvor automatisch als Settings-Backup gesichert.")) return;
  setStatus("restore laeuft...");
  const j = await api(`/api/backups/${{encodeURIComponent(backupId)}}/restore`, {{method:"POST"}});
  alert(j.message || "Restore abgeschlossen. Service-Neustart erforderlich.");
  await reloadAll();
}}

async function deleteBackup(backupId){{
  if(!confirm("Backup wirklich loeschen?")) return;
  setStatus("loesche Backup...");
  await api(`/api/backups/${{encodeURIComponent(backupId)}}`, {{method:"DELETE"}});
  await reloadAll();
}}

reloadAll().catch(err => setStatus(err.message));
</script>
</body>
</html>
"""
