from __future__ import annotations


def build_motor3_calibration_ui_html(nav_html: str) -> str:
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MAS-004 Motor 3 Kalibrierung</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f5f7fb;color:#172033}}
    .wrap{{max-width:1280px;margin:0 auto;padding:16px}}
    .topnav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}}
    .navbtn{{padding:8px 12px;border:1px solid #d8e0ee;border-radius:8px;background:#fff;color:#172033;text-decoration:none;font-weight:700}}
    .navbtn.active{{background:#005eb8;color:#fff;border-color:#005eb8}}
    .grid{{display:grid;grid-template-columns:1.1fr .9fr;gap:14px}}
    .panel{{background:white;border:1px solid #d8e0ee;border-radius:8px;padding:14px}}
    h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}}
    .muted{{color:#627086;font-size:13px}} .row{{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin:10px 0}}
    label{{display:grid;gap:4px;font-size:12px;color:#44516a}}
    input{{font:inherit;padding:8px;border:1px solid #c8d2e3;border-radius:6px;min-width:130px}}
    button{{font:inherit;font-weight:650;border:1px solid #9db7d9;background:#e9f2ff;color:#113f78;border-radius:6px;padding:8px 12px;cursor:pointer}}
    button.danger{{background:#fff0f0;color:#8d1f1f;border-color:#e0aaaa}}
    button:disabled{{opacity:.55;cursor:not-allowed}}
    .kv{{display:grid;grid-template-columns:220px 1fr;gap:6px;font-size:13px}}
    .kv div:nth-child(odd){{color:#627086}}
    pre{{white-space:pre-wrap;background:#111827;color:#e5e7eb;border-radius:8px;padding:10px;max-height:420px;overflow:auto}}
    table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #e2e8f0;padding:6px;text-align:right}} th:first-child,td:first-child{{text-align:left}}
    @media(max-width:900px){{.grid{{grid-template-columns:1fr}}.kv{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
{nav_html}
<main class="wrap">
  <h1>Motor 3 / Encoder Kalibrierung</h1>
  <p class="muted">2000-mm-Abgleich fuer ID3, Einlaufencoder, Auslaufencoder und Label-Laengenkorrektur.</p>
  <div class="grid">
    <section class="panel">
      <h2>Kalibrierfahrt</h2>
      <div class="row">
        <button id="prepareBtn" onclick="prepareRun()">Vorbereiten</button>
        <button id="startBtn" onclick="startRun()">2000-mm-Fahrt starten</button>
        <button class="danger" onclick="refresh()">Aktualisieren</button>
      </div>
      <div id="status" class="kv"></div>
      <h2>Messwerte anwenden</h2>
      <div class="row">
        <label>Real gefahrene Strecke mm<input id="actualTravel" type="number" step="0.001" value="2000.000"></label>
        <label>Reale Label-Laenge mm<input id="actualLabel" type="number" step="0.001" value="99.800"></label>
        <label><input id="applyLabelComp" type="checkbox"> Label-Laengenkompensation schreiben</label>
        <button onclick="applyValues()">Abgleich berechnen + senden</button>
      </div>
      <div id="applyResult" class="kv"></div>
    </section>
    <section class="panel">
      <h2>Letzte Labelmessung</h2>
      <table>
        <thead><tr><th>#</th><th>Roh Einlauf</th><th>Komp. Einlauf</th><th>Roh Auslauf</th></tr></thead>
        <tbody id="labels"></tbody>
      </table>
    </section>
  </div>
  <section class="panel" style="margin-top:14px">
    <h2>Rohdaten</h2>
    <pre id="raw"></pre>
  </section>
</main>
<script>
async function api(url, opts={{}}){{
  const r = await fetch(url, {{...opts, headers:{{"Content-Type":"application/json", ...(opts.headers||{{}})}}}});
  const text = await r.text();
  let payload = null;
  try{{ payload = text ? JSON.parse(text) : {{}}; }}catch(e){{ payload = {{ok:false, detail:text}}; }}
  if(!r.ok) throw payload;
  return payload;
}}
function fmt(v, digits=3){{
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : (v ?? "");
}}
function kv(el, obj){{
  el.innerHTML = Object.entries(obj).map(([k,v])=>`<div>${{k}}</div><div>${{v}}</div>`).join("");
}}
function renderLabels(labels){{
  document.getElementById("labels").innerHTML = (labels||[]).slice(0,20).map((l,i)=>`
    <tr><td>${{l.label_no ?? i+1}}</td><td>${{fmt(l.raw_infeed_mm ?? l.infeed_mm)}}</td><td>${{fmt(l.measured_infeed_mm ?? l.infeed_mm)}}</td><td>${{fmt(l.raw_drive_mm ?? l.drive_mm)}}</td></tr>
  `).join("");
}}
async function refresh(){{
  const j = await api("/api/machine/motor3-calibration/status");
  const r = j.result || {{}};
  const t = j.travel_diag || r.travel_diag || {{}};
  kv(document.getElementById("status"), {{
    "Status": j.running ? "laeuft" : "bereit",
    "Script": j.script || "",
    "Letzte Strecke Einlauf": fmt(t.infeed_mm),
    "Letzte Strecke Auslauf": fmt(t.drive_mm),
    "Labels": t.label_count ?? "",
    "MAP0076": j.params?.MAP0076 ?? "",
    "MAP0077": j.params?.MAP0077 ?? "",
    "MAP0078": j.params?.MAP0078 ?? "",
    "ID3 steps/mm": fmt(j.motor3?.config?.steps_per_mm ?? j.motor3?.steps_per_mm, 6),
  }});
  renderLabels(t.labels || []);
  document.getElementById("raw").textContent = JSON.stringify(j, null, 2);
}}
async function startRun(){{
  document.getElementById("startBtn").disabled = true;
  try{{ await api("/api/machine/motor3-calibration/start", {{method:"POST", body:"{{}}"}}); }}
  finally{{ setTimeout(()=>{{document.getElementById("startBtn").disabled=false; refresh();}}, 1200); }}
}}
async function prepareRun(){{
  document.getElementById("prepareBtn").disabled = true;
  try{{ await api("/api/machine/motor3-calibration/prepare", {{method:"POST", body:"{{}}"}}); }}
  finally{{ setTimeout(()=>{{document.getElementById("prepareBtn").disabled=false; refresh();}}, 1200); }}
}}
async function applyValues(){{
  const applyLabelComp = document.getElementById("applyLabelComp").checked;
  const body = {{
    actual_travel_mm: Number(document.getElementById("actualTravel").value),
    actual_label_length_mm: applyLabelComp ? Number(document.getElementById("actualLabel").value) : null,
  }};
  const j = await api("/api/machine/motor3-calibration/apply", {{method:"POST", body:JSON.stringify(body)}});
  kv(document.getElementById("applyResult"), {{
    "ID3 steps/mm": fmt(j.calculated?.motor3_steps_per_mm, 6),
    "MAP0076": j.calculated?.MAP0076,
    "MAP0077": j.calculated?.MAP0077,
    "MAP0078": j.calculated?.MAP0078,
    "OK": j.ok
  }});
  document.getElementById("raw").textContent = JSON.stringify(j, null, 2);
  refresh();
}}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>"""
