#Requires -Version 5.1
# Build the Windows portable folder. Run from anywhere; paths are resolved
# relative to this script so the output location does not depend on the cwd.
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $projectRoot
try {
    Write-Host "==> Installing the project and build tooling"
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"

    Write-Host "==> Building the one-folder bundle"
    python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath "dist" `
        --workpath "build" `
        "packaging/fis.spec"

    $bundle = Join-Path $projectRoot "dist/FIS"
    if (-not (Test-Path (Join-Path $bundle "FIS.exe"))) {
        throw "FIS.exe was not produced"
    }

    # The three collected resource trees are what a frozen build most often
    # loses, and losing them fails only at runtime. Check them here instead.
    foreach ($required in @(
        "_internal/fis/web/templates/workspace.html",
        "_internal/fis/web/static/app.js",
        "_internal/fis/xlsx/assets/渠道分解&Fba编码.xlsx"
    )) {
        $path = Join-Path $bundle $required
        if (-not (Test-Path -LiteralPath $path)) {
            throw "bundle is missing $required"
        }
    }

    $size = "{0:N0}" -f ((Get-ChildItem $bundle -Recurse | Measure-Object Length -Sum).Sum / 1MB)
    Write-Host "==> Built dist/FIS ($size MB)"
}
finally {
    Pop-Location
}
