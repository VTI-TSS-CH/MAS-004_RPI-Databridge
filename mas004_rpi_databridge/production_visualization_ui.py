from __future__ import annotations


def build_production_visualization_ui_html(nav_html: str) -> str:
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MAS-004 Produktionsvisualisierung</title>
  <style>
    :root{
      --bg:#f4f6f9; --surface:#fff; --ink:#17202a; --muted:#607086; --line:#d9e1ec;
      --blue:#005eb8; --green:#237a44; --yellow:#9b6700; --red:#b42318; --cyan:#087f8c;
      --violet:#6d28d9; --orange:#b45309; --soft-blue:#e8f1fb; --soft-green:#e4f6e9;
      --soft-yellow:#fff3cf; --soft-red:#fde7e7;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}
    .wrap{max-width:1760px;margin:0 auto;padding:16px}
    .topnav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
    .navbtn{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);text-decoration:none;font-weight:700}
    .navbtn.active{background:var(--blue);color:#fff;border-color:var(--blue)}
    .surface{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
    .title{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}
    h1,h2,h3{margin:0} h1{font-size:25px} h2{font-size:18px} h3{font-size:14px}
    .muted{color:var(--muted)} .small{font-size:12px} .mono{font-family:Consolas,Menlo,monospace}
    .toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    button,.btn{min-height:36px;border:1px solid #abc3dc;border-radius:8px;background:#e8f0f8;color:#17324b;padding:7px 11px;font-weight:700;cursor:pointer;text-decoration:none}
    button:disabled{opacity:.55;cursor:wait}
    .pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;background:#eef3f8;padding:5px 9px;font-size:12px;font-weight:700;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .pill.ok{background:var(--soft-green);color:var(--green);border-color:#a9dfb8}
    .pill.warn{background:var(--soft-yellow);color:var(--yellow);border-color:#e3c66c}
    .pill.bad{background:var(--soft-red);color:var(--red);border-color:#efaaa4}
    .pill.open{background:var(--soft-blue);color:#08345f;border-color:#8dbce8}
    .metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(265px,1fr));gap:12px}
    .metric{border:1px solid #dfe7f2;border-radius:8px;padding:12px;background:#fbfdff;min-height:146px}
    .kv{display:grid;grid-template-columns:136px 1fr;gap:7px 12px;align-items:start;margin-top:10px}
    .track-card{margin-top:12px;overflow:hidden}
    .track-head{display:grid;grid-template-columns:190px 1fr 100px;gap:10px;align-items:center;margin-bottom:8px}
    .track-note{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .track-shell{position:relative;border:1px solid var(--line);border-radius:8px;background:#f8fbff;overflow:auto;min-height:520px;max-height:780px}
    .track{position:relative;min-width:1180px;background:linear-gradient(180deg,#fbfdff 0,#fbfdff 282px,#f3f7fc 282px,#f3f7fc 100%)}
    .track-ruler{position:absolute;left:0;right:0;top:28px;height:28px;border-top:1px solid #cfd9e6;border-bottom:1px solid #e2e9f2;background:#fff}
    .tick{position:absolute;top:28px;width:1px;height:28px;background:#cbd6e4}
    .tick.major{background:#8fa2b8}
    .tick-label{position:absolute;top:60px;transform:translateX(-50%);font-size:11px;color:#50637a;white-space:nowrap}
    .rail{position:absolute;left:0;right:0;top:226px;height:18px;border-radius:999px;background:#d8e2ef;border:1px solid #c6d4e4}
    .rail:after{content:"";position:absolute;right:10px;top:4px;width:8px;height:8px;border-top:2px solid #63758b;border-right:2px solid #63758b;transform:rotate(45deg)}
    .component-line{position:absolute;top:94px;width:2px;background:#9cadc4;opacity:.72}
    .component-pin{position:absolute;top:218px;transform:translateX(-50%);width:10px;height:10px;border-radius:999px;background:#fff;border:2px solid currentColor;z-index:4}
    .component-tag{position:absolute;text-align:center;background:#fff;border:1px solid var(--line);border-radius:7px;padding:4px 7px;font-size:11px;font-weight:800;color:#334155;line-height:1.15;box-shadow:0 1px 2px rgba(15,23,42,.08);z-index:5;white-space:normal;overflow:hidden}
    .component-tag.editable{cursor:pointer;border-color:#9bb9d9}
    .component-tag.editable:hover{box-shadow:0 2px 6px rgba(0,94,184,.18);border-color:#6fa0d4}
    .component-mm{display:block;margin-top:2px;font-size:10px;font-weight:600;color:var(--muted);white-space:nowrap}
    .component-detect{color:#475569}.component-material{color:var(--cyan)}.component-print{color:var(--blue)}.component-verify{color:var(--violet)}.component-control{color:var(--orange)}.component-exit{color:var(--red)}
    .lane{position:absolute;left:0;right:0;height:44px;border-top:1px solid #e2e9f2;background:rgba(255,255,255,.46)}
    .lane.alt{background:rgba(232,241,251,.35)}
    .lane-no{position:absolute;left:8px;font-size:11px;font-weight:800;color:#51647b;background:#fff;border:1px solid #d7e0eb;border-radius:999px;padding:2px 7px;z-index:2}
    .label-bar{position:absolute;height:31px;border-radius:6px;border:1px solid rgba(15,23,42,.24);background:#cbd5e1;box-shadow:0 1px 3px rgba(15,23,42,.13);display:grid;grid-template-columns:auto 1fr;align-items:center;gap:7px;padding:0 8px;font-size:12px;font-weight:800;overflow:hidden;white-space:nowrap;z-index:3}
    .label-bar:before,.label-bar:after{content:"";position:absolute;top:4px;bottom:4px;width:2px;border-radius:2px;background:rgba(15,23,42,.42)}
    .label-bar:before{left:3px}.label-bar:after{right:3px}
    .label-id{font-family:Consolas,Menlo,monospace;font-size:12px}
    .label-stage{overflow:hidden;text-overflow:ellipsis}
    .label-mm{display:none}
    .label-bar.ok{background:#bdeec9;color:#14542d;border-color:#78c48e}
    .label-bar.warn{background:#fff0b8;color:#6d4800;border-color:#e3c66c}
    .label-bar.bad{background:#ffc9c5;color:#8a1c15;border-color:#efaaa4}
    .label-bar.open{background:#dbeafe;color:#08345f;border-color:#8dbce8}
    .label-bar.clipped-left{border-left-style:dashed}.label-bar.clipped-right{border-right-style:dashed}
    .track-empty{position:absolute;left:18px;right:18px;top:304px;border:1px dashed #cbd6e4;border-radius:8px;padding:16px;color:var(--muted);background:#fff}
    .legend-dot{width:9px;height:9px;border-radius:999px;background:currentColor;display:inline-block}
    .panel{display:grid;grid-template-columns:1.15fr .85fr;gap:12px;margin-top:12px}
    .table-wrap{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:#fbfdff}
    table{width:100%;border-collapse:collapse} th,td{padding:7px 8px;border-bottom:1px solid #e7edf6;text-align:left;vertical-align:top;font-size:13px}
    th{color:#425466;background:#f7fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;z-index:1}
    .flag-grid{display:flex;flex-wrap:wrap;gap:5px}
    .flag{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px 7px;font-size:11px;font-weight:700;background:#f8fafc;color:#334155}
    .flag.on{background:var(--soft-green);border-color:#a9dfb8;color:#14542d}
    .flag.bad{background:var(--soft-red);border-color:#efaaa4;color:#8a1c15}
    @media(max-width:1100px){.panel{grid-template-columns:1fr}.track-head{grid-template-columns:1fr}.kv{grid-template-columns:120px 1fr}}
  </style>
</head>
<body>
<div class="wrap">
  __NAV_HTML__
  <section class="surface">
    <div class="title">
      <div>
        <h1>Produktionsvisualisierung</h1>
      </div>
      <div class="toolbar">
        <span id="state_pill" class="pill">lade...</span>
        <button onclick="loadAll()">Aktualisieren</button>
        <label class="pill"><input id="auto_refresh" type="checkbox" checked/>Auto</label>
      </div>
    </div>
    <div class="metric-grid">
      <div class="metric"><h3>Maschine</h3><div id="machine_kv" class="kv"></div></div>
      <div class="metric"><h3>Transport</h3><div id="transport_kv" class="kv"></div></div>
      <div class="metric"><h3>Label</h3><div id="label_kv" class="kv"></div></div>
      <div class="metric">
        <div class="title" style="margin-bottom:0"><h3>LED-Streifen</h3><span id="led_test_status" class="pill">bereit</span></div>
        <div class="toolbar" style="margin-top:10px">
          <button id="led_test_start" onclick="startLedTest()">Controller Rot</button>
          <button id="led_test_stop" onclick="stopLedTest()">Stop</button>
        </div>
        <div id="led_kv" class="kv"></div>
      </div>
    </div>
  </section>

  <section class="surface track-card">
    <div class="track-head">
      <span class="pill" id="track_scale">0 mm</span>
      <div class="track-note" id="track_note">lade...</div>
      <span class="pill" id="updated_at">-</span>
    </div>
    <div class="track-shell"><div id="track" class="track"></div></div>
  </section>

  <div class="panel">
    <section class="surface">
      <div class="title"><h2>Aktive Labels</h2><span id="active_count" class="pill">0</span></div>
      <div class="table-wrap"><table><thead><tr><th>Label</th><th>Position</th><th>Status</th><th>Register</th></tr></thead><tbody id="active_rows"></tbody></table></div>
    </section>
    <section class="surface">
      <div class="title"><h2>Abgeschlossene Labels</h2><span id="history_count" class="pill">0</span></div>
      <div class="table-wrap"><table><thead><tr><th>Label</th><th>Laenge</th><th>Ergebnis</th><th>Register</th></tr></thead><tbody id="history_rows"></tbody></table></div>
    </section>
  </div>
</div>
<script>
let timer = null;
const componentColors = {detect:"#475569", material:"#087f8c", print:"#005eb8", verify:"#6d28d9", control:"#b45309", exit:"#b42318"};
function esc(v){return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");}
function fmt(v,d=1){const n=Number(v); return Number.isFinite(n) ? n.toFixed(d) : "-";}
function bool(v){return v===true || v===1 || v==="1";}
async function api(path){const r=await fetch(path,{credentials:"same-origin"}); if(!r.ok)throw new Error(await r.text()); return r.json();}
async function postApi(path, body){const r=await fetch(path,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})}); if(!r.ok)throw new Error(await r.text()); return r.json();}
function kv(id, rows){document.getElementById(id).innerHTML=rows.map(([k,v])=>`<div class="muted">${esc(k)}</div><div>${v}</div>`).join("");}
function flag(name, value, bad=false){return `<span class="flag ${bool(value)?(bad?"bad":"on"):""}">${esc(name)} ${bool(value)?"1":"0"}</span>`;}
function flags(label){
  return `<div class="flag-grid">
    ${flag("Material trig", label.material_triggered)}${flag("Material ok", label.material_ok, !bool(label.material_ok))}${flag("Material bypass", label.material_bypass)}
    ${flag("Print trig", label.print_triggered)}${flag("Print ok", label.print_ok, !bool(label.print_ok))}${flag("Print bypass", label.print_bypass)}
    ${flag("Verify trig", label.verify_triggered)}${flag("Verify ok", label.verify_ok, !bool(label.verify_ok))}${flag("Verify bypass", label.verify_bypass)}
    ${flag("Control", label.control_seen)}${flag("Short", label.length_too_short, true)}${flag("Long", label.length_too_long, true)}${flag("Reg late", label.registration_late, true)}
  </div>`;
}
function labelClass(label){
  if(bool(label.length_too_short)||bool(label.length_too_long)||bool(label.registration_late)||!bool(label.material_ok)||!bool(label.print_ok)||!bool(label.verify_ok)) return "bad";
  if(bool(label.open)) return "open";
  if(bool(label.control_seen)) return "ok";
  return "warn";
}
function stageText(label){
  if(bool(label.open)) return "offen";
  if(!bool(label.material_triggered)) return "erfasst";
  if(!bool(label.print_triggered)) return "Material";
  if(!bool(label.verify_triggered)) return "Druck";
  if(!bool(label.control_seen)) return "Verify";
  return "Kontrolle";
}
function componentKind(kind){
  const k = String(kind || "detect");
  return ["detect","material","print","verify","control","exit"].includes(k) ? k : "detect";
}
function niceStep(rangeMm){
  if(rangeMm <= 500) return 50;
  if(rangeMm <= 1200) return 100;
  if(rangeMm <= 2500) return 200;
  return 500;
}
function renderTrack(payload){
  const track = document.getElementById("track");
  const t = payload.track || {};
  const components = [...(t.components || [])].sort((a,b)=>Number(a.mm||0)-Number(b.mm||0));
  const formatLength = Math.max(20, Number((payload.format||{}).label_length_mm || 100));
  const labels = [...(payload.active_labels || [])].map(l => {
    const lead = Number(l.lead_mm || 0);
    const measured = Number(l.measured_length_mm || 0);
    const width = Math.max(8, measured > 0 ? measured : formatLength);
    const tailValue = Number(l.tail_mm);
    const tail = Number.isFinite(tailValue) ? tailValue : lead - width;
    return {...l, _lead:lead, _tail:tail, _width:Math.max(8, Math.abs(lead - tail), width)};
  }).sort((a,b)=>Math.min(a._tail,a._lead)-Math.min(b._tail,b._lead));
  const allMm = [0, Number(t.length_mm || 0), ...components.map(c=>Number(c.mm||0)), ...labels.flatMap(l=>[Number(l._lead||0), Number(l._tail||0)])].filter(Number.isFinite);
  const rawMin = Math.min(...allMm, 0);
  const rawMax = Math.max(...allMm, 1000);
  const step = niceStep(rawMax - rawMin);
  const minMm = Math.floor(Math.min(0, rawMin - step * 0.5) / step) * step;
  const maxMm = Math.ceil(Math.max(rawMax + step * 0.6, Number(t.length_mm || 0), 1000) / step) * step;
  const range = Math.max(1, maxMm - minMm);
  const pxPerMm = range <= 1200 ? 1.08 : range <= 2200 ? 0.90 : 0.68;
  const widthPx = Math.max(1180, Math.round(range * pxPerMm));
  const toPx = mm => (Number(mm) - minMm) * pxPerMm;
  const clampPx = (px, min=0, max=widthPx) => Math.max(min, Math.min(max, px));
  const labelRects = labels.map(l => {
    const rawLeft = toPx(Math.min(l._tail, l._lead));
    const rawRight = toPx(Math.max(l._tail, l._lead));
    const naturalWidth = Math.max(92, rawRight - rawLeft);
    const width = Math.min(Math.max(naturalWidth, 96), Math.max(96, widthPx - 16));
    const left = clampPx(rawLeft, 4, Math.max(4, widthPx - width - 4));
    return {...l, _leftPx:left, _rightPx:left + width, _barWidthPx:width, _clippedLeft:rawLeft < 0, _clippedRight:rawRight > widthPx};
  });
  const laneEnds = [];
  labelRects.forEach(l => {
    let lane = laneEnds.findIndex(end => l._leftPx > end + 10);
    if(lane < 0){
      lane = laneEnds.length;
      laneEnds.push(0);
    }
    l._lane = lane;
    laneEnds[lane] = l._rightPx;
  });
  const laneCount = Math.max(2, laneEnds.length || 1);
  const laneTop = 286;
  const laneHeight = 46;
  const heightPx = Math.max(430, laneTop + laneCount * laneHeight + 34);
  track.style.height = `${heightPx}px`;
  track.style.width = `${widthPx}px`;
  document.getElementById("track_scale").textContent = `${fmt(minMm,0)} bis ${fmt(maxMm,0)} mm`;
  document.getElementById("track_note").innerHTML = `
    <span class="pill"><span class="legend-dot" style="color:#005eb8"></span>Druck ${fmt((payload.format||{}).print_distance_mm,1)} mm</span>
    <span class="pill">${labels.length} aktive Labels</span>
    <span class="pill">${components.length} Komponenten</span>`;
  let html = `<div class="track-ruler"></div><div class="rail"></div>`;
  for(let mm = minMm; mm <= maxMm + 0.001; mm += step){
    const left = clampPx(toPx(mm));
    const major = Math.abs(mm % (step*2)) < 0.001;
    html += `<div class="tick ${major?"major":""}" style="left:${left}px"></div><div class="tick-label" style="left:${left}px">${fmt(mm,0)} mm</div>`;
  }
  const componentRows = [];
  components.forEach(c => {
    const kind = componentKind(c.kind);
    const x = clampPx(toPx(c.mm || 0));
    const label = String(c.label || "");
    const tagWidth = Math.min(158, Math.max(92, label.length * 7 + 42));
    const tagLeft = clampPx(x - tagWidth / 2, 4, Math.max(4, widthPx - tagWidth - 4));
    let useRow = 0;
    while(componentRows[useRow] !== undefined && tagLeft <= componentRows[useRow] + 12){
      useRow += 1;
    }
    componentRows[useRow] = tagLeft + tagWidth;
    const tagTop = 76 + useRow * 34;
    const editable = bool(c.editable);
    const title = editable ? `${label}: ${fmt(c.mm,1)} mm / ${esc(c.param || "")}` : `${label}: ${fmt(c.mm,1)} mm`;
    html += `
      <div class="component-line component-${kind}" style="left:${x}px;height:${heightPx-92}px"></div>
      <div class="component-pin component-${kind}" style="left:${x}px"></div>
      <div class="component-tag component-${kind} ${editable?"editable":""}" data-key="${esc(c.key || "")}" data-mm="${fmt(c.mm,1)}" data-label="${esc(label)}" title="${title}" style="left:${tagLeft}px;top:${tagTop}px;width:${tagWidth}px">${esc(c.label)}<span class="component-mm">${fmt(c.mm,1)} mm</span></div>`;
  });
  for(let r=0;r<laneCount;r++){
    const top = laneTop + r*laneHeight;
    html += `<div class="lane ${r%2?"alt":""}" style="top:${top}px"></div><div class="lane-no" style="top:${top+11}px">L${r+1}</div>`;
  }
  if(labels.length === 0){
    html += `<div class="track-empty">Keine aktiven Labels im ESP-Schieberegister.</div>`;
  }
  labelRects.forEach(l => {
    const top = laneTop + l._lane*laneHeight + 7;
    const err = Number(l.print_error_mm);
    const errText = Number.isFinite(err) ? ` / ${fmt(err,3)} mm` : "";
    html += `<div class="label-bar ${labelClass(l)} ${l._clippedLeft?"clipped-left":""} ${l._clippedRight?"clipped-right":""}" title="#${esc(l.no)} ${fmt(l._tail,1)}..${fmt(l._lead,1)} mm${esc(errText)}" style="left:${l._leftPx}px;width:${l._barWidthPx}px;top:${top}px">
      <span class="label-id">#${esc(l.no)}</span>
      <span class="label-stage">${esc(stageText(l))}${esc(errText)}</span>
      <span class="label-mm">${fmt(l._tail,1)}..${fmt(l._lead,1)} mm</span>
    </div>`;
  });
  track.innerHTML = html;
}
let savingComponent = false;
async function editComponentMarker(tag){
  if(savingComponent || !tag) return;
  const key = tag.dataset.key || "";
  const label = tag.dataset.label || key;
  const current = tag.dataset.mm || "0.0";
  const raw = window.prompt(`${label} Position in mm`, current);
  if(raw === null) return;
  const mm = Number(String(raw).trim().replace(",", "."));
  if(!Number.isFinite(mm)){
    window.alert("Ungueltiger mm-Wert");
    return;
  }
  savingComponent = true;
  tag.style.opacity = ".62";
  try{
    const payload = await postApi("/api/machine/production-visualization/component", {key, mm});
    render(payload);
    const pill = document.getElementById("state_pill");
    const saved = payload.saved || {};
    pill.className = "pill ok";
    pill.textContent = `${saved.label || label} ${fmt(saved.mm ?? mm,1)} mm gespeichert`;
  }catch(err){
    window.alert(err.message || String(err));
  }finally{
    savingComponent = false;
    tag.style.opacity = "";
  }
}
function renderRows(payload){
  const labels = payload.active_labels || [];
  document.getElementById("active_count").textContent = String(labels.length);
  document.getElementById("active_rows").innerHTML = labels.length ? labels.map(l => `
    <tr>
      <td><b>#${esc(l.no)}</b><br/><span class="muted small">${bool(l.open)?"offen":"geschlossen"}</span></td>
      <td>Lead ${fmt(l.lead_mm,1)} mm<br/>Tail ${fmt(l.tail_mm,1)} mm<br/>Laenge ${fmt(l.measured_length_mm,1)} mm</td>
      <td><span class="pill ${labelClass(l)}">${esc(stageText(l))}</span><br/>Printfehler ${fmt(l.print_error_mm,3)} mm<br/>Korrekturen ${esc(l.print_corrections ?? l.registration_attempts ?? 0)}</td>
      <td>${flags(l)}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="muted">Keine aktiven Labels im ESP-Schieberegister.</td></tr>';
  const hist = payload.completed_labels || [];
  document.getElementById("history_count").textContent = String(hist.length);
  document.getElementById("history_rows").innerHTML = hist.length ? hist.map(item => {
    const p = item.payload || {};
    const ok = bool(item.production_ok);
    return `<tr>
      <td><b>#${esc(item.label_no)}</b><br/><span class="muted small">${esc(item.production_label||"")}</span></td>
      <td>${fmt(p.measured_length_mm,1)} mm<br/><span class="muted small">Start ${fmt(p.zero_mm,1)} / Exit ${fmt(p.exit_mm,1)}</span></td>
      <td><span class="pill ${ok?"ok":"bad"}">${ok?"OK":"NOK"}</span><br/>Material ${Number(item.material_ok)} / Print ${Number(item.print_ok)} / Verify ${Number(item.verify_ok)} / Removed ${Number(item.removed)}</td>
      <td>${flags(p)}<br/><span class="muted small">Printfehler ${fmt(p.print_error_mm,3)} mm / Korr. ${esc(p.registration_attempts ?? 0)}</span></td>
    </tr>`;
  }).join("") : '<tr><td colspan="4" class="muted">Noch keine abgeschlossenen Labels in der Datenbank.</td></tr>';
}
function render(payload){
  const machine = payload.machine || {};
  const esp = payload.esp_snapshot || {};
  const prod = esp.production || {};
  const fmtData = payload.format || {};
  const led = payload.led || {};
  const ledTest = esp.led_test || {};
  const pill = document.getElementById("state_pill");
  pill.className = `pill ${payload.ok ? "ok" : "bad"}`;
  pill.textContent = machine.current_state_label || "unbekannt";
  kv("machine_kv", [
    ["Status", `${esc(machine.current_state)} / ${esc(machine.current_state_label)}`],
    ["Auftrag", esc(machine.production_label || "-")],
    ["ESP", payload.esp_error ? `<span class="pill bad">${esc(payload.esp_error)}</span>` : '<span class="pill ok">verbunden</span>'],
    ["Letztes Label", esc(machine.last_label_no ?? "-")]
  ]);
  kv("transport_kv", [
    ["Infeed", `${fmt(esp.infeed_mm,3)} mm`],
    ["Drive", `${fmt(esp.drive_mm,3)} mm`],
    ["Speed", `${fmt(esp.infeed_speed_mm_s,2)} mm/s`],
    ["Production", `${bool(prod.running)?"running":"idle"} / Phase ${esc(prod.phase ?? "-")}`]
  ]);
  kv("label_kv", [
    ["Soll", `${fmt(fmtData.label_length_mm,1)} mm +/- ${fmt(fmtData.label_tolerance_mm,1)}`],
    ["Aktiv", esc((payload.active_labels||[]).length)],
    ["Erfasst", esc(prod.label_start_seen ? "ja" : "nein")],
    ["Fehler", `${bool(esp.faults?.label_short)?"zu kurz ":""}${bool(esp.faults?.label_long)?"zu lang ":""}${bool(esp.faults?.sensor_fault)?"Sensor ":""}` || "-"]
  ]);
  kv("led_kv", [
    ["Offset", `${fmt(led.offset_mm,1)} mm`],
    ["Laenge", `${fmt(led.length_mm,1)} mm aktiv / ${fmt(led.physical_length_mm,1)} mm phys.`],
    ["Pitch", `${fmt(led.pitch_mm,2)} mm`],
    ["Pixel", `${esc(led.count ?? "-")} / ${esc(led.max_count ?? "-")}`],
    ["Controller", `${esc(led.pin_label || "Externer Controller")}`],
    ["Ziel", `${bool(led.controller_enabled) ? '<span class="pill ok">aktiv</span>' : '<span class="pill warn">aus</span>'} ${esc(led.controller_target || "-")}`],
    ["Protokoll", `${esc(led.protocol || "MAS004-LED-UDP/v1")}`],
    ["Quelle", `${esc(led.controller_source || "ESP32-PLC")}`],
    ["Hinweis", led.warning ? `<span class="pill warn">${esc(led.warning)}</span>` : "-"],
    ["Test", bool(ledTest.running) ? (bool(ledTest.permanent) ? `${esc(ledTest.mode || "aktiv")} permanent` : `aktiv ${esc(ledTest.remaining_ms ?? 0)} ms`) : "inaktiv"]
  ]);
  document.getElementById("updated_at").textContent = new Date().toLocaleTimeString();
  renderTrack(payload);
  renderRows(payload);
}
function setLedTestStatus(text, kind=""){
  const status = document.getElementById("led_test_status");
  const msg = String(text || "");
  status.className = `pill ${kind}`;
  status.textContent = msg.length > 90 ? `${msg.slice(0, 87)}...` : msg;
}
async function startLedTest(){
  const start = document.getElementById("led_test_start");
  const stop = document.getElementById("led_test_stop");
  start.disabled = true;
  stop.disabled = true;
  setLedTestStatus("rot startet", "warn");
  try{
    await postApi("/api/machine/led-test", {action:"red", duration_ms:0});
    setLedTestStatus("rot permanent", "ok");
    await loadAll();
  }catch(err){
    setLedTestStatus(err.message, "bad");
  }finally{
    start.disabled = false;
    stop.disabled = false;
  }
}
async function stopLedTest(){
  const start = document.getElementById("led_test_start");
  const stop = document.getElementById("led_test_stop");
  start.disabled = true;
  stop.disabled = true;
  setLedTestStatus("stoppt", "warn");
  try{
    await postApi("/api/machine/led-test", {action:"stop"});
    setLedTestStatus("bereit");
  }catch(err){
    setLedTestStatus(err.message, "bad");
  }finally{
    start.disabled = false;
    stop.disabled = false;
  }
}
async function loadAll(){
  try{
    const payload = await api("/api/machine/production-visualization");
    render(payload);
  }catch(err){
    const pill = document.getElementById("state_pill");
    pill.className = "pill bad";
    pill.textContent = err.message;
  }
}
document.getElementById("track").addEventListener("dblclick", ev => {
  const tag = ev.target.closest(".component-tag.editable");
  if(tag) editComponentMarker(tag);
});
function schedule(){
  if(timer) clearInterval(timer);
  timer = setInterval(()=>{ if(!document.hidden && document.getElementById("auto_refresh").checked) loadAll(); }, 1500);
}
document.getElementById("auto_refresh").addEventListener("change", schedule);
schedule();
loadAll();
</script>
</body>
</html>
""".replace("__NAV_HTML__", nav_html)
