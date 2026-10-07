# Collect every closed UTC day of ticks not yet stored, for both bound instruments.
# Run Monday-Friday after 00:30 UTC. Reads .env for this process only: the
# application itself never loads .env (see RuntimeSettings).
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}
$env:UV_SYSTEM_CERTS = "1"
$failed = 0
foreach ($instrument in @("fx.eurusd", "metal.xauusd")) {
    uv run trading-house data ticks update --instrument $instrument
    if ($LASTEXITCODE -ne 0) { $failed = $LASTEXITCODE }
}
exit $failed
