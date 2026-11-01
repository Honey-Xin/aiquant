# 定时把仓库快照提交并推送到 git@github.com:Honey-Xin/aiquant.git
#
# 用法（Windows PowerShell 5.1 与 PowerShell 7 均可）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\auto_push.ps1
#   ... -Message "自定义提交信息"     # 自定义提交信息
#   ... -NoPush                      # 只提交不推送（演练）
#
# 行为：
#   * 仓库根 = 本脚本的上一级目录；不是 git 仓库则先 init 并设置 origin；
#   * 只提交源码/文档/配置（.gitignore 已排除 .env、日志、缓存、演示数据）；
#   * 推送失败重试 3 次（指数退避），每次先 --rebase --autostash 同步远端；
#   * 全过程写日志 logs/auto_push.log（logs/ 已被忽略，不会自己提交自己）。

[CmdletBinding()]
param(
    [string]$Message = "",
    [switch]$NoPush,
    [int]$Retries = 3,
    [string]$Remote = "git@github.com:Honey-Xin/aiquant.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Continue"
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

# 执行 git 并同时拿到 stdout/stderr 与退出码。
# 经 cmd.exe 调用可避免 PS 5.1 把 git 的 stderr 包装成 NativeCommandError 噪声。
function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $quoted = ($GitArgs | ForEach-Object {
            if ($_ -match '[\s"]') { '"{0}"' -f ($_ -replace '"', '\"') } else { $_ }
        }) -join ' '
    $out = (& cmd.exe /c ("git " + $quoted + " 2>&1") | Out-String)
    $code = $LASTEXITCODE
    return [pscustomobject]@{ Code = $code; Output = $out.Trim() }
}

if (-not $Message) {
    $Message = "chore(auto): snapshot {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
}

$state = Invoke-Git rev-parse --is-inside-work-tree
if ($state.Code -ne 0 -or $state.Output -ne "true") {
    Write-Log "git init（首次运行）"
    Invoke-Git init | Out-Null
    Invoke-Git symbolic-ref HEAD "refs/heads/$Branch" | Out-Null
    Invoke-Git remote add origin $Remote | Out-Null
}
else {
    $url = Invoke-Git remote get-url origin
    if ($url.Code -ne 0) {
        Invoke-Git remote add origin $Remote | Out-Null
    }
}

$status = Invoke-Git status --porcelain
if (-not $status.Output) {
    Write-Log "无变更，跳过"
    exit 0
}

Invoke-Git add -A | Out-Null
$commit = Invoke-Git -c user.name=aiquant-bot -c user.email=aiquant-bot@users.noreply.git commit -m $Message
if ($commit.Code -ne 0) {
    Write-Log ("提交失败（exit={0}）：{1}" -f $commit.Code, $commit.Output)
    exit $commit.Code
}
Write-Log ("已提交：{0}" -f $Message)

if ($NoPush) {
    Write-Log "-NoPush：跳过推送"
    exit 0
}

for ($i = 1; $i -le $Retries; $i++) {
    $push = Invoke-Git push -u origin $Branch
    foreach ($line in ($push.Output -split "`r?`n")) {
        if ($line.Trim()) { Write-Log ("  push: {0}" -f $line.Trim()) }
    }
    if ($push.Code -eq 0) {
        Write-Log ("推送成功（第 {0} 次尝试）" -f $i)
        exit 0
    }
    Write-Log ("推送失败（第 {0} 次，exit={1}），尝试 rebase 同步远端" -f $i, $push.Code)
    $pull = Invoke-Git pull --rebase --autostash origin $Branch
    foreach ($line in ($pull.Output -split "`r?`n")) {
        if ($line.Trim()) { Write-Log ("  pull: {0}" -f $line.Trim()) }
    }
    Start-Sleep -Seconds ([math]::Min(60, [math]::Pow(2, $i) * 5))
}

Write-Log "推送最终失败，请人工检查网络/SSH key"
exit 1
