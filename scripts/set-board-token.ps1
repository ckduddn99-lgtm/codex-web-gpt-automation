# Stores a credential for the meeting board.
#
# The value is read from a masked prompt, never from a command-line argument,
# so it does not land in the shell transcript or in PowerShell's history file
# (ConsoleHost_history.txt). Paste it at the prompt; nothing is echoed.
#
#   scripts\set-board-token.ps1                     # seat bot (Discord)
#   scripts\set-board-token.ps1 -Conductor          # conductor bot (Discord)
#   scripts\set-board-token.ps1 -Provider xai       # Grok API key
#   scripts\set-board-token.ps1 -Provider deepseek  # DeepSeek API key
#
# Every credential gets its own file and its own variable name on purpose. The
# board runs two Discord bots so that a seat cannot hold the token that writes
# instructions, and that separation is only as good as the storage: one shared
# file or variable and a seat quietly acts as the conductor.

param(
    [switch]$Conductor,
    [ValidateSet('xai', 'deepseek')]
    [string]$Provider,
    # Read the value straight from the clipboard instead of a console prompt.
    # Right-click paste into a masked prompt mangled a 121-character API key
    # once, splicing two characters of the current path into the middle of it,
    # and the damage is not recoverable by inspection afterwards.
    [switch]$FromClipboard
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

if ($Provider -and $Conductor) {
    throw 'Pass either -Conductor or -Provider, not both.'
}

if ($Provider) {
    $spec = @{
        xai      = @{ File = '.board-grok.env';     Key = 'XAI_API_KEY';      Role = 'Grok (xAI) API key';  Prefix = 'xai-' }
        deepseek = @{ File = '.board-deepseek.env'; Key = 'DEEPSEEK_API_KEY'; Role = 'DeepSeek API key';    Prefix = 'sk-' }
    }[$Provider]
    $fileName = $spec.File
    $keyName = $spec.Key
    $role = $spec.Role
    # API keys are opaque strings; only Discord tokens have the three-part shape.
    $expectDiscordShape = $false
    # The console shows a key's full value once and its id forever after, and the
    # id is the easier thing to copy. Checking the prefix turns that mistake into
    # a refusal here instead of an HTTP 400 an hour later.
    $expectedPrefix = $spec.Prefix
} else {
    $fileName = if ($Conductor) { '.board-conductor.env' } else { '.board.env' }
    $keyName = 'DISCORD_BOT_TOKEN'
    $role = if ($Conductor) { 'CONDUCTOR bot (writes #script)' } else { 'SEAT bot (reads #script only)' }
    $expectDiscordShape = $true
    $expectedPrefix = ''
}

$envPath = Join-Path $repoRoot $fileName

Write-Host ''
Write-Host "Credential for the $role"
if ($FromClipboard) {
    Write-Host 'Reading the value from the clipboard.'
} else {
    Write-Host 'Input is hidden. If pasting into this prompt goes wrong, re-run with -FromClipboard.'
}
Write-Host ''

if ($FromClipboard) {
    $value = Get-Clipboard -Raw
    if ($null -eq $value) { throw 'The clipboard is empty. Copy the value first, then re-run.' }
} else {
    $secure = Read-Host -Prompt 'Value' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$value = $value.Trim()
if ([string]::IsNullOrWhiteSpace($value)) {
    throw 'Nothing entered; nothing was written.'
}

# Catching a mangled paste here beats debugging a 401 later, but never print
# the value itself. Non-ASCII is checked first because that is what a broken
# console paste actually produced: Korean characters from the shell's own path
# spliced into the middle of the key, which then died as a latin-1 encoding
# error deep inside the HTTP client rather than as anything readable.
$nonAscii = [regex]::Matches($value, '[^ -~]')
if ($nonAscii.Count -gt 0) {
    throw "The value contains $($nonAscii.Count) non-ASCII character(s), so the paste was mangled. Copy it again and re-run with -FromClipboard. Nothing was written."
}
if ($expectDiscordShape) {
    $parts = $value.Split('.')
    if ($parts.Count -ne 3) {
        throw "That does not look like a bot token ($($parts.Count) dot-separated parts, expected 3). Nothing was written."
    }
} else {
    if ($expectedPrefix -and -not $value.StartsWith($expectedPrefix)) {
        throw "That does not start with '$expectedPrefix', so it is not the API key -- most likely the key's id from the console list. The full key is shown only once, right after Create API key. Nothing was written."
    }
    if ($value.Length -lt 20) {
        throw "That looks too short for an API key ($($value.Length) characters). Nothing was written."
    }
}

# UTF8 without BOM: the reader treats the file as plain KEY=VALUE lines, and a
# BOM would end up inside the first key name.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($envPath, "$keyName=$value`n", $utf8NoBom)

Write-Host ''
Write-Host "Wrote $envPath ($($value.Length) characters, key $keyName)."
Write-Host 'Covered by .gitignore (*.env). Verify with: git check-ignore -v ' $fileName
Write-Host ''
