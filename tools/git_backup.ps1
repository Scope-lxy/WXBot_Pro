param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MessageParts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path ".git")) {
    Write-Error "当前目录还不是 Git 仓库：$repoRoot"
}

$message = ($MessageParts -join " ").Trim()
if ([string]::IsNullOrWhiteSpace($message)) {
    $message = "backup: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}

git add -A

$statusLines = git status --short
if (-not $statusLines) {
    Write-Host "没有需要备份的新改动。"
    exit 0
}

git commit -m $message
git status --short --branch

