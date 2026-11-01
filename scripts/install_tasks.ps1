# 注册（或刷新）本项目的两个计划任务：
#   1) aiquant-autopush     每 10 分钟：提交并推送代码快照到 git@github.com:Honey-Xin/aiquant.git
#   2) aiquant-audit-purge  每天 03:10：清理超过保留期的审计分片
#
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#       -AutopushMinutes 10  -PurgeAt 03:10  -Remove
# 任务以"仅在用户登录时运行"注册，无需管理员权限。

[CmdletBinding()]
param(
    [int]$AutopushMinutes = 10,
    [string]$PurgeAt = "03:10",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$pushScript = Join-Path $repo "scripts\auto_push.ps1"
$purgeScript = Join-Path $repo "scripts\purge_audit.ps1"

$pushTask = "aiquant-autopush"
$purgeTask = "aiquant-audit-purge"

if ($Remove) {
    schtasks /Delete /TN $pushTask /F 2>$null | Out-Null
    schtasks /Delete /TN $purgeTask /F 2>$null | Out-Null
    Write-Host "Removed scheduled tasks: $pushTask, $purgeTask"
    exit 0
}

$pushCmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $pushScript + '"'
schtasks /Create /SC MINUTE /MO $AutopushMinutes /TN $pushTask /TR $pushCmd /F | Out-Null
Write-Host "Registered $pushTask every $AutopushMinutes min -> auto_push.ps1"

$purgeCmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $purgeScript + '"'
schtasks /Create /SC DAILY /ST $PurgeAt /TN $purgeTask /TR $purgeCmd /F | Out-Null
Write-Host "Registered $purgeTask daily at $PurgeAt -> purge_audit.ps1"

schtasks /Query /TN $pushTask /FO LIST | Select-String -Pattern "TaskName|Next Run Time|Status"
schtasks /Query /TN $purgeTask /FO LIST | Select-String -Pattern "TaskName|Next Run Time|Status"
