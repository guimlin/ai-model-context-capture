# Start the Strata context logger in its own window.
#   .\Start-ContextMonitor.ps1              finds the running Strata port itself
#   .\Start-ContextMonitor.ps1 -Live        also opens a window that follows the log live
#   .\Start-ContextMonitor.ps1 -Upstream http://127.0.0.1:8080   (if you start Strata yourself)
param(
    [int]$Port = 8788,
    [string]$Upstream = "",
    [switch]$Live
)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $here
try {
    if (-not $Upstream) {
        # The Strata app listens on a new loopback port every start, so ask it.
        . (Join-Path $here "find-strata-port.ps1")
        $found = Find-StrataPort
        if (-not $found) {
            Write-Error "no running Strata API found on loopback - start the model first, then run this again"
            exit 1
        }
        if (@($found).Count -gt 1) {
            Write-Host "several Strata APIs answering; using the first:"
            $found | Format-Table -AutoSize
        }
        $Upstream = "http://127.0.0.1:$($found | Select-Object -First 1 -ExpandProperty Port)"
    }

    $existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*context_proxy.py*--port $Port*" }
    if ($existing) {
        Write-Host "a logger is already running on port $Port (pid $($existing.ProcessId -join ', ')); stop it first if you want a fresh one"
        exit 0
    }

    New-Item -ItemType Directory -Force -Path "logs" | Out-Null
    $p = Start-Process -FilePath "python" -ArgumentList @(
        "context_proxy.py", "--port", "$Port", "--upstream", $Upstream
    ) -WorkingDirectory $here -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "logs\logger-out.txt" -RedirectStandardError "logs\logger-err.txt"
    Start-Sleep -Seconds 2
    if ($p.HasExited) {
        Write-Error "the logger stopped again right away; last messages:"
        Get-Content "logs\logger-err.txt" -Tail 15 -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host "context logger (pid $($p.Id)): http://127.0.0.1:$Port  ->  $Upstream"
    Write-Host "logs: $(Join-Path $here 'logs')"
    Write-Host "point your agent at  http://127.0.0.1:$Port/v1   (Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:$Port)"
    if ($Live) {
        Start-Process -FilePath "python" -ArgumentList "ctxview.py", "live" -WorkingDirectory $here
    }
}
finally {
    Pop-Location
}
