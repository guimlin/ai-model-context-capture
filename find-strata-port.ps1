# Find the port of the running Strata API.
# The Strata app picks a new loopback port every time it starts, so nothing here
# can assume 8080. This probes each loopback listener with GET /v1/models and
# keeps the ones that answer like Strata. The logger's own port is skipped, since
# it forwards /v1/models to Strata and would answer too.
#
#   .\find-strata-port.ps1            print the port
#   .\find-strata-port.ps1 -Detailed  port, model id, process, pid
param([switch]$Detailed)

function Find-StrataPort {
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq "127.0.0.1" }
    $skip = @()
    foreach ($l in $listeners) {
        $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($l.OwningProcess)" -ErrorAction SilentlyContinue).CommandLine
        if ($cl -like "*context_proxy.py*") { $skip += $l.LocalPort }
    }
    $found = @()
    foreach ($l in ($listeners | Where-Object { $_.LocalPort -notin $skip } | Sort-Object LocalPort -Unique)) {
        $port = $l.LocalPort
        try {
            $text = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:${port}/v1/models" -TimeoutSec 2).Content
        } catch { continue }
        if ($text -match '"owned_by"\s*:\s*"strata"') {
            $model = if ($text -match '"id"\s*:\s*"([^"]+)"') { $Matches[1] } else { "?" }
            $proc = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
            $found += [pscustomobject]@{
                Port = $port; Model = $model
                Process = $proc.ProcessName; Pid = $l.OwningProcess
            }
        }
    }
    return $found
}

if ($MyInvocation.InvocationName -eq ".") { return }

$found = Find-StrataPort
if (-not $found) {
    Write-Error "no running Strata API found on loopback - start the model first (the app, or run-iq3_s.bat)"
    exit 1
}
if ($Detailed) { $found | Format-Table -AutoSize } else { $found | Select-Object -First 1 -ExpandProperty Port }
