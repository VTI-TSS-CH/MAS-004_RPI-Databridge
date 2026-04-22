param(
    [ValidateSet("Plan", "Precheck", "DeployRaspiBootstrap", "ApplyRaspiRuntimeConfig", "ApplyRaspiNetwork", "StatusAfterCutover", "DeployEspFromRaspi", "DeployWicklerUsb")]
    [string]$Phase = "Plan",
    [switch]$Execute,
    [string]$BootstrapSsh = "pi@10.27.67.68",
    [string]$ProductionSsh = "pi@10.141.94.213",
    [string]$ProductionHost = "10.141.94.213",
    [string]$ConfigPath = "/etc/mas004_rpi_databridge/config.json"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptRoot = $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $scriptRoot "..")).Path
$gitRoot = Split-Path $repoRoot -Parent
$topologyPath = Join-Path $scriptRoot "production_topology_10_141_94.json"
$configPatchPath = Join-Path $scriptRoot "production_commissioning_config_patch_10_141_94.json"

function Read-JsonFile([string]$Path) {
    return Get-Content $Path -Raw | ConvertFrom-Json
}

function Write-Section([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Planned([string]$Title, [scriptblock]$Action) {
    Write-Section $Title
    if (-not $Execute) {
        Write-Host "Plan only. Re-run this phase with -Execute to run it." -ForegroundColor DarkYellow
        return
    }
    & $Action
}

function Invoke-RemoteBash([string]$SshTarget, [string]$Script) {
    ($Script -replace "`r`n", "`n" -replace "`r", "`n") | ssh $SshTarget "bash -s"
}

function Show-Plan {
    $topology = Read-JsonFile $topologyPath
    Write-Section "Production IBN profile"
    Write-Host ("Bootstrap SSH: {0}" -f $topology.bootstrap.raspi_current_ssh)
    Write-Host ("Final Raspi:   {0}/24, gateway {1}" -f $topology.eth0.raspi_ip, $topology.eth0.gateway)
    Write-Host ("Laptop peer:   {0}" -f $topology.eth0.laptop_microtom_testtool_ip)
    Write-Host ("TTO 6530:      {0}:3002" -f $topology.eth0.devices.vj6530_tto.host)
    Write-Host ("Laser 3350:    {0}:20000" -f $topology.eth0.devices.vj3350_laser.host)
    Write-Host ("Abwickler:     {0}:3011" -f $topology.eth0.devices.smart_unwinder.host)
    Write-Host ("Aufwickler:    {0}:3012" -f $topology.eth0.devices.smart_rewinder.host)
    Write-Host ("ESP/Moxa net:  Raspi {0}, ESP {1}, Moxa {2}/{3}" -f $topology.eth1.raspi_ip, $topology.eth1.devices.esp32_plc58.host, $topology.eth1.devices.moxa1_e1211.host, $topology.eth1.devices.moxa2_e1211.host)

    Write-Section "Recommended phase order for tomorrow"
    Write-Host "1. Connect laptop to the old Raspi address and verify SSH: $BootstrapSsh"
    Write-Host "2. Run: powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase DeployRaspiBootstrap -Execute"
    Write-Host "3. Run: powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase ApplyRaspiRuntimeConfig -Execute"
    Write-Host "4. Run: powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase ApplyRaspiNetwork -Execute"
    Write-Host "5. Change laptop NIC to 10.141.94.212/24, gateway 10.141.94.1, then check https://10.141.94.213:8080"
    Write-Host "6. Run: powershell -ExecutionPolicy Bypass -File scripts/mas004_production_ibn.ps1 -Phase StatusAfterCutover -Execute"
    Write-Host "7. Deploy ESP via USB on Raspi: -Phase DeployEspFromRaspi -Execute"
    Write-Host "8. Deploy Abwickler and Aufwickler one after another via USB on laptop only: -Phase DeployWicklerUsb"
    Write-Host "   Note: Smart Wickler modules stay autonomous and are not flashed through the Raspi USB gateway."
}

function Invoke-Precheck {
    Write-Section "Local repo status"
    $repos = @(
        "MAS-004_RPI-Databridge",
        "MAS-004_ESP32-PLC-Firmware",
        "MAS-004_SmartWickler"
    )
    foreach ($repo in $repos) {
        $path = Join-Path $gitRoot $repo
        if (Test-Path $path) {
            Write-Host "-- $repo"
            git -C $path status -sb | Out-Host
        } else {
            Write-Host "-- $repo missing at $path" -ForegroundColor Yellow
        }
    }

    Write-Section "JSON profile validation"
    Get-Content $topologyPath -Raw | ConvertFrom-Json | Out-Null
    Get-Content $configPatchPath -Raw | ConvertFrom-Json | Out-Null
    Write-Host "Topology and config patch JSON are valid."
}

function Invoke-DeployRaspiBootstrap {
    Invoke-Planned "Deploy Raspi code while still reachable at $BootstrapSsh" {
        & (Join-Path $scriptRoot "mas004_multirepo_sync.ps1") -Target test -SshHost $BootstrapSsh -RestartServices
    }
}

function Invoke-ApplyRuntimeConfig {
    Invoke-Planned "Apply production commissioning runtime config on $BootstrapSsh" {
        $remotePatch = "/tmp/mas004_production_commissioning_config_patch_10_141_94.json"
        scp $configPatchPath "${BootstrapSsh}:$remotePatch" | Out-Host
        $remoteScript = @'
set -e
CONFIG_PATH='__CONFIG_PATH__'
PATCH_PATH='/tmp/mas004_production_commissioning_config_patch_10_141_94.json'
sudo mkdir -p "$(dirname "$CONFIG_PATH")"
if [ -f "$CONFIG_PATH" ]; then
  sudo cp "$CONFIG_PATH" "$CONFIG_PATH.bak.$(date +%Y%m%d_%H%M%S)"
fi
sudo python3 - <<'PY'
import json
from pathlib import Path
cfg_path = Path("__CONFIG_PATH__")
patch_path = Path("/tmp/mas004_production_commissioning_config_patch_10_141_94.json")
cfg = {}
if cfg_path.exists():
    cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
patch = json.loads(patch_path.read_text(encoding="utf-8") or "{}")
cfg.update(patch)
cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=False) + "\n", encoding="utf-8")
PY
sudo systemctl restart mas004-rpi-databridge.service
systemctl is-active mas004-rpi-databridge.service
'@
        $remoteScript = $remoteScript.Replace("__CONFIG_PATH__", $ConfigPath)
        Invoke-RemoteBash -SshTarget $BootstrapSsh -Script $remoteScript | Out-Host
    }
}

function Invoke-ApplyNetwork {
    Invoke-Planned "Apply OS network cutover on $BootstrapSsh (SSH will move to $ProductionSsh)" {
        $remoteScript = @'
set -e
cd /opt/MAS-004_RPI-Databridge
PYTHON=python3
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
fi
sudo "$PYTHON" - <<'PY'
from mas004_rpi_databridge.netconfig import IfaceCfg, apply_static
print("Applying eth0 10.141.94.213/24 gw 10.141.94.1")
print(apply_static("eth0", IfaceCfg(ip="10.141.94.213", prefix=24, gw="10.141.94.1", dns=[])))
print("Keeping eth1 192.168.2.100/24 without gateway")
print(apply_static("eth1", IfaceCfg(ip="192.168.2.100", prefix=24, gw="", dns=[])))
PY
'@
        Invoke-RemoteBash -SshTarget $BootstrapSsh -Script $remoteScript | Out-Host
        Write-Host ""
        Write-Host "The old SSH path may now drop. Set the laptop NIC to 10.141.94.212/24 and reconnect to $ProductionSsh." -ForegroundColor Yellow
    }
}

function Invoke-StatusAfterCutover {
    Invoke-Planned "Check production target after IP cutover" {
        ping $ProductionHost -n 2 | Out-Host
        ssh -o ConnectTimeout=5 $ProductionSsh "hostname; ip -4 addr show eth0; ip -4 addr show eth1; systemctl is-active mas004-rpi-databridge.service" | Out-Host
        & (Join-Path $scriptRoot "mas004_multirepo_status.ps1") -Target production -SshHost $ProductionSsh | Out-Host
    }
}

function Invoke-DeployEsp {
    Invoke-Planned "Deploy ESP32-PLC firmware through the Raspi USB alias /dev/esp32plc58" {
        $espRepo = Join-Path $gitRoot "MAS-004_ESP32-PLC-Firmware"
        if (-not (Test-Path $espRepo)) {
            throw "ESP firmware repo not found: $espRepo"
        }
        Push-Location $espRepo
        try {
            python scripts/upload_via_test_raspi.py --project-dir . --env-name esp32plc_58_v3 --pi-host $ProductionHost
        } finally {
            Pop-Location
        }
    }
}

function Resolve-PlatformIoCommand {
    $cmd = Get-Command pio -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $fallback = Join-Path $env:USERPROFILE ".platformio\penv\Scripts\platformio.exe"
    if (Test-Path $fallback) {
        return $fallback
    }

    throw "PlatformIO not found. Install PlatformIO or add pio/platformio.exe to PATH."
}

function Invoke-DeployWickler {
    Write-Section "Smart Wickler autonomous USB deploy guidance"
    $wicklerRepo = Join-Path $gitRoot "MAS-004_SmartWickler"
    Write-Host "The Smart Wicklers stay autonomous; do not connect them to the Raspi for flashing."
    Write-Host "Connect exactly one Smart Wickler ESP32-S3 to this laptop by USB."
    Write-Host "Open/keep the repo: $wicklerRepo"
    Write-Host "Recommended order:"
    Write-Host "  1. Connect Abwickler only, run this phase with -Execute, then unplug it."
    Write-Host "  2. Connect Aufwickler only, run this phase with -Execute again."
    Write-Host "Equivalent manual command:"
    Write-Host "  pio run -t upload"
    Write-Host "After both flashes, set/check their web/network config:"
    Write-Host "  Abwickler: 10.141.94.216, port 3011"
    Write-Host "  Aufwickler: 10.141.94.217, port 3012"
    if ($Execute) {
        Push-Location $wicklerRepo
        try {
            $platformIo = Resolve-PlatformIoCommand
            & $platformIo run -t upload
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "Plan only. Re-run with -Execute after connecting the correct Wickler USB cable." -ForegroundColor DarkYellow
    }
}

switch ($Phase) {
    "Plan" { Show-Plan }
    "Precheck" { Invoke-Precheck }
    "DeployRaspiBootstrap" { Invoke-DeployRaspiBootstrap }
    "ApplyRaspiRuntimeConfig" { Invoke-ApplyRuntimeConfig }
    "ApplyRaspiNetwork" { Invoke-ApplyNetwork }
    "StatusAfterCutover" { Invoke-StatusAfterCutover }
    "DeployEspFromRaspi" { Invoke-DeployEsp }
    "DeployWicklerUsb" { Invoke-DeployWickler }
}
