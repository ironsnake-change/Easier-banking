$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $ProjectRoot ".desktop_venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $Python = if ($env:EASIER_BANKING_BUILD_PYTHON) {
        $env:EASIER_BANKING_BUILD_PYTHON
    } else {
        $Command = Get-Command python -ErrorAction SilentlyContinue
        if ($Command) { $Command.Source } else { $null }
    }
    if (-not $Python) {
        $Fallback = "C:\Users\COLORFUL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (Test-Path -LiteralPath $Fallback) { $Python = $Fallback }
    }
    if (-not $Python) {
        throw "Python 3.11+ is required to build the Windows desktop package."
    }
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create desktop build environment." }
}

& $VenvPython -m pip install --disable-pip-version-check fastapi uvicorn openai pydantic httpx pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Unable to install desktop build dependencies." }

$env:PYTHONPATH = $ProjectRoot
$env:PYTHONUTF8 = "1"
Push-Location $ProjectRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --clean "Easier-banking.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$Executable = Join-Path $ProjectRoot "dist\Easier-banking.exe"
if (-not (Test-Path -LiteralPath $Executable)) {
    throw "Desktop executable was not created: $Executable"
}

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash
Write-Output "Desktop package: $Executable"
Write-Output "SHA256: $Hash"
