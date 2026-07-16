param(
    [string]$Root = $PSScriptRoot,
    [string]$Zip = (Join-Path $PSScriptRoot "dist\WXBot_Pro.zip")
)

$ErrorActionPreference = "Stop"

$rootPath = (Resolve-Path -LiteralPath $Root).Path
$zipPath = [System.IO.Path]::GetFullPath($Zip)
$zipDir = Split-Path -Path $zipPath -Parent
if (-not (Test-Path -LiteralPath $zipDir)) {
    New-Item -ItemType Directory -Path $zipDir -Force | Out-Null
}

Write-Host "Packaging custom build..."
Write-Host "Output: $zipPath"

$stage = Join-Path $env:TEMP ("siver_pack_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null

try {
    $launchScript = Get-ChildItem -LiteralPath $rootPath -File -Filter "*.bat" | Select-Object -First 1
    if (-not $launchScript) {
        throw "Missing startup script (.bat) required for packaged runtime."
    }

    $includeDirs = @(
        "core",
        "feature",
        "extension",
        "templates",
        "data\system_prompts"
    )
    $includeFiles = @(
        $launchScript.Name,
        "web_server.py",
        "wxbot_core.py",
        "README.md",
        "LICENSE",
        "LOGO.ico"
    )

    foreach ($relative in $includeDirs) {
        $source = Join-Path $rootPath $relative
        if (-not (Test-Path -LiteralPath $source)) {
            throw ("Missing required directory: " + $relative)
        }
        $parent = Split-Path -Path $relative -Parent
        $destination = if ($parent) { Join-Path $stage $parent } else { $stage }
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Recurse -Force
    }

    foreach ($relative in $includeFiles) {
        $source = Join-Path $rootPath $relative
        if (-not (Test-Path -LiteralPath $source)) {
            throw ("Missing required file: " + $relative)
        }
        $parent = Split-Path -Path $relative -Parent
        $destination = if ($parent) { Join-Path $stage $parent } else { $stage }
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }

    $venvPy = Join-Path $rootPath "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPy)) {
        throw "Local venv not found. Run 打开软件.bat once on the maintainer computer before packaging."
    }

    $pythonHome = (& $venvPy -c "import sys; print(sys.base_prefix)")
    if (-not $pythonHome) {
        throw "Python 3.12 runtime not found from local venv."
    }
    $pythonHome = $pythonHome.Trim()
    if (-not (Test-Path -LiteralPath (Join-Path $pythonHome "python.exe"))) {
        throw "Python 3.12 runtime not found from local venv."
    }

    $runtimeDir = Join-Path $stage "runtime"
    New-Item -ItemType Directory -Path $runtimeDir | Out-Null
    Copy-Item -LiteralPath $pythonHome -Destination (Join-Path $runtimeDir "python") -Recurse -Force

    Get-ChildItem -LiteralPath $stage -Recurse -Force -Directory |
        Where-Object { $_.Name -eq "__pycache__" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath -Force
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

Write-Host ""
Write-Host "Package created:"
Write-Host $zipPath
Write-Host ""
Write-Host "Self-use package only includes runtime files required to start the bot."
Write-Host "Included: core\, feature\, extension\, templates\, data\system_prompts\, $($launchScript.Name), web_server.py, wxbot_core.py, README.md, LICENSE, and LOGO.ico."
Write-Host "data\config\, data\prompt\, data\accounts\, wxbot_save\ and other local runtime data are excluded from the package."
Write-Host "wxbot_logs\, backups\, dist\, tests\, docs\, venv\, and git metadata are also not included."
Write-Host "打开软件.bat will recreate venv\ and data\config\ automatically on the target machine."
Write-Host "Python 3.12 runtime is included, so the work computer does not need py or uv."
