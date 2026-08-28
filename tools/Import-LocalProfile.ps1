[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('dev-fixture', 'functional-uat')]
    [string]$Profile,

    [Parameter(Mandatory)]
    [ValidateSet('backend', 'agent')]
    [string]$Scope
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($MyInvocation.InvocationName -ne '.') {
    throw 'Dot-source this script so the validated local values remain in the current process.'
}

# A normal checkout lives directly below the shared workspace.  A task
# worktree lives below <workspace>\_worktrees instead, but must still use the
# one shared ignored profile.  Only accept that alternate layout when the
# canonical Backend checkout is present; otherwise retain the normal path and
# fail through the existing missing-profile boundary.
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspace = (Resolve-Path (Join-Path $repositoryRoot '..')).Path
if ((Split-Path -Leaf $workspace) -ceq '_worktrees') {
    $sharedWorkspace = (Resolve-Path (Join-Path $workspace '..')).Path
    $canonicalLoader = Join-Path $sharedWorkspace 'customs-backend\tools\Import-LocalProfile.ps1'
    if (Test-Path -LiteralPath $canonicalLoader -PathType Leaf) {
        $workspace = $sharedWorkspace
    }
}
$profileFile = Join-Path $workspace "_local\profiles\$Profile\backend.env"
if (-not (Test-Path -LiteralPath $profileFile -PathType Leaf)) {
    throw "Local profile is missing: _local/profiles/$Profile/backend.env"
}

$backendKeys = [System.Collections.Generic.HashSet[string]]::new([string[]]@(
    'CUSTOMS_BACKEND_ENVIRONMENT',
    'CUSTOMS_BACKEND_PUBLIC_BASE_URL',
    'CUSTOMS_BACKEND_AGENT_BASE_URL',
    'CUSTOMS_BACKEND_AGENT_SERVICE_TOKEN',
    'CUSTOMS_BACKEND_DEV_REVIEWER_SESSION_TOKEN',
    'CUSTOMS_BACKEND_DEV_APPROVER_SESSION_TOKEN',
    'CUSTOMS_BACKEND_DATABASE_URL',
    'CUSTOMS_BACKEND_MINIO_ENDPOINT',
    'CUSTOMS_BACKEND_MINIO_ACCESS_KEY',
    'CUSTOMS_BACKEND_MINIO_SECRET_KEY',
    'CUSTOMS_BACKEND_OIDC_ISSUER',
    'CUSTOMS_BACKEND_OIDC_AUDIENCE',
    'CUSTOMS_BACKEND_OIDC_JWKS_URL',
    'CUSTOMS_BACKEND_OIDC_CLIENT_ID',
    'CUSTOMS_BACKEND_OIDC_AUTHORIZATION_ENDPOINT',
    'CUSTOMS_BACKEND_OIDC_TOKEN_ENDPOINT',
    'CUSTOMS_BACKEND_OIDC_END_SESSION_ENDPOINT'
))
$agentKeys = [System.Collections.Generic.HashSet[string]]::new([string[]]@(
    'CUSTOMS_AGENT_SERVICE_BEARER_TOKEN',
    'CUSTOMS_AGENT_SERVICE_MODE'
))
$composeKeys = [System.Collections.Generic.HashSet[string]]::new([string[]]@(
    'CUSTOMS_FUNCTIONAL_POSTGRES_PASSWORD',
    'CUSTOMS_FUNCTIONAL_MINIO_ROOT_USER',
    'CUSTOMS_FUNCTIONAL_MINIO_ROOT_PASSWORD',
    'CUSTOMS_FUNCTIONAL_KEYCLOAK_ADMIN_PASSWORD',
    'CUSTOMS_FUNCTIONAL_REVIEWER_USERNAME',
    'CUSTOMS_FUNCTIONAL_REVIEWER_PASSWORD',
    'CUSTOMS_FUNCTIONAL_APPROVER_USERNAME',
    'CUSTOMS_FUNCTIONAL_APPROVER_PASSWORD',
    'CUSTOMS_FUNCTIONAL_REALM_FILE'
))
$allowedKeys = if ($Scope -eq 'backend') { $backendKeys } else { $agentKeys }

$imported = 0
foreach ($rawLine in Get-Content -LiteralPath $profileFile -Encoding utf8) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith('#')) {
        continue
    }
    if ($line -notmatch '^(?<key>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$') {
        throw 'Local profile contains an invalid KEY=VALUE line.'
    }
    $key = $Matches['key']
    $value = $Matches['value']
    if (-not ($backendKeys.Contains($key) -or $agentKeys.Contains($key) -or $composeKeys.Contains($key))) {
        throw 'Local profile contains a key outside the Customs local boundary.'
    }
    if ($allowedKeys.Contains($key)) {
        Set-Item -LiteralPath "Env:$key" -Value $value
        $imported++
    }
}

if ($imported -eq 0) {
    throw 'Local profile contains no usable settings.'
}

# Existing ignored profiles may have been created before the Agent loopback
# port moved out of the Windows-reserved 80xx range.  Preserve their tokens
# and all other values, but use the current local topology at process start.
if (
    $Scope -eq 'backend' -and
    $env:CUSTOMS_BACKEND_AGENT_BASE_URL -ceq 'http://127.0.0.1:8090'
) {
    $env:CUSTOMS_BACKEND_AGENT_BASE_URL = 'http://127.0.0.1:18090'
}

# Existing ignored profiles may likewise contain the prior Backend default in
# the Windows-reserved 80xx range.  This narrow compatibility mapping keeps
# locally generated secrets intact without accepting a non-loopback URL.
if (
    $Scope -eq 'backend' -and
    $env:CUSTOMS_BACKEND_PUBLIC_BASE_URL -ceq 'http://127.0.0.1:8080'
) {
    $env:CUSTOMS_BACKEND_PUBLIC_BASE_URL = 'http://127.0.0.1:18081'
}

Write-Host "Imported local $Profile $Scope profile values."
