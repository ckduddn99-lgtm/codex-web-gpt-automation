param(
    [string]$Root = "C:\",
    [ValidateSet("legacy-drive", "parallel-exact-unit")]
    [string]$ScopeMode = "legacy-drive",
    [string]$TopologyBinding = "",
    [int]$Port = 0,
    [string]$Token = "",
    [ValidateSet("auto", "ngrok", "cloudflare")]
    [string]$TunnelProvider = "auto",
    [string]$Hostname = "",
    [string]$NpxPath = "",
    [string]$NgrokPath = "",
    [string]$NgrokConfig = "",
    [string]$LogRoot = "",
    [int]$WaitSeconds = 90,
    [switch]$ForceRecreate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$CodexHome = Join-Path $env:USERPROFILE ".codex"
if (-not $LogRoot) { $LogRoot = Join-Path $CodexHome "state\codexpro-project-apps" }
$Manager = Join-Path $CodexHome "bin\codexpro_project_app_manager.py"
$Identity = Join-Path $CodexHome "bin\codexpro_mcp_identity.py"
$Npx = $NpxPath
if (-not $Npx) {
    foreach ($candidate in @("npx.cmd", "npx")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            $Npx = if ($command.Source) { $command.Source } else { $command.Path }
            break
        }
    }
}
if (-not (Test-Path -LiteralPath $Manager)) { throw "project app manager not found: $Manager" }
if (-not (Test-Path -LiteralPath $Identity)) { throw "CodexPro identity helper not found: $Identity" }
if (-not $Npx -or -not (Test-Path -LiteralPath $Npx)) {
    throw "npx not found on PATH; install Node.js or pass -NpxPath explicitly"
}

function Invoke-ManagerJson {
    param([string[]]$ManagerArgs)
    $raw = & python $Manager @ManagerArgs
    if ($LASTEXITCODE -ne 0) { throw "manager failed: $raw" }
    return $raw | ConvertFrom-Json
}

function Stop-OwnedBootstrapProcess {
    param([System.Diagnostics.Process]$OwnedProcess)
    if (-not $OwnedProcess) { return $false }
    try {
        if ($OwnedProcess.HasExited) { return $false }
        $taskkill = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList @("/PID", "$($OwnedProcess.Id)", "/T", "/F") -WindowStyle Hidden -Wait -PassThru
        return ($taskkill.ExitCode -eq 0)
    } catch {
        return $false
    }
}

if ($ScopeMode -eq "parallel-exact-unit") {
    throw "EXACT_UNIT_BOOTSTRAP_ENTRYPOINT_REQUIRED: use codexpro_exact_unit_cloudflare_bootstrap.ps1"
}
$decisionArgs = @("--root", $Root, "--scope-mode", $ScopeMode)
if ($ScopeMode -eq "parallel-exact-unit") {
    if ($TopologyBinding -notmatch "^[0-9a-f]{64}$") {
        throw "EXACT_UNIT_TOPOLOGY_BINDING_INVALID"
    }
    if ($TunnelProvider -notin @("auto", "cloudflare")) {
        throw "EXACT_UNIT_CLOUDFLARE_REQUIRED"
    }
    $decisionArgs += @("--topology-receipt-sha256", $TopologyBinding, "--no-git-root")
}
if ($Port -gt 0) { $decisionArgs += @("--port", "$Port") }
if ($ForceRecreate) { $decisionArgs += "--force-recreate" }
$decision = Invoke-ManagerJson -ManagerArgs $decisionArgs
$ResolvedRoot = [string]$decision.root
$AppName = [string]$decision.app_name
$ResolvedPort = [int]$decision.port
$RequestedHostname = $Hostname.Trim()
$TokenSource = if ($Token) { "argument" } else { "none" }
Write-Verbose "CodexPro bootstrap decision resolved for $ResolvedRoot on port $ResolvedPort"
if (-not $Token -and $decision.public_url) {
    $tokenMatch = [regex]::Match([string]$decision.public_url, "codexpro_token=([^&\s`"']+)")
    if ($tokenMatch.Success) {
        $Token = $tokenMatch.Groups[1].Value
        $TokenSource = "registry"
    }
}
function Get-NormalizedDriveRoot {
    param([string]$PathValue)
    if (-not $PathValue) { return "" }
    try {
        $fullPath = [System.IO.Path]::GetFullPath($PathValue)
        return ([System.IO.Path]::GetPathRoot($fullPath)).TrimEnd([char[]]"\/").ToUpperInvariant()
    } catch {
        return ""
    }
}

function Get-NormalizedPath {
    param([string]$PathValue)
    if (-not $PathValue) { return "" }
    try {
        return ([System.IO.Path]::GetFullPath($PathValue)).TrimEnd([char[]]"\/").ToUpperInvariant()
    } catch {
        return ""
    }
}

function Test-NormalizedRootScope {
    param(
        [string]$RequestedPath,
        [string]$ResolvedPath
    )
    $requested = Get-NormalizedPath -PathValue $RequestedPath
    $resolved = Get-NormalizedPath -PathValue $ResolvedPath
    if (-not $requested -or -not $resolved) { return $false }
    if ($requested -eq $resolved) { return $true }
    $requestedDrive = Get-NormalizedDriveRoot -PathValue $requested
    $resolvedDrive = Get-NormalizedDriveRoot -PathValue $resolved
    if (-not $requestedDrive -or $requestedDrive -ne $resolvedDrive) { return $false }
    if ($resolved -eq $resolvedDrive) { return $true }
    $resolvedPrefix = $resolved + [System.IO.Path]::DirectorySeparatorChar
    return $requested.StartsWith($resolvedPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

$DriveTunnelPolicyPath = Join-Path $LogRoot "drive-tunnel-policy.json"
$DriveTunnelPolicy = $null
if (Test-Path -LiteralPath $DriveTunnelPolicyPath) {
    try { $DriveTunnelPolicy = Get-Content -LiteralPath $DriveTunnelPolicyPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { throw "invalid drive tunnel policy: $DriveTunnelPolicyPath" }
}
$ResolvedDrive = Get-NormalizedDriveRoot -PathValue $ResolvedRoot
$DrivePolicyEntry = $null
if ($DriveTunnelPolicy -and $DriveTunnelPolicy.drives) {
    $DrivePolicyEntry = $DriveTunnelPolicy.drives.PSObject.Properties |
        Where-Object { (Get-NormalizedDriveRoot -PathValue $_.Name) -eq $ResolvedDrive } |
        Select-Object -First 1 -ExpandProperty Value
}
$RequestedTunnelProvider = $TunnelProvider
if ($RequestedTunnelProvider -eq "auto") {
    $RequestedTunnelProvider = if ($DrivePolicyEntry -and $DrivePolicyEntry.provider) { [string]$DrivePolicyEntry.provider } elseif ($DriveTunnelPolicy -and $DriveTunnelPolicy.default_provider) { [string]$DriveTunnelPolicy.default_provider } else { "cloudflare" }
}
if ($RequestedTunnelProvider -notin @("ngrok", "cloudflare")) { throw "drive tunnel policy selected an unsupported provider: $RequestedTunnelProvider" }
if ($DrivePolicyEntry -and $DrivePolicyEntry.provider -and $TunnelProvider -ne "auto" -and $RequestedTunnelProvider -ne [string]$DrivePolicyEntry.provider) {
    throw "DRIVE_TUNNEL_POLICY_MISMATCH: $ResolvedDrive requires $($DrivePolicyEntry.provider); refusing $RequestedTunnelProvider"
}
if (-not $RequestedHostname -and $DrivePolicyEntry -and $DrivePolicyEntry.hostname) { $RequestedHostname = [string]$DrivePolicyEntry.hostname }
if (-not $NgrokPath -and $DrivePolicyEntry -and $DrivePolicyEntry.ngrok_path) { $NgrokPath = [string]$DrivePolicyEntry.ngrok_path }
if (-not $NgrokConfig -and $DrivePolicyEntry -and $DrivePolicyEntry.ngrok_config) { $NgrokConfig = [string]$DrivePolicyEntry.ngrok_config }

function Get-QueryValue {
    param(
        [System.Uri]$Uri,
        [string]$Name
    )
    foreach ($part in $Uri.Query.TrimStart("?").Split("&", [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $pair = $part.Split("=", 2)
        if ($pair.Count -ne 2) { continue }
        $key = [System.Uri]::UnescapeDataString($pair[0])
        if ($key -ceq $Name) {
            return [System.Uri]::UnescapeDataString($pair[1])
        }
    }
    return ""
}

function Test-CompleteFixedNgrokRegistryContract {
    if ($RequestedTunnelProvider -ne "ngrok") {
        return [pscustomobject][ordered]@{ ok = $false; reason = "dynamic-provider-requested"; identity_required = $false }
    }
    if (-not $RequestedHostname) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-hostname-missing"; identity_required = $false }
    }
    if (-not $Token) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-token-missing"; identity_required = $false }
    }
    $registeredUrl = [string]$decision.public_url
    if (-not $registeredUrl) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-registry-url-missing"; identity_required = $false }
    }
    try {
        $registeredUri = [System.Uri]$registeredUrl
    } catch {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-registry-url-invalid"; identity_required = $false }
    }
    if ($registeredUri.Scheme -ne "https") {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-registry-scheme-mismatch"; identity_required = $false }
    }
    $queryKey = "codexpro" + "_token"
    $registeredToken = Get-QueryValue -Uri $registeredUri -Name $queryKey
    if (-not $registeredToken) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-registry-token-missing"; identity_required = $false }
    }
    $requestedDrive = Get-NormalizedDriveRoot -PathValue $Root
    $resolvedDrive = Get-NormalizedDriveRoot -PathValue $ResolvedRoot
    if (-not $requestedDrive -or -not $resolvedDrive -or $requestedDrive -ne $resolvedDrive) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-root-drive-mismatch"; identity_required = $false }
    }
    if (-not (Test-NormalizedRootScope -RequestedPath $Root -ResolvedPath $ResolvedRoot)) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-root-scope-mismatch"; identity_required = $false }
    }
    if ($Port -gt 0 -and $Port -ne $ResolvedPort) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-port-registry-mismatch"; identity_required = $false }
    }
    if ($registeredUri.AbsolutePath.TrimEnd("/") -ne "/mcp") {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-registry-path-mismatch"; identity_required = $false }
    }
    if (-not $registeredUri.Host.Equals($RequestedHostname, [System.StringComparison]::OrdinalIgnoreCase)) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-hostname-registry-mismatch"; identity_required = $false }
    }
    if ($registeredToken -cne $Token) {
        return [pscustomobject][ordered]@{ ok = $false; reason = "fixed-token-registry-mismatch"; identity_required = $false }
    }
    return [pscustomobject][ordered]@{
        ok = $true
        reason = "fixed-registry-contract-matched-identity-pending"
        identity_required = $true
    }
}

$FixedNgrokContract = Test-CompleteFixedNgrokRegistryContract
if ($RequestedTunnelProvider -eq "ngrok" -and -not [bool]$FixedNgrokContract.ok) {
    throw "FIXED_NGROK_CONTRACT_INVALID: $($FixedNgrokContract.reason); dynamic fallback is forbidden for this drive"
}
$EffectiveTunnelProvider = "cloudflare"
$EffectiveHostname = ""
if ($RequestedTunnelProvider -eq "ngrok" -and [bool]$FixedNgrokContract.ok) {
    if (-not $RequestedHostname) {
        throw "ngrok requires a reserved hostname; pass -Hostname <your-domain>.ngrok-free.dev"
    }
    if (-not $NgrokPath) {
        $ngrokCommand = Get-Command "ngrok.exe", "ngrok" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $ngrokCommand) {
            throw "ngrok was requested but was not found on PATH"
        }
        $NgrokPath = if ($ngrokCommand.Source) { $ngrokCommand.Source } else { $ngrokCommand.Path }
    }
    $EffectiveTunnelProvider = "ngrok"
    $EffectiveHostname = $RequestedHostname
}

if (-not [bool]$FixedNgrokContract.ok -and $TokenSource -eq "registry") {
    $Token = ""
    $TokenSource = "none"
}
$ProjectLogDir = Join-Path $LogRoot $decision.slug
New-Item -ItemType Directory -Force -Path $ProjectLogDir | Out-Null

function Redact-TokenText {
    param([string]$Text)
    return ($Text -replace '(codexpro_token=)[^&\s"'']+', '$1<redacted>')
}

function Write-StateJson {
    param([object]$Payload)
    $json = ($Payload | ConvertTo-Json -Depth 8)
    $json = Redact-TokenText -Text $json
    $json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Output $json
}

if ($DryRun) {
    [ordered]@{
        status = "dry-run"
        root = $ResolvedRoot
        app_name = $AppName
        requested_port = $Port
        resolved_port = $ResolvedPort
        public_url = $decision.public_url
        action = $decision.action
        token_supplied = [bool]$Token
        requested_tunnel_provider = $RequestedTunnelProvider
        effective_tunnel_provider = $EffectiveTunnelProvider
        requested_hostname = $RequestedHostname
        effective_hostname = $EffectiveHostname
        fixed_ngrok_registry_contract = [bool]$FixedNgrokContract.ok
        fixed_ngrok_contract_reason = [string]$FixedNgrokContract.reason
        public_identity_required = [bool]$FixedNgrokContract.identity_required
    } | ConvertTo-Json -Depth 4
    exit 0
}

$outLog = Join-Path $ProjectLogDir "cloudflare.out.log"
$errLog = Join-Path $ProjectLogDir "cloudflare.err.log"
$statePath = Join-Path $ProjectLogDir "last-bootstrap.json"
Remove-Item -LiteralPath $outLog, $errLog -Force -ErrorAction SilentlyContinue
$preClipboardText = ""
try {
    $preClipboardText = (Get-Clipboard -Raw -ErrorAction Stop).Trim()
} catch {
    $preClipboardText = ""
}

function Get-RuntimeCandidateUrl {
    param(
        [datetime]$MinLastWriteTime = [datetime]::MinValue
    )
    $runtimeDir = Join-Path $env:USERPROFILE ".codexpro\runtime"
    if (-not (Test-Path -LiteralPath $runtimeDir)) {
        return $null
    }
    $runtimeFiles = Get-ChildItem -LiteralPath $runtimeDir -Filter "*.json" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 12
    foreach ($runtimeFile in $runtimeFiles) {
        if ($runtimeFile.LastWriteTime -lt $MinLastWriteTime) {
            continue
        }
        try {
            $runtime = Get-Content -LiteralPath $runtimeFile.FullName -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        } catch {
            continue
        }
        $runtimeRoot = [string]$runtime.root
        $runtimeLocalBase = [string]$runtime.localBase
        $runtimeEndpoint = ([string]$runtime.endpoint).TrimEnd("/")
        $expectedLocalBase = "http://127.0.0.1:$ResolvedPort"
        $endpointMatches = $runtimeEndpoint -match "^https://[^\s`"']+/mcp$"
        if ($EffectiveTunnelProvider -eq "ngrok" -and $EffectiveHostname) {
            $expectedNgrokEndpoint = "https://$EffectiveHostname/mcp"
            $endpointMatches = ($runtimeEndpoint -eq $expectedNgrokEndpoint)
        } elseif ($EffectiveTunnelProvider -eq "cloudflare") {
            $endpointMatches = $runtimeEndpoint -match "^https://[^\s`"']+\.trycloudflare\.com/mcp$"
        }
        if ($runtimeRoot -eq $ResolvedRoot -and $runtimeLocalBase -eq $expectedLocalBase -and $endpointMatches) {
            if ($Token) {
                return "${runtimeEndpoint}?codexpro_token=$Token"
            }
            $runtimeLocalStatus = [string]$runtime.localStatusUrl
            $tokenMatch = [regex]::Match($runtimeLocalStatus, "codexpro_token=([^&\s`"']+)")
            if ($tokenMatch.Success -and $tokenMatch.Groups[1].Value -ne "<redacted>") {
                return "${runtimeEndpoint}?codexpro_token=$($tokenMatch.Groups[1].Value)"
            }
        }
    }
    return $null
}

function Test-CodexProIdentity {
    param([string]$CandidateUrl)
    $pythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) {
        return [pscustomobject][ordered]@{
            ok = $false
            result = @{ ok = $false; reason = "identity-python-unavailable" }
        }
    }
    $probeId = [guid]::NewGuid().ToString("N")
    $identityOut = Join-Path $ProjectLogDir "identity-$probeId.out.tmp"
    $identityErr = Join-Path $ProjectLogDir "identity-$probeId.err.tmp"
    $identityProcess = $null
    $identityExit = -1
    $identityRaw = ""
    $identityError = ""
    try {
        $pythonPath = if ($pythonCommand.Source) { $pythonCommand.Source } else { $pythonCommand.Path }
        $identityScriptArg = if ($Identity -match "\s") { "`"$Identity`"" } else { $Identity }
        $rootArg = if ($ResolvedRoot -match "\s") { "`"$ResolvedRoot`"" } else { $ResolvedRoot }
        $identityArgs = @(
            $identityScriptArg,
            "--url", $CandidateUrl,
            "--expected-root", $rootArg,
            "--expected-port", "$ResolvedPort",
            "--timeout", "12"
        )
        $identityProcess = Start-Process -FilePath $pythonPath -ArgumentList $identityArgs -WindowStyle Hidden -RedirectStandardOutput $identityOut -RedirectStandardError $identityErr -PassThru
        if (-not $identityProcess.WaitForExit(16000)) {
            [void](Stop-OwnedBootstrapProcess -OwnedProcess $identityProcess)
            return [pscustomobject][ordered]@{
                ok = $false
                result = @{ ok = $false; reason = "identity-probe-timeout" }
            }
        }
        $identityProcess.WaitForExit()
        $identityProcess.Refresh()
        $identityExit = $identityProcess.ExitCode
        if (Test-Path -LiteralPath $identityOut) {
            $identityRaw = Get-Content -LiteralPath $identityOut -Raw -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $identityErr) {
            $identityError = Get-Content -LiteralPath $identityErr -Raw -ErrorAction SilentlyContinue
        }
    } finally {
        Remove-Item -LiteralPath $identityOut, $identityErr -Force -ErrorAction SilentlyContinue
    }
    $identityResult = $null
    try {
        $identityResult = $identityRaw | ConvertFrom-Json
    } catch {
        $identityResult = @{
            ok = $false
            reason = "identity-json-parse-failed"
            raw = (Redact-TokenText -Text ($identityRaw -join "`n"))
            stderr = (Redact-TokenText -Text $identityError)
        }
    }
    if ($null -eq $identityExit -or [string]$identityExit -eq "") {
        $identityExit = if ([bool]$identityResult.ok) { 0 } else { -1 }
    }
    return [pscustomobject][ordered]@{
        ok = ($identityExit -eq 0 -and [bool]$identityResult.ok)
        result = $identityResult
        exit_code = $identityExit
    }
}

function Test-CodexProIdentityWithRetry {
    param(
        [string]$CandidateUrl,
        [int]$MaxSeconds = 24
    )
    $deadline = (Get-Date).AddSeconds([Math]::Max(4, $MaxSeconds))
    $attempts = 0
    $probe = $null
    do {
        $attempts++
        $probe = Test-CodexProIdentity -CandidateUrl $CandidateUrl
        if ([bool]$probe.ok -or (Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 2
    } while ($true)
    return [pscustomobject][ordered]@{
        ok = [bool]$probe.ok
        result = $probe.result
        attempts = $attempts
        exit_code = $probe.exit_code
    }
}

function Test-LocalTcpPort {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$TargetPort,
        [int]$TimeoutMs = 2000
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync($HostName, $TargetPort)
        if (-not $connectTask.Wait([Math]::Max(100, $TimeoutMs))) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-FixedRuntimeWatchdog {
    if ($EffectiveTunnelProvider -ne "ngrok" -or -not $EffectiveHostname) { return $null }
    $watchdog = Join-Path $CodexHome "bin\codexpro_fixed_runtime_watchdog.py"
    if (-not (Test-Path -LiteralPath $watchdog)) { return $null }
    $python = Get-Command "pythonw.exe", "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $python) { return $null }
    $pythonPath = if ($python.Source) { $python.Source } else { $python.Path }
    $args = @(
        $watchdog,
        "--root", $ResolvedRoot,
        "--port", "$ResolvedPort",
        "--hostname", $EffectiveHostname,
        "--bootstrap", $PSCommandPath,
        "--state-dir", $ProjectLogDir
    )
    return Start-Process -FilePath $pythonPath -ArgumentList $args -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -PassThru
}

function Get-ExistingFixedNgrokProcess {
    if ($EffectiveTunnelProvider -ne "ngrok" -or -not $EffectiveHostname) { return $null }
    $expectedUpstream = "127.0.0.1:$ResolvedPort"
    try {
        $processes = Get-CimInstance Win32_Process -Filter "Name = 'ngrok.exe'" -ErrorAction Stop
    } catch {
        return $null
    }
    foreach ($candidate in $processes) {
        $commandLine = [string]$candidate.CommandLine
        if (-not $commandLine) { continue }
        if ($commandLine.IndexOf($EffectiveHostname, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        if ($commandLine.IndexOf($expectedUpstream, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
        return $candidate
    }
    return $null
}

function Resolve-CachedCodexProCli {
    $cacheRoot = Join-Path $env:LOCALAPPDATA "npm-cache\_npx"
    if (-not (Test-Path -LiteralPath $cacheRoot)) { return $null }
    $candidates = Get-ChildItem -LiteralPath $cacheRoot -Directory -ErrorAction SilentlyContinue |
        ForEach-Object {
            $script = Join-Path $_.FullName "node_modules\codexpro\scripts\codexpro.mjs"
            if (Test-Path -LiteralPath $script) { Get-Item -LiteralPath $script }
        } |
        Sort-Object LastWriteTime -Descending
    return ($candidates | Select-Object -First 1)
}

if (Test-LocalTcpPort -TargetPort $ResolvedPort) {
    Write-Verbose "Existing listener detected on port $ResolvedPort"
    $existingUrl = Get-RuntimeCandidateUrl
    if (-not $existingUrl -and $EffectiveTunnelProvider -eq "ngrok" -and $EffectiveHostname -and $Token) {
        $registeredUrl = [string]$decision.public_url
        $expectedPrefix = "https://$EffectiveHostname/mcp?codexpro_token="
        if ($registeredUrl.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $existingUrl = $registeredUrl
        }
    }
    if ($existingUrl) {
        Write-Verbose "Validating the registered fixed-address runtime before reuse"
        $existingIdentity = Test-CodexProIdentityWithRetry -CandidateUrl $existingUrl -MaxSeconds ([Math]::Min(30, [Math]::Max(8, $WaitSeconds)))
        Write-Verbose "Existing runtime identity result: ok=$([bool]$existingIdentity.ok), attempts=$($existingIdentity.attempts), reason=$($existingIdentity.result.reason), exit=$($existingIdentity.exit_code)"
        if ([bool]$existingIdentity.ok) {
            Write-Verbose "Refreshing the existing registry record without re-registering the ChatGPT app"
            $updateArgs = @("--root", $ResolvedRoot, "--public-url", "$existingUrl", "--port", "$ResolvedPort", "--verified-open-port", "--update")
            if ($ForceRecreate) { $updateArgs += "--force-recreate" }
            $updated = Invoke-ManagerJson -ManagerArgs $updateArgs
            $watchdogProcess = Start-FixedRuntimeWatchdog
            $payload = [ordered]@{
                status = "ready"
                root = $ResolvedRoot
                app_name = $updated.app_name
                port = $ResolvedPort
                public_url = $updated.public_url
                action = "reuse-existing-runtime-identity-verified"
                old_app_name = $updated.old_app_name
                old_public_url = $updated.old_public_url
                chrome_next_action = $updated.chrome_next_action
                transaction_id = $updated.transaction_id
                identity = $existingIdentity.result
                identity_attempts = $existingIdentity.attempts
                process_id = $null
                watchdog_start_requested = [bool]$watchdogProcess
                watchdog_process_id = if ($watchdogProcess) { $watchdogProcess.Id } else { $null }
                out_log = $outLog
                err_log = $errLog
            }
            Write-StateJson -Payload $payload
            exit 0
        }
    }
    $payload = [ordered]@{
        status = "port-occupied-identity-mismatch"
        root = $ResolvedRoot
        app_name = $AppName
        port = $ResolvedPort
        public_url = $decision.public_url
        action = "blocked-without-starting-a-duplicate-runtime"
        process_id = $null
        out_log = $outLog
        err_log = $errLog
        next_action = "inspect the existing listener; do not replace or duplicate the registered fixed-address runtime"
    }
    Write-StateJson -Payload $payload
    exit 2
}

$commonArgs = @(
    "--root", $ResolvedRoot,
    "--no-profile",
    "--bash", "full",
    "--write", "workspace",
    "--tool-mode", "full",
    "--allow-home",
    "--port", "$ResolvedPort"
)
$launchFile = $Npx
$launchArgs = @()
$launchMode = "start-new-dynamic-cloudflare-tunnel"
$publicUrl = $null
$fixedNgrokProcess = Get-ExistingFixedNgrokProcess
if ($fixedNgrokProcess) {
    $registeredUrl = [string]$decision.public_url
    $expectedPrefix = "https://$EffectiveHostname/mcp?codexpro_token="
    if (-not $Token -or -not $registeredUrl.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $payload = [ordered]@{
            status = "fixed-tunnel-present-but-address-contract-incomplete"
            root = $ResolvedRoot
            app_name = $AppName
            port = $ResolvedPort
            public_url = $decision.public_url
            action = "blocked-without-replacing-the-fixed-tunnel"
            ngrok_process_id = $fixedNgrokProcess.ProcessId
            next_action = "restore the registered full /mcp URL and token before starting the local server"
        }
        Write-StateJson -Payload $payload
        exit 2
    }
    $cachedCli = Resolve-CachedCodexProCli
    $nodeCommand = Get-Command "node.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cachedCli -or -not $nodeCommand) {
        $payload = [ordered]@{
            status = "fixed-tunnel-present-local-launcher-unavailable"
            root = $ResolvedRoot
            app_name = $AppName
            port = $ResolvedPort
            public_url = $decision.public_url
            action = "blocked-without-touching-the-fixed-tunnel"
            ngrok_process_id = $fixedNgrokProcess.ProcessId
            next_action = "restore the cached CodexPro CLI and Node.js; do not launch a second ngrok tunnel"
        }
        Write-StateJson -Payload $payload
        exit 2
    }
    $launchFile = if ($nodeCommand.Source) { $nodeCommand.Source } else { $nodeCommand.Path }
    $launchArgs = @($cachedCli.FullName, "start") + $commonArgs + @("--tunnel", "none", "--token", $Token)
    $launchMode = "reuse-fixed-tunnel-start-local-server-only"
    $publicUrl = $registeredUrl
} else {
    $launchArgs = @(
        "codexpro@latest",
        $(if ($EffectiveTunnelProvider -eq "ngrok") { "ngrok" } else { "start" })
    ) + $commonArgs
    if ($EffectiveTunnelProvider -eq "ngrok") {
        $launchMode = "start-new-fixed-ngrok-tunnel"
        $launchArgs += @("--tunnel", "ngrok")
        if ($EffectiveHostname) { $launchArgs += @("--hostname", $EffectiveHostname) }
        if ($NgrokPath) { $launchArgs += @("--ngrok", $NgrokPath) }
        if ($NgrokConfig) { $launchArgs += @("--ngrok-config", $NgrokConfig) }
    } else {
        $launchArgs += @("--tunnel", "cloudflare")
    }
    if ($Token) { $launchArgs += @("--token", $Token) }
}
$processStartedAt = Get-Date
$process = Start-Process -FilePath $launchFile -ArgumentList $launchArgs -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    if ($publicUrl) { break }
    $texts = @()
    if (Test-Path -LiteralPath $outLog) { $texts += (Get-Content -LiteralPath $outLog -Raw -ErrorAction SilentlyContinue) }
    if (Test-Path -LiteralPath $errLog) { $texts += (Get-Content -LiteralPath $errLog -Raw -ErrorAction SilentlyContinue) }
    $joined = $texts -join "`n"
    if ($EffectiveTunnelProvider -eq "ngrok" -and $EffectiveHostname) {
        $exactPattern = [regex]::Escape("https://$EffectiveHostname/mcp?codexpro_token=") + "[^\s`"']+"
    } else {
        $exactPattern = "https://[^\s`"']+\.trycloudflare\.com/mcp\?codexpro_token=[^\s`"']+"
    }
    $exact = [regex]::Match($joined, $exactPattern)
    if ($exact.Success) {
        $publicUrl = $exact.Value.TrimEnd("/")
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $publicUrl) {
    try {
        $clipboardText = (Get-Clipboard -Raw -ErrorAction Stop).Trim()
        if ($EffectiveTunnelProvider -eq "ngrok" -and $EffectiveHostname) {
            $clipboardPattern = "^" + [regex]::Escape("https://$EffectiveHostname/mcp?codexpro_token=") + "[^\s`"']+$"
        } else {
            $clipboardPattern = "^https://[^\s`"']+\.trycloudflare\.com/mcp\?codexpro_token=[^\s`"']+$"
        }
        if ($clipboardText -ne $preClipboardText -and $clipboardText -match $clipboardPattern) {
            $publicUrl = $clipboardText
        }
    } catch {
        $publicUrl = $null
    }
}

if (-not $publicUrl) {
    try {
        $publicUrl = Get-RuntimeCandidateUrl -MinLastWriteTime $processStartedAt.AddSeconds(-5)
    } catch {
        $publicUrl = $null
    }
}

if ($publicUrl -and $publicUrl -notmatch "/mcp\?codexpro_token=") {
    $publicUrl = $null
}

if (-not $publicUrl) {
    $processStopped = Stop-OwnedBootstrapProcess -OwnedProcess $process
    $payload = [ordered]@{
        status = "full-tokenized-mcp-url-not-found-yet"
        root = $ResolvedRoot
        app_name = $AppName
        port = $ResolvedPort
        process_id = if ($process) { $process.Id } else { $null }
        process_stopped = $processStopped
        out_log = $outLog
        err_log = $errLog
        next_action = "inspect logs or rerun after CodexPro prints a full /mcp?codexpro_token= URL"
    }
    Write-StateJson -Payload $payload
    exit 2
}

$identityDeadline = (Get-Date).AddSeconds([Math]::Max(20, $WaitSeconds))
$identityAttempts = 0
$identityProbe = $null
do {
    $identityAttempts++
    $identityProbe = Test-CodexProIdentity -CandidateUrl $publicUrl
    if ([bool]$identityProbe.ok) { break }
    if ((Get-Date) -ge $identityDeadline) { break }
    Start-Sleep -Seconds 2
} while ($true)
$identityResult = $identityProbe.result
if (-not [bool]$identityProbe.ok) {
    $processStopped = Stop-OwnedBootstrapProcess -OwnedProcess $process
    $payload = [ordered]@{
        status = "identity-failed-before-registry-update"
        root = $ResolvedRoot
        app_name = $AppName
        port = $ResolvedPort
        process_id = if ($process) { $process.Id } else { $null }
        process_stopped = $processStopped
        identity_attempts = $identityAttempts
        identity = $identityResult
        out_log = $outLog
        err_log = $errLog
        next_action = "do not update registry or Developer App until CodexPro MCP identity matches root and port"
    }
    Write-StateJson -Payload $payload
    exit 2
}

$updateArgs = @("--root", $ResolvedRoot, "--public-url", "$publicUrl", "--port", "$ResolvedPort", "--verified-open-port", "--update")
if ($ForceRecreate) { $updateArgs += "--force-recreate" }
$updated = Invoke-ManagerJson -ManagerArgs $updateArgs
$watchdogProcess = Start-FixedRuntimeWatchdog
$payload = [ordered]@{
    status = "ready"
    root = $ResolvedRoot
    app_name = $updated.app_name
    port = $ResolvedPort
    public_url = $updated.public_url
    action = $updated.action
    old_app_name = $updated.old_app_name
    old_public_url = $updated.old_public_url
    chrome_next_action = $updated.chrome_next_action
    transaction_id = $updated.transaction_id
    identity = $identityResult
    identity_attempts = $identityAttempts
    launch_mode = $launchMode
    requested_tunnel_provider = $RequestedTunnelProvider
    effective_tunnel_provider = $EffectiveTunnelProvider
    fixed_ngrok_registry_contract = [bool]$FixedNgrokContract.ok
    fixed_ngrok_contract_reason = [string]$FixedNgrokContract.reason
    fixed_ngrok_process_id = if ($fixedNgrokProcess) { $fixedNgrokProcess.ProcessId } else { $null }
    process_id = if ($process) { $process.Id } else { $null }
    watchdog_start_requested = [bool]$watchdogProcess
    watchdog_process_id = if ($watchdogProcess) { $watchdogProcess.Id } else { $null }
    out_log = $outLog
    err_log = $errLog
}
Write-StateJson -Payload $payload
