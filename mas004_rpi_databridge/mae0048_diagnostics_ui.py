from __future__ import annotations


def build_mae0048_diagnostics_ui_html(nav_html: str) -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MAS-004 MAE0048 Diagnose</title>
  <style>
    :root{
      --bg:#f4f6f9; --card:#fff; --ink:#17202a; --muted:#607086; --line:#d9e1ec;
      --blue:#005eb8; --green:#237a44; --yellow:#9b6700; --red:#b42318;
      --soft-blue:#e8f1fb; --soft-green:#e4f6e9; --soft-yellow:#fff3cf; --soft-red:#fde7e7;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}
    .wrap{max-width:1680px;margin:0 auto;padding:16px}
    .topnav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
    .navbtn{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);text-decoration:none;font-weight:700}
    .navbtn.active{background:var(--blue);color:#fff;border-color:var(--blue)}
    .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
    .title{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}
    h1,h2,h3{margin:0} h1{font-size:25px} h2{font-size:18px} h3{font-size:14px}
    .muted{color:var(--muted)} .small{font-size:12px} .mono{font-family:Consolas,Menlo,monospace}
    .toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    button{min-height:36px;border:1px solid #abc3dc;border-radius:8px;background:#e8f0f8;color:#17324b;padding:7px 11px;font-weight:700;cursor:pointer}
    .pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;background:#eef3f8;padding:5px 9px;font-size:12px;font-weight:700;white-space:nowrap}
    .pill.ok{background:var(--soft-green);color:var(--green);border-color:#a9dfb8}
    .pill.warn{background:var(--soft-yellow);color:var(--yellow);border-color:#e3c66c}
    .pill.bad{background:var(--soft-red);color:var(--red);border-color:#efaaa4}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
    .wide{grid-column:1/-1}
    .kv{display:grid;grid-template-columns:170px 1fr;gap:7px 12px;align-items:start}
    .findings{display:grid;gap:8px;margin:0;padding:0;list-style:none}
    .finding{border:1px solid var(--line);border-left:5px solid var(--blue);border-radius:7px;padding:9px 10px;background:#fbfdff;font-weight:700}
    .finding.bad{border-left-color:var(--red);background:var(--soft-red)}
    .finding.warn{border-left-color:var(--yellow);background:var(--soft-yellow)}
    .bar{height:14px;border-radius:999px;background:#edf2f7;border:1px solid var(--line);overflow:hidden}
    .bar>span{display:block;height:100%;background:var(--blue);min-width:1px}
    .table-wrap{max-height:360px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fbfdff}
    table{width:100%;border-collapse:collapse} th,td{padding:7px 8px;border-bottom:1px solid #e7edf6;text-align:left;vertical-align:top;font-size:13px}
    th{color:#425466;background:#f7fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;z-index:1}
    pre{margin:0;max-height:420px;overflow:auto;background:#0f172a;color:#e2e8f0;border-radius:8px;padding:12px;font-size:12px}
    .section{margin-top:12px}
    @media(max-width:850px){.title{display:block}.toolbar{margin-top:10px}.kv{grid-template-columns:1fr}}
  </style>
</head>
<body>
<div class="wrap">
  __NAV__
  <section class="card">
    <div class="title">
      <div>
        <h1>MAE0048 Diagnose</h1>
        <div class="muted">Etikettenantrieb Stopptoleranz, Registration und Korrekturversuche.</div>
      </div>
      <div class="toolbar">
        <span id="status_pill" class="pill">lade...</span>
        <button onclick="loadAll()">Aktualisieren</button>
        <label class="pill"><input id="auto_refresh" type="checkbox" checked/>Auto</label>
      </div>
    </div>
    <ul id="findings" class="findings"></ul>
  </section>

  <div class="grid section">
    <section class="card">
      <h2>Registration</h2>
      <div id="registration_kv" class="kv" style="margin-top:10px"></div>
      <div class="muted small" style="margin:10px 0 4px">Abweichung zu Toleranz</div>
      <div class="bar"><span id="error_bar"></span></div>
    </section>
    <section class="card">
      <h2>Motor 3</h2>
      <div id="motor_kv" class="kv" style="margin-top:10px"></div>
    </section>
    <section class="card">
      <h2>Maschine / Parameter</h2>
      <div id="params_kv" class="kv" style="margin-top:10px"></div>
    </section>
    <section class="card">
      <h2>Wickler</h2>
      <div id="wickler_kv" class="kv" style="margin-top:10px"></div>
    </section>
  </div>

  <section class="card section">
    <div class="title"><h2>Korrekturversuche</h2><span id="attempt_count" class="pill">0</span></div>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Zeit ms</th><th>Restfehler mm</th><th>ID3-Befehl mm</th><th>Gesendet</th></tr></thead><tbody id="attempt_rows"></tbody></table></div>
  </section>

  <section class="card section">
    <div class="title"><h2>Relevante Logs</h2><span id="log_count" class="pill">0</span></div>
    <div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Kanal</th><th>Richtung</th><th>Meldung</th></tr></thead><tbody id="log_rows"></tbody></table></div>
  </section>

  <section class="card section">
    <div class="title"><h2>Rohdaten</h2><span id="updated_at" class="pill">-</span></div>
    <pre id="raw_json">{}</pre>
  </section>
</div>
<script>
let timer = null;
function esc(v){return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}
function fmt(v,d=3){const n=Number(v); return Number.isFinite(n) ? n.toFixed(d) : "-";}
function bool(v){return v===true || v===1 || v==="1";}
function pill(v, bad=false){return `<span class="pill ${bool(v)?(bad?"bad":"ok"):"warn"}">${bool(v)?"1":"0"}</span>`;}
async function api(path){const r=await fetch(path,{credentials:"same-origin"}); if(!r.ok)throw new Error(await r.text()); return r.json();}
function kv(id, rows){document.getElementById(id).innerHTML=rows.map(([k,v])=>`<div class="muted">${esc(k)}</div><div>${v}</div>`).join("");}
function rowValue(v){return v === undefined || v === null || v === "" ? "-" : esc(v);}
function renderFindings(payload){
  const items = payload.findings || [];
  document.getElementById("findings").innerHTML = items.map(text => {
    const lower = String(text).toLowerCase();
    const cls = lower.includes("mae0048") || lower.includes("fehler") || lower.includes("alarm") ? "bad" : (lower.includes("ausserhalb") || lower.includes("busy") || lower.includes("nicht ready") ? "warn" : "");
    return `<li class="finding ${cls}">${esc(text)}</li>`;
  }).join("") || '<li class="finding">Kein Befund im aktuellen Snapshot.</li>';
}
function renderRegistration(reg){
  const error = Math.abs(Number(reg.abs_error_mm || 0));
  const tolerance = Math.max(0.001, Number(reg.tolerance_mm || 0.05));
  const maxCorr = Math.max(tolerance, Number(reg.max_correction_mm || 5));
  const bar = document.getElementById("error_bar");
  bar.style.width = `${Math.min(100, (error / maxCorr) * 100)}%`;
  bar.style.background = error <= tolerance ? "var(--green)" : (error <= maxCorr ? "var(--yellow)" : "var(--red)");
  kv("registration_kv", [
    ["Grund", `<b>${rowValue(reg.reason)}</b>`],
    ["Label", rowValue(reg.label_no)],
    ["Phase / Running", `${rowValue(reg.phase)} / ${pill(reg.running)}`],
    ["Fehler", `<b>${fmt(reg.error_mm,4)} mm</b> (abs ${fmt(reg.abs_error_mm,4)} mm)`],
    ["Toleranz", `+/-${fmt(reg.tolerance_mm,4)} mm`],
    ["Korrekturfenster", `${fmt(reg.max_correction_mm,3)} mm`],
    ["Fortschritt / Ziel", `${fmt(reg.progressed_mm,3)} / ${fmt(reg.target_mm,3)} mm`],
    ["Restweg", `${fmt(reg.remaining_mm,3)} mm`],
    ["AZD Zielmodus", `${rowValue(reg.position_mode)} | Command ${pill(reg.position_commanded)}`],
    ["AZD Befehl", `${fmt(reg.position_command_mm,3)} mm`],
    ["Druckziel", `${fmt(reg.target_mm,3)} mm`],
    ["Infeed Speed", `${fmt(reg.infeed_speed_mm_s,3)} mm/s`],
    ["Drive Speed", `${fmt(reg.drive_speed_mm_s,3)} mm/s`],
    ["Motor busy/ready", `${pill(reg.motor_busy,true)} ${pill(reg.motor_ready)}`],
    ["Registration ready/late", `${pill(reg.registration_ready)} ${pill(reg.registration_late,true)}`],
    ["Print triggered/resolved", `${pill(reg.print_triggered)} ${pill(reg.print_resolved)}`],
    ["Counts", `In ${rowValue(reg.infeed_count)} / Drive ${rowValue(reg.drive_count)} / Target ${rowValue(reg.print_target_count)}`],
    ["Last error", rowValue(reg.last_error)]
  ]);
}
function renderMotor(m){
  kv("motor_kv", [
    ["Ready / Busy / Move", `${pill(m.ready)} ${pill(m.busy,true)} ${pill(m.move)}`],
    ["InPos / Alarm", `${pill(m.in_pos)} ${pill(m.alarm,true)}`],
    ["Alarmcode", rowValue(m.alarm_code)],
    ["Velocity Mode", pill(m.velocity_mode)],
    ["Target Speed", `${fmt(m.target_speed_mm_s,3)} mm/s`],
    ["Feedback / Command", `${rowValue(m.feedback_tenths_mm)} / ${rowValue(m.command_tenths_mm)} 1/10mm`],
    ["Step Error", `${rowValue(m.command_feedback_step_error)} Steps`],
    ["Error mm", `${fmt(m.command_feedback_error_mm,4)} mm`],
    ["Steps/mm", rowValue(m.steps_per_mm)],
    ["Invert / Zero", `${rowValue(m.invert_direction)} / ${rowValue(m.zero_offset_steps)}`],
    ["Last Reply", `<span class="mono small">${esc(m.last_reply || "-")}</span>`]
  ]);
}
function renderParams(params){
  kv("params_kv", [
    ["MAS0001", rowValue(params.MAS0001)],
    ["MAS0028", rowValue(params.MAS0028)],
    ["MAE0048", rowValue(params.MAE0048)],
    ["Label Soll", `${rowValue(params.MAP0002)} 1/10mm`],
    ["Druck Offset", `MAP0004=${rowValue(params.MAP0004)} / MAP0006=${rowValue(params.MAP0006)}`],
    ["Label MAE", `25=${rowValue(params.MAE0025)} 26=${rowValue(params.MAE0026)} 27=${rowValue(params.MAE0027)} 28=${rowValue(params.MAE0028)}`],
    ["Wickler MAE", `30=${rowValue(params.MAE0030)} 32=${rowValue(params.MAE0032)} 33=${rowValue(params.MAE0033)} 34=${rowValue(params.MAE0034)}`],
    ["MAP0014", rowValue(params.MAP0014)],
    ["MAP0016", rowValue(params.MAP0016)],
    ["MAP0018/19", `${rowValue(params.MAP0018)} / ${rowValue(params.MAP0019)}`],
    ["Bypass TTO/Laser", `${rowValue(params.MAP0069)} / ${rowValue(params.MAP0070)}`]
  ]);
}
function renderWicklers(w){
  function one(x){
    if(!x || !x.ok) return `<span class="pill bad">${esc(x?.error || "offline")}</span>`;
    return `${esc(x.mode || "-")} | Wippe ${fmt(x.wipe_percent,1)}% | stop ${Number(bool(x.external_stop))} | indexed ${Number(bool(x.indexed_mode))} | ready ${Number(bool(x.drive_ready))} | move ${Number(bool(x.drive_move))} | alarm ${Number(bool(x.drive_alarm))}`;
  }
  kv("wickler_kv", [["Abwickler", one(w.unwinder)], ["Aufwickler", one(w.rewinder)]]);
}
function renderAttempts(reg){
  const attempts = reg.attempts || [];
  const used = attempts.filter(a => Number(a.ms || 0) > 0 || Number(a.error_mm || 0) !== 0 || Number(a.command_mm || 0) !== 0 || bool(a.commanded));
  document.getElementById("attempt_count").textContent = `${reg.registration_attempts ?? used.length}/${reg.max_attempts ?? 3}`;
  document.getElementById("attempt_rows").innerHTML = attempts.length ? attempts.map(a => `
    <tr><td>${rowValue(a.index)}</td><td>${rowValue(a.ms)}</td><td>${fmt(a.error_mm,4)}</td><td>${fmt(a.command_mm,4)}</td><td>${pill(a.commanded)}</td></tr>
  `).join("") : '<tr><td colspan="5" class="muted">Noch keine Korrekturversuche im Snapshot.</td></tr>';
}
function renderLogs(logs){
  document.getElementById("log_count").textContent = String(logs.length || 0);
  document.getElementById("log_rows").innerHTML = logs.length ? logs.map(l => `
    <tr><td>${new Date(Number(l.ts || 0)*1000).toLocaleString()}</td><td>${rowValue(l.channel)}</td><td>${rowValue(l.direction)}</td><td><span class="mono small">${esc(l.message || "")}</span></td></tr>
  `).join("") : '<tr><td colspan="4" class="muted">Keine passenden Logzeilen gefunden.</td></tr>';
}
function render(payload){
  const pillEl = document.getElementById("status_pill");
  pillEl.className = `pill ${payload.ok ? "ok" : "bad"}`;
  pillEl.textContent = payload.ok ? "bereit" : "Diagnose mit Fehlern";
  renderFindings(payload);
  renderRegistration(payload.registration || {});
  renderMotor(payload.motor3 || {});
  renderParams(payload.params || {});
  renderWicklers(payload.wicklers || {});
  renderAttempts(payload.registration || {});
  renderLogs(payload.logs || []);
  document.getElementById("updated_at").textContent = new Date(Number(payload.ts || Date.now()/1000)*1000).toLocaleTimeString();
  document.getElementById("raw_json").textContent = JSON.stringify(payload, null, 2);
}
async function loadAll(){
  try{
    const payload = await api("/api/machine/mae0048-diagnostics");
    render(payload);
  }catch(err){
    document.getElementById("status_pill").className = "pill bad";
    document.getElementById("status_pill").textContent = "Fehler";
    document.getElementById("findings").innerHTML = `<li class="finding bad">${esc(err.message || err)}</li>`;
  }
}
function schedule(){
  if(timer) clearInterval(timer);
  timer = setInterval(() => { if(document.getElementById("auto_refresh").checked) loadAll(); }, 2000);
}
document.getElementById("auto_refresh").addEventListener("change", schedule);
loadAll(); schedule();
</script>
</body>
</html>
""".replace("__NAV__", nav_html)
