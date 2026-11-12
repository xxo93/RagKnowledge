# 本地离线回归一键脚本（对应 .github/workflows/ci.yml 的 backend-offline 作业；零 DB / 零模型调用 / 零密钥）。
#
# 用法（PowerShell，cwd 任意；脚本以自身位置定位 backend）：
#   powershell -ExecutionPolicy Bypass -File backend\scripts\offline_regression.ps1
#   powershell -ExecutionPolicy Bypass -File backend\scripts\offline_regression.ps1 -Frontend
#
# 纳入步骤：pytest 纯离线单测、check_env 导入与 FastAPI 装配自检、全量静态编译；
# 加 -Frontend 时额外执行 npm ci 与 vite build（对应 CI 的 frontend-build 作业）。
# 不纳入：依赖 PostgreSQL 或真实 Embedding/Rerank/LLM 的脚本（本地按需按 README 命令执行）。
# 退出码：0=全部通过；1=存在失败步骤。
[CmdletBinding()]
param(
    [switch]$Frontend
)

$ErrorActionPreference = "Continue"
$BackendDir = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $BackendDir
Set-Location $RepoRoot

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$script:fail = 0

function Run-Step {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$ArgList
    )
    Write-Host ""
    Write-Host "== $Name =="
    if ($ArgList) {
        & $Exe @ArgList
    } else {
        & $Exe
    }
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) {
        Write-Host "[FAIL] $Name (exit=$code)"
        $script:fail += 1
    } else {
        Write-Host "[PASS] $Name"
    }
}

Run-Step -Name "pytest offline unit tests" -Exe $python -ArgList @("-m", "pytest", "backend\tests", "-q")
Run-Step -Name "check_env import & assembly" -Exe $python -ArgList @("backend\scripts\check_env.py")
Run-Step -Name "static compileall" -Exe $python -ArgList @("-m", "compileall", "-q", "backend")

if ($Frontend) {
    $feDir = Join-Path $RepoRoot "frontend"
    Push-Location $feDir
    Write-Host ""
    Write-Host "== frontend npm ci =="
    npm ci
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] npm ci"; $script:fail += 1 }
    Write-Host "== frontend vite build =="
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] vite build"; $script:fail += 1 }
    Pop-Location
}

Write-Host ""
if ($script:fail -eq 0) {
    Write-Host "OFFLINE_REGRESSION = PASS"
    exit 0
}
Write-Host "OFFLINE_REGRESSION = FAIL ($($script:fail) step(s) failed)"
exit 1
