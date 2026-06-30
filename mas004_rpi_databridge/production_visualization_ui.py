from __future__ import annotations


def build_production_visualization_ui_html(nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>MAS-004 Produktionsvisualisierung</title>
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
    .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
    .title{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}}
    h1,h2,h3{{margin:0}} h1{{font-size:25px}} h2{{font-size:18px}} h3{{font-size:14px}}
    .muted{{color:var(--muted)}} .small{{font-size:12px}} .mono{{font-family:Consolas,Menlo,monospace}}
    .toolbar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
    button,.btn{{min-height:36px;border:1px solid #abc3dc;border-radius:8px;background:#e8f0f8;color:#17324b;padding:7px 11px;font-weight:700;cursor:pointer;text-decoration:none}}
    .pill{{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;background:#eef3f8;padding:5px 9px;font-size:12px;font-weight:700;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .pill.ok{{background:var(--soft-green);color:var(--green);border-color:#a9dfb8}}
    .pill.warn{{background:var(--soft-yellow);color:var(--yellow);border-color:#e3c66c}}
    .pill.bad{{background:var(--soft-red);color:var(--red);border-color:#efaaa4}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
    .kv{{display:grid;grid-template-columns:150px 1fr;gap:7px 12px;align-items:start}}
    .track-card{{margin-top:12px;overflow:hidden}}
    .track-head{{display:grid;grid-template-columns:110px 1fr 90px;gap:10px;align-items:center;margin-bottom:8px}}
    .track-shell{{position:relative;height:330px;border:1px solid var(--line);border-radius:10px;background:#f9fbfe;overflow-x:auto;overflow-y:hidden}}
    .track{{position:relative;height:100%;min-width:980px}}
    .axis{{position:absolute;left:0;right:0;top:186px;height:10px;border-radius:999px;background:#d8e2ef}}
    .component{{position:absolute;top:26px;width:2px;height:230px;background:#9cadc4}}
    .component .tag{{position:absolute;top:-20px;left:50%;transform:translateX(-50%);white-space:nowrap;background:#fff;border:1px solid var(--line);border-radius:8px;padding:4px 7px;font-size:12px;font-weight:700;color:#334155}}
    .component .mm{{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:11px;color:var(--muted);white-space:nowrap}}
    .component.detect{{background:#475569}} .component.material{{background:var(--cyan)}} .component.print{{background:var(--blue)}} .component.verify{{background:#7c3aed}} .component.control{{background:#b45309}} .component.exit{{background:var(--red)}}
    .label-bar{{position:absolute;height:34px;border-radius:7px;border:1px solid rgba(15,23,42,.22);background:#cbd5e1;box-shadow:0 1px 3px rgba(15,23,42,.12);display:flex;align-items:center;gap:6px;padding:0 8px;font-size:12px;font-weight:800;overflow:hidden;white-space:nowrap}}
    .label-bar.ok{{background:#bdeec9;color:#14542d;border-color:#78c48e}}
    .label-bar.warn{{background:#fff0b8;color:#6d4800;border-color:#e3c66c}}
    .label-bar.bad{{background:#ffc9c5;color:#8a1c15;border-color:#efaaa4}}
    .label-bar.open{{background:#dbeafe;color:#08345f;border-color:#8dbce8}}
    .label-line{{position:absolute;left:0;right:0;height:1px;background:#e5edf6}}
    .table-wrap{{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:#fbfdff}}
    table{{width:100%;border-collapse:collapse}} th,td{{padding:7px 8px;border-bottom:1px solid #e7edf6;text-align:left;vertical-align:top;font-size:13px}}
    th{{color:#425466;background:#f7fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;z-index:1}}
    .flag-grid{{display:flex;flex-wrap:wrap;gap:5px}}
    .flag{{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px 7px;font-size:11px;font-weight:700;background:#f8fafc;color:#334155}}
    .flag.on{{background:var(--soft-green);border-color:#a9dfb8;color:#14542d}}
    .flag.bad{{background:var(--soft-red);border-color:#efaaa4;color:#8a1c15}}
    .panel{{display:grid;grid-template-columns:1.1fr .9fr;gap:12px;margin-top:12px}}
    @media(max-width:1100px){{.panel{{grid-template-columns:1fr}} .track-head{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<div class="wrap">
  {nav_html}
  <section class="card">
    <div class="title">
      <div>
        <h1>Produktionsvisualisierung</h1>
        <div class="muted">Labelpfad, Komponentenpositionen und Schieberegisterdaten.</div>
      </div>
      <div class="toolbar">
        <span id="state_pill" class="pill">lade...</span>
        <button onclick="loadAll()">Aktualisieren</button>
        <label class="pill"><input id="auto_refresh" type="checkbox" checked/>Auto</label>
      </div>
    </div>
    <div class="grid">
      <div class="card" style="box-shadow:none"><h3>Maschine</h3><div id="machine_kv" class="kv" style="margin-top:10px"></div></div>
      <div class="card" style="box-shadow:none"><h3>Transport</h3><div id="transport_kv" class="kv" style="margin-top:10px"></div></div>
      <div class="card" style="box-shadow:none"><h3>Label</h3><div id="label_kv" class="kv" style="margin-top:10px"></div></div>
      <div class="card" style="box-shadow:none">
        <div class="title" style="margin-bottom:0"><h3>LED-Streifen</h3><span id="led_test_status" class="pill">bereit</span></div>
        <div class="toolbar" style="margin-top:10px">
          <button id="led_test_start" onclick="startLedTest()">Controller Rot</button>
          <button id="led_test_stop" onclick="stopLedTest()">Stop</button>
        </div>
        <div id="led_kv" class="kv" style="margin-top:10px"></div>
      </div>
    </div>
  </section>

  <section class="card track-card">
    <div class="track-head">
      <span class="pill" id="track_scale">0 mm</span>
      <div class="muted small" id="track_note">lade...</div>
      <span class="pill" id="updated_at">-</span>
    </div>
    <div class="track-shell"><div id="track" class="track"></div></div>
  </section>

  <div class="panel">
    <section class="card">
      <div class="title"><h2>Aktive Labels</h2><span id="active_count" class="pill">0</span></div>
      <div class="table-wrap"><table><thead><tr><th>Label</th><th>Position</th><th>Status</th><th>Register</th></tr></thead><tbody id="active_rows"></tbody></table></div>
    </section>
    <section class="card">
      <div class="title"><h2>Abgeschlossene Labels</h2><span id="history_count" class="pill">0</span></div>
      <div class="table-wrap"><table><thead><tr><th>Label</th><th>Laenge</th><th>Ergebnis</th><th>Payload</th></tr></thead><tbody id="history_rows"></tbody></table></div>
    </section>
  </div>
</div>
<script>
let timer = null;
function esc(v){{return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}}
function fmt(v,d=1){{const n=Number(v); return Number.isFinite(n) ? n.toFixed(d) : "-";}}
function bool(v){{return v===true || v===1 || v==="1";}}
async function api(path){{const r=await fetch(path,{{credentials:"same-origin"}}); if(!r.ok)throw new Error(await r.text()); return r.json();}}
async function postApi(path, body){{const r=await fetch(path,{{method:"POST",credentials:"same-origin",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(body||{{}})}}); if(!r.ok)throw new Error(await r.text()); return r.json();}}
function kv(id, rows){{document.getElementById(id).innerHTML=rows.map(([k,v])=>`<div class="muted">${{esc(k)}}</div><div>${{v}}</div>`).join("");}}
function flag(name, value, bad=false){{return `<span class="flag ${{bool(value)?(bad?"bad":"on"):""}}">${{esc(name)}} ${{bool(value)?"1":"0"}}</span>`;}}
function flags(label){{
  return `<div class="flag-grid">
    ${{flag("Material trig", label.material_triggered)}}${{flag("Material ok", label.material_ok, !bool(label.material_ok))}}${{flag("Material bypass", label.material_bypass)}}
    ${{flag("Print trig", label.print_triggered)}}${{flag("Print ok", label.print_ok, !bool(label.print_ok))}}${{flag("Print bypass", label.print_bypass)}}
    ${{flag("Verify trig", label.verify_triggered)}}${{flag("Verify ok", label.verify_ok, !bool(label.verify_ok))}}${{flag("Verify bypass", label.verify_bypass)}}
    ${{flag("Control", label.control_seen)}}${{flag("Short", label.length_too_short, true)}}${{flag("Long", label.length_too_long, true)}}${{flag("Reg late", label.registration_late, true)}}
  </div>`;
}}
function labelClass(label){{
  if(bool(label.length_too_short)||bool(label.length_too_long)||bool(label.registration_late)||!bool(label.material_ok)||!bool(label.print_ok)||!bool(label.verify_ok)) return "bad";
  if(bool(label.open)) return "open";
  if(bool(label.control_seen)) return "ok";
  return "warn";
}}
function stageText(label){{
  if(bool(label.open)) return "offen";
  if(!bool(label.material_triggered)) return "erfasst";
  if(!bool(label.print_triggered)) return "Material";
  if(!bool(label.verify_triggered)) return "Druck";
  if(!bool(label.control_seen)) return "Verify";
  return "Kontrolle";
}}
function renderTrack(payload){{
  const track = document.getElementById("track");
  const t = payload.track || {{}};
  const components = t.components || [];
  const labels = payload.active_labels || [];
  const maxMm = Math.max(Number(t.length_mm || 0), ...components.map(c=>Number(c.mm||0)), ...labels.map(l=>Number(l.lead_mm||0)), 1000);
  const scale = 100 / maxMm;
  const rows = Math.max(4, labels.length || 1);
  track.style.height = `${{Math.max(330, 118 + rows*48)}}px`;
  document.getElementById("track_scale").textContent = `${{fmt(maxMm,0)}} mm`;
  document.getElementById("track_note").textContent = `${{labels.length}} aktive Labels, ${{components.length}} Komponenten`;
  let html = `<div class="axis"></div>`;
  for(let r=0;r<rows;r++) html += `<div class="label-line" style="top:${{86+r*48}}px"></div>`;
  components.forEach(c => {{
    const left = Math.max(0, Math.min(100, Number(c.mm||0)*scale));
    html += `<div class="component ${{esc(c.kind||"")}}" style="left:${{left}}%"><div class="tag">${{esc(c.label)}}</div><div class="mm">${{fmt(c.mm,1)}} mm</div></div>`;
  }});
  labels.forEach((l, idx) => {{
    const lead = Math.max(0, Number(l.lead_mm||0));
    const tail = Math.max(0, Number(l.tail_mm ?? lead));
    const leftMm = Math.min(tail, lead);
    const widthMm = Math.max(8, Math.abs(lead-tail), Number(l.measured_length_mm||0), Number((payload.format||{{}}).label_length_mm||80));
    const left = Math.max(0, Math.min(100, leftMm*scale));
    const width = Math.max(1.4, Math.min(100-left, widthMm*scale));
    const top = 72 + idx*48;
    html += `<div class="label-bar ${{labelClass(l)}}" style="left:${{left}}%;width:${{width}}%;top:${{top}}px">#${{esc(l.no)}} · ${{esc(stageText(l))}} · ${{fmt(l.lead_mm,1)}} mm</div>`;
  }});
  track.innerHTML = html;
}}
function renderRows(payload){{
  const labels = payload.active_labels || [];
  document.getElementById("active_count").textContent = String(labels.length);
  document.getElementById("active_rows").innerHTML = labels.length ? labels.map(l => `
    <tr>
      <td><b>#${{esc(l.no)}}</b><br/><span class="muted small">${{bool(l.open)?"offen":"geschlossen"}}</span></td>
      <td>Lead ${{fmt(l.lead_mm,1)}} mm<br/>Tail ${{fmt(l.tail_mm,1)}} mm<br/>Laenge ${{fmt(l.measured_length_mm,1)}} mm</td>
      <td><span class="pill ${{labelClass(l)}}">${{esc(stageText(l))}}</span><br/>Printfehler ${{fmt(l.print_error_mm,3)}} mm<br/>Korrekturen ${{esc(l.print_corrections ?? l.registration_attempts ?? 0)}}</td>
      <td>${{flags(l)}}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="muted">Keine aktiven Labels im ESP-Schieberegister.</td></tr>';
  const hist = payload.completed_labels || [];
  document.getElementById("history_count").textContent = String(hist.length);
  document.getElementById("history_rows").innerHTML = hist.length ? hist.map(item => {{
    const p = item.payload || {{}};
    const ok = bool(item.production_ok);
    return `<tr>
      <td><b>#${{esc(item.label_no)}}</b><br/><span class="muted small">${{esc(item.production_label||"")}}</span></td>
      <td>${{fmt(p.measured_length_mm,1)}} mm<br/><span class="muted small">Start ${{fmt(p.zero_mm,1)}} / Exit ${{fmt(p.exit_mm,1)}}</span></td>
      <td><span class="pill ${{ok?"ok":"bad"}}">${{ok?"OK":"NOK"}}</span><br/>Material ${{Number(item.material_ok)}} · Print ${{Number(item.print_ok)}} · Verify ${{Number(item.verify_ok)}} · Removed ${{Number(item.removed)}}</td>
      <td><code class="small">${{esc(JSON.stringify(p))}}</code></td>
    </tr>`;
  }}).join("") : '<tr><td colspan="4" class="muted">Noch keine abgeschlossenen Labels in der Datenbank.</td></tr>';
}}
function render(payload){{
  const machine = payload.machine || {{}};
  const esp = payload.esp_snapshot || {{}};
  const prod = esp.production || {{}};
  const fmtData = payload.format || {{}};
  const led = payload.led || {{}};
  const ledTest = esp.led_test || {{}};
  const pill = document.getElementById("state_pill");
  pill.className = `pill ${{payload.ok ? "ok" : "bad"}}`;
  pill.textContent = machine.current_state_label || "unbekannt";
  kv("machine_kv", [
    ["Status", `${{esc(machine.current_state)}} · ${{esc(machine.current_state_label)}}`],
    ["Auftrag", esc(machine.production_label || "-")],
    ["ESP", payload.esp_error ? `<span class="pill bad">${{esc(payload.esp_error)}}</span>` : '<span class="pill ok">verbunden</span>'],
    ["Letztes Label", esc(machine.last_label_no ?? "-")]
  ]);
  kv("transport_kv", [
    ["Infeed", `${{fmt(esp.infeed_mm,3)}} mm`],
    ["Drive", `${{fmt(esp.drive_mm,3)}} mm`],
    ["Speed", `${{fmt(esp.infeed_speed_mm_s,2)}} mm/s`],
    ["Production", `${{bool(prod.running)?"running":"idle"}} · Phase ${{esc(prod.phase ?? "-")}}`]
  ]);
  kv("label_kv", [
    ["Soll", `${{fmt(fmtData.label_length_mm,1)}} mm ± ${{fmt(fmtData.label_tolerance_mm,1)}}`],
    ["Aktiv", esc((payload.active_labels||[]).length)],
    ["Erfasst", esc(prod.label_start_seen ? "ja" : "nein")],
    ["Fehler", `${{bool(esp.faults?.label_short)?"zu kurz ":""}}${{bool(esp.faults?.label_long)?"zu lang ":""}}${{bool(esp.faults?.sensor_fault)?"Sensor ":""}}` || "-"]
  ]);
  kv("led_kv", [
    ["Offset", `${{fmt(led.offset_mm,1)}} mm`],
    ["Laenge", `${{fmt(led.length_mm,1)}} mm aktiv / ${{fmt(led.physical_length_mm,1)}} mm phys.`],
    ["Pitch", `${{fmt(led.pitch_mm,2)}} mm`],
    ["Pixel", `${{esc(led.count ?? "-")}} / ${{esc(led.max_count ?? "-")}}`],
    ["Controller", `${{esc(led.pin_label || "Externer Controller")}}`],
    ["Ziel", `${{bool(led.controller_enabled) ? '<span class="pill ok">aktiv</span>' : '<span class="pill warn">aus</span>'}} ${{esc(led.controller_target || "-")}}`],
    ["Protokoll", `${{esc(led.protocol || "MAS004-LED-UDP/v1")}}`],
    ["Quelle", `${{esc(led.controller_source || "ESP32-PLC")}}`],
    ["Hinweis", led.warning ? `<span class="pill warn">${{esc(led.warning)}}</span>` : "-"],
    ["Test", bool(ledTest.running) ? (bool(ledTest.permanent) ? `${{esc(ledTest.mode || "aktiv")}} permanent` : `aktiv ${{esc(ledTest.remaining_ms ?? 0)}} ms`) : "inaktiv"]
  ]);
  document.getElementById("updated_at").textContent = new Date().toLocaleTimeString();
  renderTrack(payload);
  renderRows(payload);
}}
function setLedTestStatus(text, kind=""){{
  const status = document.getElementById("led_test_status");
  const msg = String(text || "");
  status.className = `pill ${{kind}}`;
  status.textContent = msg.length > 90 ? `${{msg.slice(0, 87)}}...` : msg;
}}
async function startLedTest(){{
  const start = document.getElementById("led_test_start");
  const stop = document.getElementById("led_test_stop");
  start.disabled = true;
  stop.disabled = true;
  setLedTestStatus("rot startet", "warn");
  try{{
    await postApi("/api/machine/led-test", {{action:"red", duration_ms:0}});
    setLedTestStatus("rot permanent", "ok");
    await loadAll();
  }}catch(err){{
    setLedTestStatus(err.message, "bad");
  }}finally{{
    start.disabled = false;
    stop.disabled = false;
  }}
}}
async function stopLedTest(){{
  const start = document.getElementById("led_test_start");
  const stop = document.getElementById("led_test_stop");
  start.disabled = true;
  stop.disabled = true;
  setLedTestStatus("stoppt", "warn");
  try{{
    await postApi("/api/machine/led-test", {{action:"stop"}});
    setLedTestStatus("bereit");
  }}catch(err){{
    setLedTestStatus(err.message, "bad");
  }}finally{{
    start.disabled = false;
    stop.disabled = false;
  }}
}}
async function loadAll(){{
  try{{
    const payload = await api("/api/machine/production-visualization");
    render(payload);
  }}catch(err){{
    const pill = document.getElementById("state_pill");
    pill.className = "pill bad";
    pill.textContent = err.message;
  }}
}}
function schedule(){{
  if(timer) clearInterval(timer);
  timer = setInterval(()=>{{ if(!document.hidden && document.getElementById("auto_refresh").checked) loadAll(); }}, 1500);
}}
document.getElementById("auto_refresh").addEventListener("change", schedule);
schedule();
loadAll();
</script>
</body>
</html>
"""
