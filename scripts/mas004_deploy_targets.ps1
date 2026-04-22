Set-StrictMode -Version Latest

function Get-Mas004TargetMeta {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("test", "production", "live")]
        [string]$Target
    )

    $testSsh = if ($env:MAS004_TEST_SSH) { $env:MAS004_TEST_SSH } else { "pi@10.141.94.213" }
    $productionSsh = if ($env:MAS004_PRODUCTION_SSH) { $env:MAS004_PRODUCTION_SSH } else { "pi@10.141.94.213" }
    $liveSsh = if ($env:MAS004_LIVE_SSH) { $env:MAS004_LIVE_SSH } else { "pi@192.168.210.20" }
    $testWeb = if ($env:MAS004_TEST_WEB) { $env:MAS004_TEST_WEB } else { "https://10.141.94.213:8080" }
    $productionWeb = if ($env:MAS004_PRODUCTION_WEB) { $env:MAS004_PRODUCTION_WEB } else { "https://10.141.94.213:8080" }
    $liveWeb = if ($env:MAS004_LIVE_WEB) { $env:MAS004_LIVE_WEB } else { "https://192.168.210.20:8080" }

    $targets = @{
        test = [pscustomobject]@{
            name = "test"
            role = "Local production Raspberry (former TEST, post-cutover)"
            ssh_host = $testSsh
            web_url = $testWeb
            auto_sync_default = $true
        }
        production = [pscustomobject]@{
            name = "production"
            role = "Local production Raspberry (post-cutover)"
            ssh_host = $productionSsh
            web_url = $productionWeb
            auto_sync_default = $true
        }
        live = [pscustomobject]@{
            name = "live"
            role = "Mikrotom LIVE Raspberry"
            ssh_host = $liveSsh
            web_url = $liveWeb
            auto_sync_default = $false
        }
    }

    return $targets[$Target]
}

function Resolve-Mas004SshHost {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("test", "production", "live")]
        [string]$Target,
        [string]$SshHost
    )

    if ($SshHost -and $SshHost.Trim()) {
        return $SshHost.Trim()
    }
    $meta = Get-Mas004TargetMeta -Target $Target
    return $meta.ssh_host
}
