# 定时把仓库快照提交并推送到 git@github.com:Honey-Xin/aiquant.git
#
# 用法：
#   pwsh -File scripts/auto_push.ps1                 # 有变更才提交
#   pwsh -File scripts/auto_push.ps1 -Message "xxx"  # 自定义提交信息
#   pwsh -File scripts/auto_push.ps1 -NoPush         # 只提交不推送（演练）
#
# 行为：
#   * 仓库根 = 本脚本的上一级目录；不是 git 仓库则先 init 并设置 origin；
#   * 只提交源码/文档/配置（.gitignore 已排除 .env、日志、缓存、数据）；
#   * 推送失败重试 3 次（指数退避），优先 --rebase --autostash 拉取再推；
#   * 全过程写日志 logs/auto_push.log（logs/ 已被忽略，不会自己提交自己）。

[CmdletBinding()]
param(
    [string]$Message = "",
    [switch]$NoPush,
    [int]$Retries = 3,
    [string]$Remote = "git@github.com:Honey-Xin/aiquant.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "auto_push.log"

function Write-Log([string]$text) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"), $text
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Host $line
}

if (-not $Message) {
    $Message = "chore(auto): snapshot {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
}

if (-not (Test-Path (Join-Path $repo ".git"))) {
    Write-Log "git init（首次运行）"
    git init | Out-Null
    git symbolic-ref HEAD "refs/heads/$Branch"
    git remote add origin $Remote
}
elseif (-not (git remote get-url origin 2>$null)) {
    git remote add origin $Remote
}

$changes = git status --porcelain
if (-not $changes) {
    Write-Log "无变更，跳过"
    exit 0
}

git add -A
git -c user.name="aiquant-bot" -c user.email="aiquant-bot@users.noreply.git" commit -m $Message | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Log "提交失败（exit=$LASTEXITCODE）"
    exit $LASTEXITCODE
}
Write-Log ("已提交：{0}" -f $Message)

if ($NoPush) {
    Write-Log "-NoPush：跳过推送"
    exit 0
}

for ($i = 1; $i -le $Retries; $i++) {
    git push -u origin $Branch 2>&1 | ForEach-Object { Write-Log "  push: $_" }
    if ($LASTEXITCODE -eq 0) {
        Write-Log "推送成功（第 $i 次尝试）"
        exit 0
    }
    Write-Log "推送失败（第 $i 次），先尝试 rebase 同步远端"
    git pull --rebase --autostash origin $Branch 2>&1 | ForEach-Object { Write-Log "  pull: $_" }
    Start-Sleep -Seconds ([math]::Min(60, [math]::Pow(2, $i) * 5))
}

Write-Log "推送最终失败，请人工检查网络/SSH key"
exit 1
