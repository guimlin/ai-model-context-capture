# Open the model context monitor. This is the only command you need.
# Everything else - starting and stopping the capture, saving a capture file and
# opening one again, hiding rounds or deleting them out of a capture file, and
# reading the exact context of every round - is a button in the page it opens.
#
#   .\Start-ContextMonitor.ps1                     open the page at http://127.0.0.1:8790
#   .\Start-ContextMonitor.ps1 -Port 8899          put the page on another port
#   .\Start-ContextMonitor.ps1 -CapturePort 9001   the port your agents point at
#   .\Start-ContextMonitor.ps1 -NoWindow           start it without opening a browser
#   .\Start-ContextMonitor.ps1 -Upstream http://127.0.0.1:47831/v1
#       only if the page cannot find the model by itself (it looks for a loopback
#       port that answers /v1/models like a model server, since an app picks a new
#       port each start)
param(
    [int]$Port = 8790,
    [int]$CapturePort = 8788,
    [string]$Upstream = "",
    [switch]$NoWindow
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# the page counts as running if a python-ish process holds ctxweb.py on this port,
# however it was started: the launcher passes --port, but a bare `python ctxweb.py`
# (or pythonw) falls back to the default 8790, exactly like ctxweb.py's own default
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'python' -and $_.CommandLine -like '*ctxweb.py*'
} | Where-Object {
    $m = [regex]::Match($_.CommandLine, '--port[= ]+(\d+)')
    if ($m.Success) { [int]$m.Groups[1].Value -eq $Port } else { 8790 -eq $Port }
}
if ($running) {
    Write-Host "the monitor page is already running on http://127.0.0.1:$Port/ (pid $($running.ProcessId -join ', '))"
    if (-not $NoWindow) { Start-Process "http://127.0.0.1:$Port/" }
    exit 0
}

# Windows lets a second bind sit on top of a listening port without an error, so
# also check the port itself: if anything else listens there, a new page would be a
# silent zombie (and ctxweb.py refuses to bind there now anyway).
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $owner = $listener.OwningProcess | Sort-Object -Unique
    Write-Host "port $Port is already listened on by pid $($owner -join ', '); stop that page on the Monitor tab or start this one with -Port <free port>"
    exit 1
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
