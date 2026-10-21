# Open the Strata context monitor. This is the only command you need.
# Everything else - starting and stopping the capture, saving a capture file and
# opening one again, hiding rounds or deleting them out of a capture file, and
# reading the exact context of every round - is a button in the page it opens.
#
#   .\Start-ContextMonitor.ps1                     open the page at http://127.0.0.1:8790
#   .\Start-ContextMonitor.ps1 -Port 8899          put the page on another port
#   .\Start-ContextMonitor.ps1 -CapturePort 9001   the port your agents point at
#   .\Start-ContextMonitor.ps1 -NoWindow           start it without opening a browser
#   .\Start-ContextMonitor.ps1 -Upstream http://127.0.0.1:47831/v1
#       only if the page cannot find Strata by itself (it looks for a loopback port
#       that answers /v1/models like Strata, since the app picks a new port each start)
param(
    [int]$Port = 8790,
    [int]$CapturePort = 8788,
    [string]$Upstream = "",
    [switch]$NoWindow
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*ctxweb.py*--port $Port*" }
if ($running) {
    Write-Host "the monitor page is already running on http://127.0.0.1:$Port/ (pid $($running.ProcessId -join ', '))"
    if (-not $NoWindow) { Start-Process "http://127.0.0.1:$Port/" }
    exit 0
}

$argv = @("ctxweb.py", "--port", "$Port", "--proxy-port", "$CapturePort")
if ($Upstream) { $argv += @("--upstream", $Upstream) }
if ($NoWindow) { $argv += "--no-open" }

$p = Start-Process -FilePath "python" -ArgumentList $argv -WorkingDirectory $here -PassThru
Write-Host "context monitor page (pid $($p.Id)): http://127.0.0.1:$Port/"
if ($NoWindow) {
    Write-Host "started without a browser window; open http://127.0.0.1:$Port/ when you want it"
}
Write-Host "capture port for agents: http://127.0.0.1:$CapturePort/v1   (Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:$CapturePort)"
