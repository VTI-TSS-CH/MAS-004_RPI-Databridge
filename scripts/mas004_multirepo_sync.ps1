param(
    [string]$SshHost = "mas004-rpi",
    [switch]$NoPi,
    [switch]$RestartServices,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$mainRepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitRoot = Split-Path $mainRepoPath -Parent

$repos = @(
    @{ Name = "MAS-004_RPI-Databridge"; Local = Join-Path $gitRoot "MAS-004_RPI-Databridge"; Remote = "/opt/MAS-004_RPI-Databridge"; Service = "mas004-rpi-databridge.service"; Main = $true },
    @{ Name = "MAS-004_ESP32-PLC-Bridge"; Local = Join-Path $gitRoot "MAS-004_ESP32-PLC-Bridge"; Remote = "/opt/MAS-004_ESP32-PLC-Bridge"; Service = "mas004-esp32-plc-bridge.service"; Main = $false },
    @{ Name = "MAS-004_VJ3350-Ultimate-Bridge"; Local = Join-Path $gitRoot "MAS-004_VJ3350-Ultimate-Bridge"; Remote = "/opt/MAS-004_VJ3350-Ultimate-Bridge"; Service = "mas004-vj3350-ultimate-bridge.service"; Main = $false },
    @{ Name = "MAS-004_VJ6530-ZBC-Bridge"; Local = Join-Path $gitRoot "MAS-004_VJ6530-ZBC-Bridge"; Remote = "/opt/MAS-004_VJ6530-ZBC-Bridge"; Service = "mas004-vj6530-zbc-bridge.service"; Main = $false }
)

function Invoke-Step([string]$Message, [scriptblock]$Action) {
    Write-Host "==> $Message" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "    [DRY-RUN] skipped" -ForegroundColor DarkYellow
        return
    }
    & $Action
}

function Sync-LocalRepo($repo) {
    if (-not (Test-Path $repo.Local)) {
        Write-Host "[LOCAL] $($repo.Name): missing path $($repo.Local)" -ForegroundColor Yellow
        return
    }

    Invoke-Step "[LOCAL] $($repo.Name): git fetch" {
        git -C $repo.Local fetch --all --prune | Out-Host
    }

    $dirty = [bool](git -C $repo.Local status --porcelain)
    if ($dirty) {
        Write-Host "[LOCAL] $($repo.Name): dirty -> skip pull" -ForegroundColor Yellow
        return
    }

    Invoke-Step "[LOCAL] $($repo.Name): git pull --ff-only" {
        git -C $repo.Local pull --ff-only | Out-Host
    }
}

function Sync-RemoteRepo($repo) {
    $checkScript = @'
if [ ! -d '__REMOTE_PATH__' ]; then
  echo REMOTE_MISSING
  exit 0
fi
cd '__REMOTE_PATH__' || exit 2
if git status --porcelain | grep -q .; then
  echo REMOTE_DIRTY
else
  echo REMOTE_CLEAN
fi
'@
    $checkScript = $checkScript.Replace("__REMOTE_PATH__", $repo.Remote)
    $state = (ssh $SshHost $checkScript | Select-Object -First 1).Trim()

    if ($state -eq "REMOTE_MISSING") {
        Write-Host "[PI] $($repo.Name): missing path $($repo.Remote)" -ForegroundColor Yellow
        return
    }
    if ($state -eq "REMOTE_DIRTY") {
        Write-Host "[PI] $($repo.Name): dirty -> skip pull (manual decision required)" -ForegroundColor Yellow
        return
    }

    Invoke-Step "[PI] $($repo.Name): git pull --ff-only" {
        ssh $SshHost "cd '$($repo.Remote)' && git pull --ff-only" | Out-Host
    }

    if ($RestartServices) {
        Invoke-Step "[PI] $($repo.Name): restart $($repo.Service)" {
            ssh $SshHost "sudo systemctl restart $($repo.Service) && systemctl is-active $($repo.Service)" | Out-Host
        }
    }
}

foreach ($repo in $repos) {
    Sync-LocalRepo -repo $repo
}

if (-not $NoPi) {
    foreach ($repo in $repos) {
        Sync-RemoteRepo -repo $repo
    }
}

Write-Host "==> Final status" -ForegroundColor Green
& (Join-Path $PSScriptRoot "mas004_multirepo_status.ps1") -SshHost $SshHost
