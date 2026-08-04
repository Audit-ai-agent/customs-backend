[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$profileFile = Join-Path $workspace '_local\profiles\functional-uat\backend.env'
if (-not (Test-Path -LiteralPath $profileFile -PathType Leaf)) {
    throw 'Local functional UAT profile is missing: _local/profiles/functional-uat/backend.env'
}

$passwords = [System.Collections.Generic.List[string]]::new()
foreach ($line in Get-Content -LiteralPath $profileFile -Encoding utf8) {
    if ($line -match '^CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD=(?<value>.+)$') {
        $passwords.Add($Matches['value'])
    }
}

if ($passwords.Count -ne 1) {
    throw 'Local functional UAT reviewer password is missing or duplicated.'
}

if (-not (Get-Command Set-Clipboard -ErrorAction SilentlyContinue)) {
    throw 'The current-user clipboard is unavailable for local functional UAT login.'
}

# The opt-in command deliberately prints nothing. It reads only the reviewer
# password and puts it in this Windows user's clipboard for the loopback form.
Set-Clipboard -Value $passwords[0]
