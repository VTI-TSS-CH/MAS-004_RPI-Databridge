param(
    [string]$SshHost = "mas004-rpi",
    [switch]$AsJson
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

function Get-MapValue([hashtable]$Map, [string]$Key, $Default) {
    if ($Map.ContainsKey($Key) -and $null -ne $Map[$Key] -and $Map[$Key] -ne "") {
        return $Map[$Key]
    }
    return $Default
}

function Get-LocalState([string]$Path) {
    if (-not (Test-Path $Path)) {
        return @{ exists = $false; head = ""; branch = ""; status = "MISSING"; dirty = $false }
    }

    $head = (git -C $Path rev-parse --short HEAD).Trim()
    $branch = (git -C $Path rev-parse --abbrev-ref HEAD).Trim()
    $statusSummary = (git -C $Path status -sb | Select-Object -First 1).Trim()
    $dirty = [bool](git -C $Path status --porcelain)

    return @{ exists = $true; head = $head; branch = $branch; status = $statusSummary; dirty = $dirty }
}

function Get-RemoteState([string]$SshTarget, [string]$RemotePath, [string]$Service) {
    $script = @'
if [ -d '__REMOTE_PATH__' ]; then
  cd '__REMOTE_PATH__' || exit 2
  echo EXISTS=yes
  echo HEAD=$(git rev-parse --short HEAD)
  echo BRANCH=$(git rev-parse --abbrev-ref HEAD)
  echo STATUS=$(git status -sb | head -n 1)
  if git status --porcelain | grep -q .; then
    echo DIRTY=yes
  else
    echo DIRTY=no
  fi
else
  echo EXISTS=no
fi
if systemctl list-unit-files '__SERVICE__' >/dev/null 2>&1; then
  echo SERVICE=$(systemctl is-active '__SERVICE__' 2>/dev/null)
else
  echo SERVICE=missing
fi
'@

    $script = $script.Replace("__REMOTE_PATH__", $RemotePath).Replace("__SERVICE__", $Service)

    $lines = ssh $SshTarget $script
    $map = @{}
    foreach ($line in $lines) {
        if ($line -like "*=*") {
            $parts = $line -split "=", 2
            if ($parts.Count -eq 2) {
                $map[$parts[0].Trim()] = $parts[1].Trim()
            }
        }
    }

    $exists = (Get-MapValue $map "EXISTS" "no") -eq "yes"
    $dirty = (Get-MapValue $map "DIRTY" "no") -eq "yes"
    return @{
        exists = $exists
        head = (Get-MapValue $map "HEAD" "")
        branch = (Get-MapValue $map "BRANCH" "")
        status = (Get-MapValue $map "STATUS" "MISSING")
        dirty = $dirty
        service = (Get-MapValue $map "SERVICE" "unknown")
    }
}

$report = @()
foreach ($r in $repos) {
    $local = Get-LocalState -Path $r.Local
    $remote = Get-RemoteState -SshTarget $SshHost -RemotePath $r.Remote -Service $r.Service

    $inSync = $local.exists -and $remote.exists -and ($local.head -eq $remote.head) -and (-not $local.dirty) -and (-not $remote.dirty)
    $report += [pscustomobject]@{
        name = $r.Name
        main = [bool]$r.Main
        local_head = $local.head
        local_status = $local.status
        local_dirty = [bool]$local.dirty
        remote_head = $remote.head
        remote_status = $remote.status
        remote_dirty = [bool]$remote.dirty
        service = $remote.service
        in_sync = [bool]$inSync
    }
}

if ($AsJson) {
    $report | ConvertTo-Json -Depth 4
} else {
    $report | Format-Table -AutoSize
}
