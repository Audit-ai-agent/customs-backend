$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "../..")
Push-Location $repo
try {
    uv run --no-sync customs-agent-demo
} finally {
    Pop-Location
}
