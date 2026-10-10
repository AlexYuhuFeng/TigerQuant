# Push TigerQuant to GitHub, then shut the machine down at the requested time.
#
# Why a scheduled task rather than a reminder in the agent session: the push must happen whether
# or not an agent turn is running at that moment. A reminder only fires if the session is alive,
# and the whole point is that the work reaches GitHub before the machine goes off.
#
# The push runs FIRST and the shutdown second, in one script, so the ordering is guaranteed rather
# than depending on two timers not racing.

param(
    # Exercise everything except the shutdown, so the plumbing can be tested without powering the
    # machine off. An untested shutdown script is one that fails at the only moment it matters.
    [switch]$NoShutdown
)

$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\user\Desktop\TigerQuant'
$log = Join-Path $repo 'out\shutdown_push.log'
$python = Join-Path $repo '.venv\Scripts\python.exe'

New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Write-Log($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    Add-Content -Path $log -Value $line
    Write-Host $line
}

Start-Sleep -Seconds 5   # give any in-flight write a moment to settle
Set-Location $repo
Write-Log "=== final push before shutdown ==="

# Retry: the network here reaches api.github.com but not github.com, and a transient failure at
# this moment would lose the whole point of the task.
$pushed = $false
for ($attempt = 1; $attempt -le 4; $attempt++) {
    Write-Log "push attempt $attempt"
    try {
        $output = & $python 'tools\push_via_api.py' --repo 'AlexYuhuFeng/TigerQuant' --branch 'dev' --remote-branch 'main' 2>&1
        $output | ForEach-Object { Write-Log "  $_" }
        if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
    } catch {
        Write-Log "  attempt $attempt threw: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 20
}

if ($pushed) {
    Write-Log "PUSH OK"
} else {
    # Loud, and in a place a human will find it: the work is committed locally either way, so it is
    # recoverable, but the user asked for it to be ON GITHUB and this says it is not.
    Write-Log "PUSH FAILED after 4 attempts -- work is committed locally but NOT pushed"
    Add-Content -Path (Join-Path $repo 'out\PUSH_FAILED.txt') -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
}

# Final state, so the log shows exactly what was left behind.
try {
    $head = & git -C $repo log --oneline -1
    Write-Log "HEAD: $head"
    $dirty = & git -C $repo status --porcelain
    if ($dirty) { Write-Log "WARNING: working tree was NOT clean at shutdown" }
    else { Write-Log "working tree clean" }
} catch {
    Write-Log "could not read git state: $($_.Exception.Message)"
}

Write-Log "shutting down at 23:00"

# The push takes a few minutes because the API path replays history rather than sending a pack, so
# the task starts early and then WAITS. Sleeping until the target is what makes "off at 23:00" mean
# 23:00 rather than "whenever the push happened to finish".
if ($NoShutdown) {
    Write-Log "DRY RUN: -NoShutdown set, so the machine will NOT be powered off"
    Write-Log "=== dry run complete ==="
    exit 0
}

$deadline = Get-Date -Hour 23 -Minute 0 -Second 0
if ($deadline -le (Get-Date)) { $deadline = $deadline.AddDays(1) }
$wait = [int]($deadline - (Get-Date)).TotalSeconds
if ($wait -gt 0) {
    Write-Log "waiting $wait s until the 23:00 deadline"
    Start-Sleep -Seconds $wait
}

# /t 60 so the shutdown is announced and can still be cancelled with `shutdown /a` if the user
# changes their mind in the last minute.
shutdown /s /t 60 /c "TigerQuant: scheduled shutdown requested by the user (23:00)"
