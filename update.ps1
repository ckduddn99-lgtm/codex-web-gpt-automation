[CmdletBinding(SupportsShouldProcess=$true)]
param(
  [string]$AgbrowseVersion,
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
  [string]$RollbackReceipt,
  [switch]$Preflight,
  [string]$PreflightToken
)

$ErrorActionPreference = 'Stop'
$BaselineVersion = '0.1.18'
$BaselineIntegrity = 'sha512-vO2E1XrqTAvkWeSyV1xzsONz+OBB3aDKbxIGVS7Z4pH42Hxg/mlcteIAzM+EuD4hnp6Tt5IJu/X2fjMOiftBCA=='
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$CodexRoot = [IO.Path]::GetFullPath($CodexHome)
$ContractsRoot = Join-Path $CodexRoot 'contracts'
$UpdateReceipt = Join-Path $CodexRoot 'agbrowse-update-receipt.json'
$UpdateLock = Join-Path $CodexRoot 'agbrowse-update.lock'
$Nonce = [guid]::NewGuid().ToString('N')
$Stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff')
$TransactionRoot = Join-Path $CodexRoot "backups/agbrowse-update-$Stamp-$Nonce"

function Test-IsWithinRoot([string]$Root, [string]$Path) {
  $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
  $pathFull = [IO.Path]::GetFullPath($Path)
  $pathFull.StartsWith($rootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Get-Sha256([string]$Path) {
  $stream = $null
  $sha256 = $null
  try {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    (([BitConverter]::ToString($sha256.ComputeHash($stream))) -replace '-', '').ToLowerInvariant()
  } finally {
    if ($sha256) { $sha256.Dispose() }
    if ($stream) { $stream.Dispose() }
  }
}

function Get-TextSha256([string]$Value) {
  $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
  ([Security.Cryptography.SHA256]::Create().ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Get-PackageState($Npm, [string]$Root) {
  $list = & $Npm.Source list --global agbrowse --json 2>$null
  if ($LASTEXITCODE) { throw 'npm list failed while reading dependency state' }
  $info = $null; try { $info = ($list | ConvertFrom-Json).dependencies.agbrowse } catch {}
  $version = $(if ($info) { [string]$info.version } else { $null })
  $exe = Get-Command agbrowse.cmd,agbrowse -ErrorAction SilentlyContinue | Select-Object -First 1
  $integrity = $null
  if ($version) {
    $integrity = (& $Npm.Source view "agbrowse@$version" dist.integrity --json | ConvertFrom-Json)
    if ($LASTEXITCODE -or !$integrity) { throw 'npm did not return integrity for installed prior version' }
  }
  [ordered]@{ version=$version; integrity=$integrity; executable=$(if($exe){$exe.Source}else{$null}); executable_sha256=$(if($exe){Get-Sha256 $exe.Source}else{$null}); update_receipt_sha256=$(if(Test-Path -LiteralPath (Join-Path $Root 'agbrowse-update-receipt.json')){Get-Sha256 (Join-Path $Root 'agbrowse-update-receipt.json')}else{$null}) }
}

function Test-RestoredPrior($Npm, $Prior, [string]$CodexRoot) {
  if (!$Prior.version) { return $true }
  $registryIntegrity = (& $Npm.Source view "agbrowse@$($Prior.version)" dist.integrity --json | ConvertFrom-Json)
  if ($LASTEXITCODE -or !$registryIntegrity -or $registryIntegrity -ne [string]$Prior.integrity) { return $false }
  $actual = Get-PackageState $Npm $CodexRoot
  $actual.version -eq [string]$Prior.version -and $actual.integrity -eq [string]$Prior.integrity -and $actual.executable_sha256 -eq [string]$Prior.executable_sha256
}

function Get-RunPhase([string]$RunFile) {
  try {
    $value = Get-Content -LiteralPath $RunFile -Raw -Encoding UTF8 | ConvertFrom-Json
    return [string]$value.phase
  } catch {
    return 'UNREADABLE_STATE'
  }
}

function Get-ActiveOrUncertainRuns([string]$Root) {
  $activePhases = @(
    'CREATED','PREFLIGHTED','LEASED','SEND_STARTED','SUBMITTED','URL_BOUND',
    'RESPONSE_IN_PROGRESS','RECOVERY_REQUIRED','RECOVERING',
    'SUBMISSION_UNCERTAIN_IDENTITY_MISSING','BLOCKED_RECOVERY_EXHAUSTED',
    'USER_STOP_REQUESTED'
  )
  $hits = @()
  $projects = Join-Path $Root 'state/chatgpt-agbrowse/projects'
  if (!(Test-Path -LiteralPath $projects)) { return @() }

  foreach ($runFile in @(Get-ChildItem -LiteralPath $projects -Filter 'run.json' -File -Recurse -Force -ErrorAction SilentlyContinue | Sort-Object FullName)) {
    $phase = Get-RunPhase $runFile.FullName
    if ($activePhases -contains $phase -or $phase -eq 'UNREADABLE_STATE') {
      $hits += [ordered]@{ kind='run'; path=$runFile.FullName; phase=$phase }
    }
  }

  foreach ($lockFile in @(Get-ChildItem -LiteralPath $projects -Filter 'active.lock' -File -Recurse -Force -ErrorAction SilentlyContinue | Sort-Object FullName)) {
    try {
      $lockValue = Get-Content -LiteralPath $lockFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
      $runId = [string]$lockValue.run_id
      $runFile = Join-Path (Split-Path -Parent $lockFile.FullName) "runs/$runId/run.json"
      if (!$runId -or !(Test-Path -LiteralPath $runFile)) {
        $hits += [ordered]@{ kind='orphan-active-lock'; path=$lockFile.FullName; phase='ADJUDICATION_REQUIRED' }
        continue
      }
      $phase = Get-RunPhase $runFile
      if ($activePhases -contains $phase -or $phase -eq 'UNREADABLE_STATE') {
        $hits += [ordered]@{ kind='active-lock'; path=$lockFile.FullName; run=$runFile; phase=$phase }
      }
    } catch {
      $hits += [ordered]@{ kind='unreadable-active-lock'; path=$lockFile.FullName; phase='ADJUDICATION_REQUIRED' }
    }
  }
  @($hits)
}

if ($RollbackReceipt -and $AgbrowseVersion) { throw 'AgbrowseVersion and RollbackReceipt are mutually exclusive' }
if (!$RollbackReceipt -and $AgbrowseVersion -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
  throw 'AgbrowseVersion must be an explicit resolved semver, not a floating tag.'
}

if ($RollbackReceipt) {
  $fullRollbackReceipt = [IO.Path]::GetFullPath($RollbackReceipt)
  $isOwnedBackupReceipt = Test-IsWithinRoot (Join-Path $CodexRoot 'backups') $fullRollbackReceipt
  $isCurrentUpdateReceipt = $fullRollbackReceipt -eq [IO.Path]::GetFullPath($UpdateReceipt)
  if ((!$isOwnedBackupReceipt -and !$isCurrentUpdateReceipt) -or !(Test-Path -LiteralPath $fullRollbackReceipt)) {
    throw 'rollback receipt must be owned by this CODEX_HOME backup root'
  }
  $rollbackValue = Get-Content -LiteralPath $fullRollbackReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($rollbackValue.schema -ne 'codexpro.agbrowse-update-receipt/v2' -or !$rollbackValue.prior) { throw 'unsupported dependency rollback receipt' }
  $selectedVersion = [string]$rollbackValue.selected_version
  $prior = $rollbackValue.prior
  $transactionRoot = [string]$rollbackValue.transaction_root
  if (!$selectedVersion -or !(Test-IsWithinRoot (Join-Path $CodexRoot 'backups') $transactionRoot)) { throw 'dependency rollback transaction is not owned by this CODEX_HOME' }
  $targetContract = [string]$rollbackValue.contract
  if (!(Test-IsWithinRoot $ContractsRoot $targetContract)) { throw 'dependency rollback contract is not owned by this CODEX_HOME' }
  $targetBackup = Join-Path $transactionRoot 'target-contract.json'
  $receiptBackup = Join-Path $transactionRoot 'agbrowse-update-receipt.json'
  $npm = Get-Command npm.cmd,npm -ErrorAction SilentlyContinue | Select-Object -First 1
  if (!$npm) { throw 'npm is required for dependency rollback' }
  $currentList = & $npm.Source list --global agbrowse --json 2>$null
  $currentInfo = $null; try { $currentInfo = ($currentList | ConvertFrom-Json).dependencies.agbrowse } catch {}
  $currentVersion = $(if ($currentInfo) { [string]$currentInfo.version } else { $null })
  $conflicts = @()
  if ($currentVersion -ne $selectedVersion) { $conflicts += @{kind='npm_version'; expected=$selectedVersion; actual=$currentVersion} }
  if (!(Test-Path -LiteralPath $targetContract) -or (Get-Sha256 $targetContract) -ne [string]$rollbackValue.contract_sha256) { $conflicts += @{kind='contract'; path=$targetContract; expected=$rollbackValue.contract_sha256} }
  $currentUpdateReceipt = $null; try { $currentUpdateReceipt = Get-Content -LiteralPath $UpdateReceipt -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
  if (!$currentUpdateReceipt -or [string]$currentUpdateReceipt.selected_version -ne $selectedVersion -or [string]$currentUpdateReceipt.contract_sha256 -ne [string]$rollbackValue.contract_sha256 -or [string]$currentUpdateReceipt.transaction_root -ne $transactionRoot) { $conflicts += @{kind='update_receipt'; path=$UpdateReceipt; expected_selected_version=$selectedVersion} }
  if ($prior.target_contract_existed -and (!(Test-Path -LiteralPath $targetBackup) -or (Get-Sha256 $targetBackup) -ne [string]$prior.target_contract_backup_sha256)) { $conflicts += @{kind='target_contract_backup'; path=$targetBackup} }
  if ($prior.update_receipt_existed -and (!(Test-Path -LiteralPath $receiptBackup) -or (Get-Sha256 $receiptBackup) -ne [string]$prior.update_receipt_backup_sha256)) { $conflicts += @{kind='update_receipt_backup'; path=$receiptBackup} }
  if ($conflicts.Count) { [ordered]@{schema='codexpro.agbrowse-rollback-result/v1';status='CONFLICT';conflicts=$conflicts}|ConvertTo-Json -Depth 6; exit 2 }
  if ($WhatIfPreference) { 'Would restore the exact prior agbrowse package, target contract bytes, and update receipt bytes.'; exit 0 }
  try {
    if ($prior.version) { & $npm.Source install --global "agbrowse@$($prior.version)" } else { & $npm.Source uninstall --global agbrowse }
    if ($LASTEXITCODE) { throw 'npm dependency inverse failed' }
    if ($prior.version -and !(Test-RestoredPrior $npm $prior $CodexRoot)) { throw 'npm dependency inverse did not restore recorded integrity, version, and executable hash' }
    if ($prior.target_contract_existed) { Copy-Item -LiteralPath $targetBackup -Destination $targetContract -Force } else { Remove-Item -LiteralPath $targetContract -Force }
    if ($prior.update_receipt_existed) { Copy-Item -LiteralPath $receiptBackup -Destination $UpdateReceipt -Force } else { Remove-Item -LiteralPath $UpdateReceipt -Force }
    [ordered]@{schema='codexpro.agbrowse-rollback-result/v1';status='COMPLETE';receipt=$fullRollbackReceipt}|ConvertTo-Json
  } catch {
    [ordered]@{schema='codexpro.agbrowse-rollback-result/v1';status='PARTIAL';error=$_.Exception.Message;receipt=$fullRollbackReceipt}|ConvertTo-Json
    exit 3
  }
  exit 0
}

$active = @(Get-ActiveOrUncertainRuns $CodexRoot)
if ($active.Count) {
  [ordered]@{
    code = 'DEFER_ACTIVE_WORK'
    reason = 'active, uncertain, or unadjudicated run state is protected'
    evidence = $active
  } | ConvertTo-Json -Depth 6
  exit 2
}

if ($Preflight -and $PreflightToken) { throw 'Preflight and PreflightToken are mutually exclusive' }
$npm = Get-Command npm.cmd,npm -ErrorAction SilentlyContinue | Select-Object -First 1
$python = Get-Command python.cmd,python.exe,python -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$npm -or !$python) { throw 'npm and Python are required for an explicit agbrowse update' }

$selectedIntegrity = (& $npm.Source view "agbrowse@$AgbrowseVersion" dist.integrity --json | ConvertFrom-Json)
if ($LASTEXITCODE -or !$selectedIntegrity) { throw 'npm did not return integrity for the selected version' }
if ($AgbrowseVersion -eq $BaselineVersion -and $selectedIntegrity -ne $BaselineIntegrity) {
  throw 'selected npm integrity does not match the reviewed 0.1.18 baseline'
}
$preflightInputs = [ordered]@{
  version = $AgbrowseVersion
  integrity = $selectedIntegrity
  prior = Get-PackageState $npm $CodexRoot
  target_contract_sha256 = $(if(Test-Path -LiteralPath (Join-Path $ContractsRoot "agbrowse-$AgbrowseVersion.json")){Get-Sha256 (Join-Path $ContractsRoot "agbrowse-$AgbrowseVersion.json")}else{$null})
  update_lock_absent = !(Test-Path -LiteralPath $UpdateLock)
}
if (!$preflightInputs.update_lock_absent) {
  [ordered]@{code='DEFER_ACTIVE_WORK';lock=$UpdateLock;reason='another explicit update is in progress'} | ConvertTo-Json
  exit 2
}
if ($Preflight) {
  # This branch intentionally reads only: it neither creates CODEX_HOME nor reserves the lock.
  $token = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($preflightInputs | ConvertTo-Json -Compress -Depth 8)))
  [ordered]@{schema='codexpro.agbrowse-update-preflight/v1';status='READY';token=$token;selected_version=$AgbrowseVersion;integrity=$selectedIntegrity}|ConvertTo-Json -Compress
  exit 0
}
if ($PreflightToken) {
  try { $tokenInputs = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PreflightToken)) | ConvertFrom-Json } catch { throw 'invalid dependency preflight token' }
  $expectedToken = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($preflightInputs | ConvertTo-Json -Compress -Depth 8)))
  if ($PreflightToken -ne $expectedToken) { throw 'dependency preflight token no longer matches selected registry or prior state' }
}

if ($WhatIfPreference) {
  "Would resolve npm integrity for agbrowse@$AgbrowseVersion, record immutable prior npm/contract state, install that exact version, capture and validate its public-command contract, and roll back both npm and contract bytes on failure."
  exit 0
}

New-Item -ItemType Directory -Force -Path $CodexRoot,$ContractsRoot,$TransactionRoot | Out-Null
try {
  $stream = [IO.File]::Open($UpdateLock, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  $stream.Dispose()
} catch [IO.IOException] {
  [ordered]@{code='DEFER_ACTIVE_WORK';lock=$UpdateLock;reason='another explicit update is in progress'} | ConvertTo-Json
  exit 2
}

$prior = $null
$targetContract = Join-Path $ContractsRoot "agbrowse-$AgbrowseVersion.json"
$targetContractBackup = Join-Path $TransactionRoot 'target-contract.json'
$receiptBackup = Join-Path $TransactionRoot 'agbrowse-update-receipt.json'
$targetContractExisted = Test-Path -LiteralPath $targetContract
$receiptExisted = Test-Path -LiteralPath $UpdateReceipt
$stagedContract = Join-Path $ContractsRoot ".agbrowse-$AgbrowseVersion-$Nonce.tmp.json"

try {
  $priorList = & $npm.Source list --global agbrowse --json 2>$null
  $priorInfo = $null
  try { $priorInfo = ($priorList | ConvertFrom-Json).dependencies.agbrowse } catch {}
  $priorVersion = $(if ($priorInfo) { [string]$priorInfo.version } else { $null })
  $priorExe = Get-Command agbrowse.cmd,agbrowse -ErrorAction SilentlyContinue | Select-Object -First 1
  $priorContract = $(if ($priorVersion) { Join-Path $ContractsRoot "agbrowse-$priorVersion.json" } else { $null })
  $priorIntegrity = $null
  if ($priorContract -and (Test-Path -LiteralPath $priorContract)) {
    try { $priorIntegrity = (Get-Content -LiteralPath $priorContract -Raw -Encoding UTF8 | ConvertFrom-Json).agbrowse.npmIntegrity } catch {}
  }
  if (!$priorIntegrity -and $priorVersion) {
    $priorIntegrity = (& $npm.Source view "agbrowse@$priorVersion" dist.integrity --json | ConvertFrom-Json)
  }
  if ($targetContractExisted) { Copy-Item -LiteralPath $targetContract -Destination $targetContractBackup -Force }
  if ($receiptExisted) { Copy-Item -LiteralPath $UpdateReceipt -Destination $receiptBackup -Force }
  $prior = [ordered]@{
    version = $priorVersion
    integrity = $priorIntegrity
    executable = $(if ($priorExe) { $priorExe.Source } else { $null })
    executable_sha256 = $(if ($priorExe) { Get-Sha256 $priorExe.Source } else { $null })
    contract = $priorContract
    contract_sha256 = $(if ($priorContract -and (Test-Path -LiteralPath $priorContract)) { Get-Sha256 $priorContract } else { $null })
    target_contract_existed = $targetContractExisted
    target_contract_backup_sha256 = $(if ($targetContractExisted) { Get-Sha256 $targetContractBackup } else { $null })
    update_receipt_existed = $receiptExisted
    update_receipt_backup_sha256 = $(if ($receiptExisted) { Get-Sha256 $receiptBackup } else { $null })
  }

  if (!$PSCmdlet.ShouldProcess("agbrowse@$AgbrowseVersion", 'install exact agent-selected runtime')) { return }

  & $npm.Source install --global "agbrowse@$AgbrowseVersion"
  if ($LASTEXITCODE) { throw 'npm install failed' }

  & $python.Source (Join-Path $RepoRoot 'bin/chatgpt_agbrowse_contract.py') capture `
    --expected-version $AgbrowseVersion --expected-integrity $selectedIntegrity --output $stagedContract
  if ($LASTEXITCODE) { throw 'public command contract capture failed' }
  & $python.Source (Join-Path $RepoRoot 'bin/chatgpt_agbrowse_contract.py') validate `
    --manifest $stagedContract --expected-version $AgbrowseVersion --expected-integrity $selectedIntegrity
  if ($LASTEXITCODE) { throw 'public command contract validation failed' }

  Move-Item -LiteralPath $stagedContract -Destination $targetContract -Force
  $newExe = Get-Command agbrowse.cmd,agbrowse -ErrorAction Stop | Select-Object -First 1
  [ordered]@{
    schema = 'codexpro.agbrowse-update-receipt/v2'
    updated_at = [DateTime]::UtcNow.ToString('o')
    selected_version = $AgbrowseVersion
    integrity = $selectedIntegrity
    executable = $newExe.Source
    executable_sha256 = Get-Sha256 $newExe.Source
    contract = $targetContract
    contract_sha256 = Get-Sha256 $targetContract
    prior = $prior
    transaction_root = $TransactionRoot
    activation = 'agent-must-explicitly-select-this-contract; no background promotion pointer exists'
  } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $UpdateReceipt -Encoding utf8
} catch {
  $originalError = $_
  $inverseComplete = $false
  if ($prior -and $prior.version) {
    & $npm.Source install --global "agbrowse@$($prior.version)"
    if (!$LASTEXITCODE -and (Test-RestoredPrior $npm $prior $CodexRoot)) { $inverseComplete = $true }
  } elseif ($prior) {
    & $npm.Source uninstall --global agbrowse
    if (!$LASTEXITCODE) {
      $restored = Get-PackageState $npm $CodexRoot
      $inverseComplete = !$restored.version
    }
  }
  if (!$inverseComplete) { throw "update rollback incomplete; npm prior package identity was not restored: $($originalError.Exception.Message)" }
  if ($targetContractExisted -and (Test-Path -LiteralPath $targetContractBackup)) {
    Copy-Item -LiteralPath $targetContractBackup -Destination $targetContract -Force
  } elseif (Test-Path -LiteralPath $targetContract) {
    Remove-Item -LiteralPath $targetContract -Force
  }
  if ($receiptExisted -and (Test-Path -LiteralPath $receiptBackup)) {
    Copy-Item -LiteralPath $receiptBackup -Destination $UpdateReceipt -Force
  } elseif (Test-Path -LiteralPath $UpdateReceipt) {
    Remove-Item -LiteralPath $UpdateReceipt -Force
  }
  throw $originalError
} finally {
  Remove-Item -LiteralPath $stagedContract -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $UpdateLock -Force -ErrorAction SilentlyContinue
}
