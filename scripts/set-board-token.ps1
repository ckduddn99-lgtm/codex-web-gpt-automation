# Stores a Discord bot token for the meeting board.
#
# The token is read from a masked prompt, never from a command-line argument,
# so it does not land in the shell transcript or in PowerShell's history file
# (ConsoleHost_history.txt). Paste it at the prompt; nothing is echoed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\set-board-token.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\set-board-token.ps1 -Conductor
#
# The board runs two bots on purpose. Seats and the conductor must hold
# different credentials, or the #script write restriction is decoration: a seat
# holding the writing token can post its own instructions, and a participant
# that can also issue orders breaks the one property the board enforces.

param(
    [switch]$Conductor
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$fileName = if ($Conductor) { '.board-conductor.env' } else { '.board.env' }
$envPath = Join-Path $repoRoot $fileName
$role = if ($Conductor) { 'CONDUCTOR bot (writes #script)' } else { 'SEAT bot (reads #script only)' }

Write-Host ''
Write-Host "Token for the $role"
Write-Host 'Developer Portal -> Bot -> Reset Token'
Write-Host 'Input is hidden. Right-click pastes in most terminals.'
Write-Host ''

$secure = Read-Host -Prompt 'Token' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$token = $token.Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'No token entered; nothing was written.'
}

# A bot token is three dot-separated parts. Catching a mangled paste here beats
# debugging a 401 later, but never print the value itself.
$parts = $token.Split('.')
if ($parts.Count -ne 3) {
    throw "That does not look like a bot token ($($parts.Count) dot-separated parts, expected 3). Nothing was written."
}

# UTF8 without BOM: the reader treats the file as plain KEY=VALUE lines, and a
# BOM would end up inside the first key name.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($envPath, "DISCORD_BOT_TOKEN=$token`n", $utf8NoBom)

Write-Host ''
Write-Host "Wrote $envPath ($($token.Length) characters)."
Write-Host 'Covered by .gitignore (*.env). Verify with: git check-ignore -v .board.env'
Write-Host ''
