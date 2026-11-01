# 审计保留期清理包装：供计划任务 aiquant-audit-purge 调用。
# 等价于：Set-Location <repo>; $env:PYTHONPATH="src"; python -m aiquant.audit.cli purge

[CmdletBinding()]
param([int]$RetentionDays = 0)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PYTHONPATH = "src"

$args = @("-m", "aiquant.audit.cli", "purge")
if ($RetentionDays -gt 0) { $args += @("--retention-days", "$RetentionDays") }

& python @args
exit $LASTEXITCODE
