from __future__ import annotations


def build_winder_ui_html(role: str, label: str, nav_html: str) -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{label} - Smart Wickler</title>
  <style>
    :root {{
      --bg:#f4f6f9; --card:#ffffff; --text:#1f2933; --muted:#5f6b7a; --border:#d6dde7; --blue:#005eb8;
      --good:#2e7d32; --warn:#ed6c02; --bad:#c62828;
    }}
    *{{box-sizing:border-box}}
    body{{margin:0; font-family:Segoe UI,Arial,sans-serif; background:linear-gradient(160deg,#eef5fb,#f8fafc 45%,#e8eef9); color:var(--text)}}
    .wrap{{max-width:1400px; margin:0 auto; padding:16px}}
    .toolbar,.grid,.actions{{display:flex; gap:10px; align-items:center; flex-wrap:wrap}}
    .grid{{display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px}}
    .card{{background:var(--card); border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:0 14px 34px rgba(17,24,39,.06)}}
    .hero{{display:grid; grid-template-columns:2fr 1fr; gap:14px}}
    .hero-main{{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px}}
    .kpi{{background:#f8fafc; border:1px solid var(--border); border-radius:14px; padding:14px}}
    .kpi .label{{font-size:12px; color:var(--muted); font-weight:700; text-transform:uppercase; letter-spacing:.04em}}
    .kpi .value{{font-size:2rem; font-weight:800; margin-top:6px}}
    .muted{{color:var(--muted)}}
    .btn{{min-height:38px; padding:8px 12px; border:1px solid #aec4db; border-radius:10px; background:#e8f0f8; color:#17324b; font-weight:600; cursor:pointer; text-decoration:none}}
    .btn.primary{{background:#005eb8; color:#fff; border-color:#005eb8}}
    .status-pill{{display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; font-weight:700; border:1px solid var(--border)}}
    .status-stop{{background:#dbeafe; color:#1d4ed8}}
    .status-ready{{background:#dcfce7; color:#166534}}
    .status-warn{{background:#fef3c7; color:#92400e}}
    .status-fault{{background:#fee2e2; color:#991b1b}}
    table{{width:100%; border-collapse:collapse}}
    th,td{{text-align:left; padding:9px 0; border-bottom:1px solid #e6ebf1}}
    .dial-wrap{{display:flex; align-items:center; justify-content:center; min-height:280px}}
    .dial{{width:240px; height:240px; border-radius:50%; background:conic-gradient(#005eb8 0deg, #dce8f5 0deg 360deg); display:flex; align-items:center; justify-content:center; position:relative; box-shadow:inset 0 0 0 14px #fff}}
    .dial::after{{content:""; position:absolute; width:154px; height:154px; border-radius:50%; background:#fff; border:1px solid var(--border)}}
    .dial-center{{position:relative; z-index:2; text-align:center}}
    .dial-center strong{{display:block; font-size:2rem}}
    .stack{{display:grid; gap:12px}}
    @media(max-width:980px){{ .hero{{grid-template-columns:1fr}} .hero-main{{grid-template-columns:1fr 1fr}} }}
    @media(max-width:700px){{ .hero-main{{grid-template-columns:1fr}} }}
  </style>
</head>
<body>
    <div class="wrap">
      {nav_html}
      <div class="toolbar" style="margin-bottom:12px">
      <a class="btn" href="/ui/machine-setup/motors">Motors</a>
      <button class="btn primary" onclick="openDeviceUi()">Geraete-UI oeffnen</button>
      <span id="headline" class="muted">lade {label}...</span>
    </div>
    <div class="hero">
      <div class="card">
        <div class="toolbar" style="justify-content:space-between; margin-bottom:8px">
          <div>
            <div class="muted">Smart Wickler</div>
            <h2 style="margin:4px 0 0 0">{label}</h2>
          </div>
          <div id="modeBadge" class="status-pill status-stop">Stop</div>
        </div>
        <div class="hero-main">
          <div class="kpi"><div class="label">Wippe</div><div id="wipeValue" class="value">0 %</div></div>
          <div class="kpi"><div class="label">Fuellstand</div><div id="fillValue" class="value">0 %</div></div>
          <div class="kpi"><div class="label">Bandgeschwindigkeit</div><div id="speedValue" class="value">0 mm/s</div></div>
          <div class="kpi"><div class="label">Motor</div><div id="motorValue" class="value">0 Hz</div></div>
        </div>
      </div>
      <div class="card dial-wrap">
        <div id="fillDial" class="dial">
          <div class="dial-center">
            <div class="muted">Rolle</div>
            <strong id="dialFill">0 %</strong>
            <div id="roleText" class="muted">{label}</div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid" style="margin-top:14px">
      <div class="card">
        <h3 style="margin-top:0">Drive / Maschine</h3>
        <table>
          <tbody>
            <tr><th>Endpoint</th><td id="endpointText">-</td></tr>
            <tr><th>Betriebsart</th><td id="simText">-</td></tr>
            <tr><th>AZD-CD Online</th><td id="driveOnline">-</td></tr>
            <tr><th>Drive Ready</th><td id="driveReady">-</td></tr>
            <tr><th>Drive Move</th><td id="driveMove">-</td></tr>
            <tr><th>Alarm</th><td id="driveAlarm">-</td></tr>
            <tr><th>Fehler</th><td id="deviceError">-</td></tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Masterwerte</h3>
        <table>
          <tbody>
            <tr><th>MAP0023</th><td id="map0023">0</td></tr>
            <tr><th>MAP0024</th><td id="map0024">0</td></tr>
            <tr><th>MAP0025</th><td id="map0025">0</td></tr>
            <tr><th>MAP0047</th><td id="map0047">0</td></tr>
            <tr><th>Status-MAS</th><td id="statusMas">0</td></tr>
            <tr><th>Fuellstand-MAS</th><td id="fillMas">0</td></tr>
            <tr><th>Pause Request</th><td id="pauseRequest">0</td></tr>
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Stoerungen / Flags</h3>
        <table>
          <tbody>
            <tr><th>MAE blocked</th><td id="maeBlocked">0</td></tr>
            <tr><th>MAE too high</th><td id="maeHigh">0</td></tr>
            <tr><th>MAE too low</th><td id="maeLow">0</td></tr>
            <tr><th>Wipe max counts</th><td id="wipeMaxCounts">0</td></tr>
            <tr><th>Wipe idle %</th><td id="wipeIdlePercent">0</td></tr>
            <tr><th>Wipe threshold %</th><td id="wipeThresholdPercent">0</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
<script>
const ROLE = "{role}";
const TOKEN_KEY = "mas004_ui_token";
let currentDeviceUrl = "";

function token(){{ try {{ return localStorage.getItem(TOKEN_KEY) || ""; }} catch(e) {{ return ""; }} }}

async function api(path){{
  const headers = {{}};
  const t = token();
  if(t) headers["X-Token"] = t;
  const r = await fetch(path, {{headers}});
  const txt = await r.text();
  let j = null; try {{ j = JSON.parse(txt); }} catch(e) {{}}
  if(!r.ok) throw new Error((j && j.detail) ? j.detail : (`HTTP ${{r.status}} ${{txt}}`));
  return j;
}}

function boolText(v){{ return v ? "Ja" : "Nein"; }}

function modeClass(modeCss){{
  if(modeCss === "ready") return "status-pill status-ready";
  if(modeCss === "warn") return "status-pill status-warn";
  if(modeCss === "fault") return "status-pill status-fault";
  return "status-pill status-stop";
}}

function setText(id, value){{ const el = document.getElementById(id); if(el) el.textContent = value ?? ""; }}

function updateDial(fillPercent){{
  const p = Math.max(0, Math.min(100, Number(fillPercent || 0)));
  document.getElementById("fillDial").style.background = `conic-gradient(#005eb8 ${{p * 3.6}}deg, #dce8f5 ${{p * 3.6}}deg 360deg)`;
  setText("dialFill", `${{p.toFixed(1)}} %`);
}}

function openDeviceUi(){{
  if(!currentDeviceUrl){{
    alert("Kein Wickler-Endpoint konfiguriert.");
    return;
  }}
  window.open(currentDeviceUrl, "_blank", "noopener");
}}

async function reload(){{
  const data = await api(`/api/winders/${{ROLE}}/state`);
  currentDeviceUrl = data.device?.base_url || "";
  setText("headline", data.device?.simulation ? "Simulation aktiv" : (data.device?.reachable ? "Endpoint online" : "Endpoint offline"));
  setText("roleText", data.config?.roleLabel || "{label}");
  setText("wipeValue", `${{Number(data.telemetry?.wipePercent || 0).toFixed(1)}} %`);
  setText("fillValue", `${{Number(data.telemetry?.fillPercent || 0).toFixed(1)}} %`);
  setText("speedValue", `${{Number(data.telemetry?.rollerSpeedMmS || 0).toFixed(1)}} mm/s`);
  setText("motorValue", `${{Number(data.telemetry?.motorSpeedHz || 0).toFixed(0)}} Hz`);
  setText("endpointText", currentDeviceUrl || "-");
  setText("simText", data.device?.simulation ? "Simulation" : (data.device?.reachable ? "Live" : "Live (offline)"));
  setText("driveOnline", boolText(!!data.drive?.online));
  setText("driveReady", boolText(!!data.drive?.ready));
  setText("driveMove", boolText(!!data.drive?.move));
  setText("driveAlarm", data.drive?.alarm ? `Ja (Code ${{data.drive?.alarmCode ?? 0}})` : "Nein");
  setText("deviceError", data.device?.error || "-");
  setText("map0023", data.master?.map0023 ?? 0);
  setText("map0024", data.master?.map0024 ?? 0);
  setText("map0025", data.master?.map0025 ?? 0);
  setText("map0047", data.master?.map0047 ? "1" : "0");
  setText("statusMas", data.values?.statusMas ?? 0);
  setText("fillMas", data.values?.fillMas ?? 0);
  setText("pauseRequest", data.telemetry?.pauseRequest ? "1" : "0");
  setText("maeBlocked", data.values?.maeBlocked ? "1" : "0");
  setText("maeHigh", data.values?.maeHigh ? "1" : "0");
  setText("maeLow", data.values?.maeLow ? "1" : "0");
  setText("wipeMaxCounts", data.config?.wipeMaxCounts ?? 0);
  setText("wipeIdlePercent", data.config?.wipeIdlePercent ?? 0);
  setText("wipeThresholdPercent", data.config?.wipeThresholdPercent ?? 0);
  const badge = document.getElementById("modeBadge");
  badge.className = modeClass(String(data.telemetry?.modeCss || ""));
  badge.textContent = data.telemetry?.modeLabel || "Stop";
  updateDial(data.telemetry?.fillPercent || 0);
}}

reload().catch(err => {{ document.getElementById("headline").textContent = err.message; }});
setInterval(() => reload().catch(err => {{ document.getElementById("headline").textContent = err.message; }}), 2000);
</script>
</body>
</html>
"""
