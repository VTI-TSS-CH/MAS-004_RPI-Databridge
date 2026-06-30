param(
    [string]$OutputPath = "",
    [switch]$RequireClean,
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$mainRepoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitRoot = Split-Path $mainRepoPath -Parent

$repos = @(
    @{ Name = "MAS-004_RPI-Databridge"; Path = Join-Path $gitRoot "MAS-004_RPI-Databridge" },
    @{ Name = "MAS-004_ESP32-PLC-Firmware"; Path = Join-Path $gitRoot "MAS-004_ESP32-PLC-Firmware" },
    @{ Name = "MAS-004_SmartWickler"; Path = Join-Path $gitRoot "MAS-004_SmartWickler" }
)

function Get-GitValue {
    param(
        [string]$RepoPath,
        [string[]]$GitArgs
    )

    $value = & git -C $RepoPath @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return (($value | Out-String).Trim())
}

$snapshot = @()
foreach ($repo in $repos) {
    $path = $repo.Path
    if (-not (Test-Path $path)) {
        $snapshot += [pscustomobject]@{
            name = $repo.Name
            path = $path
            exists = $false
            branch = ""
            head = ""
            head_short = ""
            remote = ""
            upstream = ""
            status = "MISSING"
            dirty = $true
            timestamp = (Get-Date).ToString("o")
        }
        continue
    }

    $statusLines = & git -C $path status --porcelain
    $dirty = [bool]$statusLines
    $snapshot += [pscustomobject]@{
        name = $repo.Name
        path = (Resolve-Path $path).Path
        exists = $true
        branch = Get-GitValue -RepoPath $path -GitArgs @("branch", "--show-current")
        head = Get-GitValue -RepoPath $path -GitArgs @("rev-parse", "HEAD")
        head_short = Get-GitValue -RepoPath $path -GitArgs @("rev-parse", "--short", "HEAD")
        remote = Get-GitValue -RepoPath $path -GitArgs @("remote", "get-url", "origin")
        upstream = Get-GitValue -RepoPath $path -GitArgs @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        status = (Get-GitValue -RepoPath $path -GitArgs @("status", "-sb"))
        dirty = $dirty
        timestamp = (Get-Date).ToString("o")
    }
}

if ($OutputPath) {
    $resolvedOutput = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
        $OutputPath
    } else {
        Join-Path (Get-Location) $OutputPath
    }
    $parent = Split-Path $resolvedOutput -Parent
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $snapshot | ConvertTo-Json -Depth 6 | Set-Content -Path $resolvedOutput -Encoding UTF8
}

if ($RequireClean) {
    $dirtyRepos = @($snapshot | Where-Object { -not $_.exists -or $_.dirty })
    if ($dirtyRepos.Count -gt 0) {
        $names = ($dirtyRepos | ForEach-Object { $_.name }) -join ", "
        throw "MAS-004 multirepo snapshot is not clean: $names"
    }
}

if ($AsJson) {
    $snapshot | ConvertTo-Json -Depth 6
} else {
    $snapshot | Format-Table -AutoSize name, branch, head_short, dirty, upstream
}
