param(
    [Parameter(Mandatory = $true)]
    [string]$TargetSsh,

    [string]$RemoteRoot = "/opt/MAS-004_RPI-Databridge",
    [string]$ConfigPath = "/etc/mas004_rpi_databridge/config.json",
    [string]$DIClientAdapterKey = $env:MAS004_DICLIENT_ADAPTER_KEY
)

$ErrorActionPreference = "Stop"

$files = @(
    "mas004_rpi_databridge/router.py",
    "mas004_rpi_databridge/esp_push_listener.py",
    "mas004_rpi_databridge/machine_runtime.py",
    "mas004_rpi_databridge/io_runtime.py",
    "mas004_rpi_databridge/config.py",
    "mas004_rpi_databridge/peers.py",
    "mas004_rpi_databridge/service.py",
    "mas004_rpi_databridge/webui.py",
    "mas004_rpi_databridge/setup_wickler_orchestrator.py",
    "docs/Microtom_Interface.md",
    "docs/SUPPORT_RUNBOOK.md",
    "docs/PROJECT_CONTEXT.md",
    "docs/SUPPORT_CHANGELOG.md",
    "scripts/microtom_test_simulation_config_patch.json"
)

$stage = "/tmp/mas004_scenario_b_deploy"

ssh -o StrictHostKeyChecking=no $TargetSsh "rm -rf $stage && mkdir -p $stage"
scp -o StrictHostKeyChecking=no @files "${TargetSsh}:$stage/"

$remote = @"
set -e
cd "$RemoteRoot"
sudo cp "$stage/router.py" mas004_rpi_databridge/router.py
sudo cp "$stage/esp_push_listener.py" mas004_rpi_databridge/esp_push_listener.py
sudo cp "$stage/machine_runtime.py" mas004_rpi_databridge/machine_runtime.py
sudo cp "$stage/io_runtime.py" mas004_rpi_databridge/io_runtime.py
sudo cp "$stage/config.py" mas004_rpi_databridge/config.py
sudo cp "$stage/peers.py" mas004_rpi_databridge/peers.py
sudo cp "$stage/service.py" mas004_rpi_databridge/service.py
sudo cp "$stage/webui.py" mas004_rpi_databridge/webui.py
sudo cp "$stage/setup_wickler_orchestrator.py" mas004_rpi_databridge/setup_wickler_orchestrator.py
pkg_dir=`$(.venv/bin/python - <<'PY'
import pathlib
import site

for raw in site.getsitepackages():
    candidate = pathlib.Path(raw) / "mas004_rpi_databridge"
    if candidate.exists():
        print(candidate.resolve())
        break
else:
    raise SystemExit("mas004_rpi_databridge package not found in site-packages")
PY
)
sudo cp "$stage/router.py" "`$pkg_dir/router.py"
sudo cp "$stage/esp_push_listener.py" "`$pkg_dir/esp_push_listener.py"
sudo cp "$stage/machine_runtime.py" "`$pkg_dir/machine_runtime.py"
sudo cp "$stage/io_runtime.py" "`$pkg_dir/io_runtime.py"
sudo cp "$stage/config.py" "`$pkg_dir/config.py"
sudo cp "$stage/peers.py" "`$pkg_dir/peers.py"
sudo cp "$stage/service.py" "`$pkg_dir/service.py"
sudo cp "$stage/webui.py" "`$pkg_dir/webui.py"
sudo cp "$stage/setup_wickler_orchestrator.py" "`$pkg_dir/setup_wickler_orchestrator.py"
sudo cp "$stage/Microtom_Interface.md" docs/Microtom_Interface.md
sudo cp "$stage/SUPPORT_RUNBOOK.md" docs/SUPPORT_RUNBOOK.md
sudo cp "$stage/PROJECT_CONTEXT.md" docs/PROJECT_CONTEXT.md
sudo cp "$stage/SUPPORT_CHANGELOG.md" docs/SUPPORT_CHANGELOG.md
sudo python3 - "$ConfigPath" "$stage/microtom_test_simulation_config_patch.json" "$DIClientAdapterKey" <<'PY'
import json
import pathlib
import sys

config_path = pathlib.Path(sys.argv[1])
patch_path = pathlib.Path(sys.argv[2])
adapter_key = sys.argv[3] if len(sys.argv) > 3 else ""
config = json.loads(config_path.read_text(encoding="utf-8"))
patch = json.loads(patch_path.read_text(encoding="utf-8"))
config.update(patch)
if adapter_key:
    config["diclient_adapter_key"] = adapter_key
tmp = config_path.with_suffix(config_path.suffix + ".tmp")
tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(config_path)
print("simulation patch applied:", ", ".join(sorted(patch)))
print("diclient adapter key:", "set" if config.get("diclient_adapter_key") else "empty")
PY
.venv/bin/python -m compileall -q mas004_rpi_databridge/router.py mas004_rpi_databridge/esp_push_listener.py mas004_rpi_databridge/machine_runtime.py mas004_rpi_databridge/io_runtime.py mas004_rpi_databridge/config.py mas004_rpi_databridge/peers.py mas004_rpi_databridge/service.py mas004_rpi_databridge/webui.py mas004_rpi_databridge/setup_wickler_orchestrator.py
.venv/bin/python -m compileall -q "`$pkg_dir/router.py" "`$pkg_dir/esp_push_listener.py" "`$pkg_dir/machine_runtime.py" "`$pkg_dir/io_runtime.py" "`$pkg_dir/config.py" "`$pkg_dir/peers.py" "`$pkg_dir/service.py" "`$pkg_dir/webui.py" "`$pkg_dir/setup_wickler_orchestrator.py"
sudo systemctl restart mas004-rpi-databridge.service
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if python3 -c 'import ssl, urllib.request; ctx=ssl._create_unverified_context(); print(urllib.request.urlopen("https://127.0.0.1:8080/health", context=ctx, timeout=5).read().decode("utf-8"))' 2>/dev/null; then
    exit 0
  fi
  sleep 1
done
echo "health check failed after service restart" >&2
sudo systemctl status mas004-rpi-databridge.service --no-pager -l >&2
exit 1
"@

$remote = $remote -replace "`r`n", "`n"
$remote | ssh -o StrictHostKeyChecking=no $TargetSsh bash -s
