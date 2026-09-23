<#
.SYNOPSIS
  Set up coord on one Windows machine: server, local CA, agent identities, the `coord` command,
  and (optionally) Codex's sandbox and a logon task for the server. Safe to run again.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\setup-windows.ps1 -Codex -AutoStart

.PARAMETER Identity
  Agent identities to enroll (default: claude). The first becomes the default identity.
.PARAMETER Codex
  Also enroll `codex` and let Codex's Windows sandbox accounts read that identity.
.PARAMETER AutoStart
  Register a "coord-server" scheduled task that starts the server at logon (and start it now).
#>
param(
    [string[]]$Identity = @("claude"),
    [switch]$Codex,
    [switch]$AutoStart
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path $PSScriptRoot -Parent
$ConfigHome = Join-Path $HOME ".config\coord"
$Bin = Join-Path $HOME ".local\bin"
$Cli = Join-Path $Repo "plugins\coord\client\cli.js"
$Warnings = New-Object System.Collections.Generic.List[string]

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "   ok  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "   !!  $msg" -ForegroundColor Yellow; $Warnings.Add($msg) }
function Fail($msg) { Write-Host "   xx  $msg" -ForegroundColor Red; exit 1 }
function Version($text) { if ($text -match '(\d+)\.(\d+)\.(\d+)') { [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])" } }

Set-Location $Repo

# --- prerequisites ------------------------------------------------------------------------------
Step "Prerequisites"
$uvs = @(Get-Command uv -All -ErrorAction SilentlyContinue)
if (-not $uvs) { Fail "uv not found: https://docs.astral.sh/uv/getting-started/installation/" }
$uvVer = Version (& $uvs[0].Source --version)
if ($uvVer -lt [version]"0.11.0") {
    Fail "uv $uvVer at $($uvs[0].Source) is too old (needs >= 0.11 for 'system-certs'): 'uv self update', or remove this copy if another uv is installed"
}
Ok "uv $uvVer ($($uvs[0].Source))"
if ($uvs.Count -gt 1) { Warn "several uv on PATH ($(($uvs | ForEach-Object Source) -join ', ')); the first one wins - remove the stale ones" }

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { Fail "Node.js >= 20 not found (the coord client and plugin need it)" }
$nodeVer = Version (& node --version)
if ($nodeVer -lt [version]"20.0.0") { Fail "Node $nodeVer is too old (needs >= 20)" }
Ok "node $nodeVer"

$listener = Get-NetTCPConnection -LocalPort 1337 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)").CommandLine
    if ($cmd -match "ACP_server") { Fail "the old ACP server (0.2) holds port 1337 (PID $($listener.OwningProcess)): stop it, then run this again" }
}

# --- server package -----------------------------------------------------------------------------
Step "Server package (uv sync)"
uv sync --quiet
if ($LASTEXITCODE) { Fail "uv sync failed" }
$openssl = uv run --quiet python -c "from coordination.sslbin import openssl; print(openssl())"
if ($LASTEXITCODE) { Fail "no openssl: install Git for Windows, or set COORD_OPENSSL to an openssl.exe" }
Ok "openssl: $openssl (override with COORD_OPENSSL)"

# --- CA, server certificate, identities ---------------------------------------------------------
Step "Local CA and identities"
uv run --quiet coord-admin init | Out-Null
if ($LASTEXITCODE) { Fail "coord-admin init failed (see above)" }
Ok "CA: $Repo\pki\ca.crt"
if (-not (Test-Path "$Repo\pki\localhost.crt")) {
    uv run --quiet coord-admin server-cert | Out-Null
    if ($LASTEXITCODE) { Fail "coord-admin server-cert failed" }
}
Ok "server certificate: $Repo\pki\localhost.crt (renews itself)"
$names = @($Identity)
if ($Codex -and $names -notcontains "codex") { $names += "codex" }
foreach ($n in $names) {
    if (Test-Path (Join-Path $ConfigHome "$n\env")) { Ok "identity $n (already enrolled)"; continue }
    uv run --quiet coord-admin enroll $n | Out-Null
    if ($LASTEXITCODE) { Fail "coord-admin enroll $n failed" }
    Ok "identity $n -> $ConfigHome\$n"
}

# --- the coord command --------------------------------------------------------------------------
Step "coord command"
New-Item -ItemType Directory -Force $Bin | Out-Null
Set-Content -Encoding ASCII (Join-Path $Bin "coord.cmd") "@node `"$Cli`" %*"
Ok "$Bin\coord.cmd -> plugins\coord\client\cli.js (the bundled client: no npm build needed)"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $Bin) {
    [Environment]::SetEnvironmentVariable("Path", ($Bin + ";" + $userPath).TrimEnd(";"), "User")
    Warn "added $Bin to your user PATH: open a new terminal to use 'coord'"
}
$env:Path = "$Bin;$env:Path"

# --- Codex sandbox ------------------------------------------------------------------------------
if ($Codex) {
    Step "Codex"
    $codexId = Join-Path $ConfigHome "codex"
    $accounts = @(Get-LocalUser -ErrorAction SilentlyContinue | Where-Object Name -like "CodexSandbox*" | ForEach-Object Name)
    if ($accounts) {
        # The elevated sandbox runs commands as these accounts: they must read the codex identity (only that one).
        $grants = $accounts | ForEach-Object { "${_}:(OI)(CI)RX" }
        icacls $codexId /grant $grants | Out-Null
        Ok "read access to $codexId for $($accounts -join ', ')"
    } else {
        Ok "no Codex sandbox accounts yet (re-run with -Codex after Codex's first sandboxed command)"
    }
    $codexConfig = Join-Path $HOME ".codex\config.toml"
    $want = "COORD_CONFIG = '$codexId\env'"
    if ((Test-Path $codexConfig) -and (Select-String -Path $codexConfig -SimpleMatch "COORD_CONFIG" -Quiet)) {
        Ok "COORD_CONFIG already set in $codexConfig"
    } else {
        Warn "add to $codexConfig (under [shell_environment_policy.set]), then restart Codex:  $want"
    }
    foreach ($old in @("$HOME\.codex\skills\acp-client", "$HOME\.codex\plugins\cache\acp-agent-coordination\acp")) {
        if (Test-Path $old) { Warn "obsolete ACP leftover (makes Codex look for ACP_client.py): $old - move or delete it" }
    }
}
if (Test-Path "$HOME\.claude\skills\acp-client") { Warn "obsolete ACP skill: $HOME\.claude\skills\acp-client - move or delete it" }
foreach ($cfg in @("$HOME\.codex\config.toml", "$HOME\.claude\plugins\known_marketplaces.json")) {
    if ((Test-Path $cfg) -and (Select-String -Path $cfg -SimpleMatch "acp-agent-coordination" -Quiet)) {
        Warn "$cfg still uses the old marketplace 'acp-agent-coordination': see README 'Renamed acp-agent-coordination -> coord'"
    }
}

# --- server ------------------------------------------------------------------------------------
$server = "$Repo\.venv\Scripts\coord-server.exe"
$serverArgs = "--pki `"$Repo\pki`""
if ($AutoStart) {
    Step "Logon task"
    $log = "$Repo\server.log"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Repo -Argument (
        "-NoProfile -WindowStyle Hidden -Command `"& '$server' --pki '$Repo\pki' *>> '$log'`"")
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
    Register-ScheduledTask -TaskName "coord-server" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Ok "scheduled task 'coord-server' (at logon; log: $log)"
    if (-not (Get-NetTCPConnection -LocalPort 1337 -State Listen -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName "coord-server"
        Start-Sleep -Seconds 3
    }
}

Step "Check"
if (Get-NetTCPConnection -LocalPort 1337 -State Listen -ErrorAction SilentlyContinue) {
    $out = & node $Cli --json locks 2>&1
    if ($LASTEXITCODE) { Warn "the server answers on 1337 but coord failed: $out" } else { Ok "coord reaches https://localhost:1337" }
} else {
    Warn "coord-server is not running: start it with  & '$server' $serverArgs  (or re-run with -AutoStart)"
}

Write-Host "`nClaude Code:  claude plugin marketplace add `"$Repo`"; claude plugin install coord@coord"
if ($Codex) { Write-Host "Codex:        codex plugin marketplace add `"$Repo`"; codex plugin add coord@coord   (then: `$coord join)" }
if ($Warnings.Count) { Write-Host "`n$($Warnings.Count) thing(s) to do - see !! above." -ForegroundColor Yellow } else { Write-Host "`nAll set." -ForegroundColor Green }
