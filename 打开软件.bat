@echo off
setlocal

cd /d "%~dp0"
set "LOCAL_FFMPEG_ROOT=%CD%\venv\tools\ffmpeg"
set "LOCAL_FFMPEG_BIN=%LOCAL_FFMPEG_ROOT%\bin"
set "LOCAL_FFMPEG_EXE=%CD%\venv\tools\ffmpeg\bin\ffmpeg.exe"
set "LOCAL_FFPROBE_EXE=%CD%\venv\tools\ffmpeg\bin\ffprobe.exe"
set "LOCAL_WECHAT_CLI_ROOT=%CD%\venv\tools\wechat-cli"
set "LOCAL_WECHAT_CLI_EXE=%LOCAL_WECHAT_CLI_ROOT%\wechat-cli.exe"
set "LOCAL_WECHAT_CLI_BIN_EXE=%LOCAL_WECHAT_CLI_ROOT%\bin\wechat-cli.exe"
set "LOCAL_WECHAT_CLI_PYENV_EXE=%LOCAL_WECHAT_CLI_ROOT%\pyenv\Scripts\wechat-cli.exe"
set "FFMPEG_RELEASE_PATH=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
set PIP_DEPENDENCIES="flask" "pywin32" "openai" "requests" "schedule" "wxautox4>=40.1.14" "cozepy" "websockets" "Pillow"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if /i "%~1"=="--create-venv-only" (
    call :create_venv
    exit /b %errorlevel%
)

if not exist "venv\Scripts\python.exe" (
    echo Python virtual environment not found. Creating venv...
    call :create_venv
    if errorlevel 1 goto venv_failed
)

if not exist "venv\.deps_installed" (
    echo Installing dependencies...
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERROR] Failed to upgrade pip.
        pause
        exit /b 1
    )
    "venv\Scripts\python.exe" -m pip install %PIP_DEPENDENCIES%
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo ok> "venv\.deps_installed"
)

"venv\Scripts\python.exe" -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing Pillow for image compression...
    "venv\Scripts\python.exe" -m pip install "Pillow"
    if errorlevel 1 (
        echo [ERROR] Failed to install Pillow.
        pause
        exit /b 1
    )
)

if not exist "data\config" (
    mkdir data\config
)

call :ensure_ffmpeg
if errorlevel 1 (
    echo [WARNING] Failed to prepare ffmpeg/ffprobe. Voice replies may fall back to text.
)

call :wechat_cli_enabled
if errorlevel 1 (
    set "WXBOT_DISABLE_WECHAT_CLI=1"
    echo wechat-cli local reader disabled by config. Skip install/init/check.
) else (
    call :ensure_wechat_cli
)

echo Starting WXBot Pro...
echo Working directory: %cd%

"venv\Scripts\python.exe" web_server.py

echo.
echo WXBot Pro has stopped.
pause
exit /b 0

:wechat_cli_enabled
if /i "%WXBOT_DISABLE_WECHAT_CLI%"=="1" exit /b 1
if /i "%WXBOT_DISABLE_WECHAT_CLI%"=="true" exit /b 1
if /i "%WXBOT_DISABLE_WECHAT_CLI%"=="yes" exit /b 1
if /i "%WXBOT_DISABLE_WECHAT_CLI%"=="on" exit /b 1
if not exist "data\config\config.json" exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$cfg=Get-Content -LiteralPath 'data\config\config.json' -Raw -Encoding UTF8 | ConvertFrom-Json;" ^
  "$prop=$cfg.PSObject.Properties['wechat_cli_enabled'];" ^
  "if(-not $prop -or -not [bool]$prop.Value){ exit 1 };" ^
  "exit 0"
exit /b %errorlevel%

:ensure_wechat_cli
if exist "%LOCAL_WECHAT_CLI_EXE%" (
    set "WXBOT_WECHAT_CLI_EXE=%LOCAL_WECHAT_CLI_EXE%"
    echo Using project-local wechat-cli: %LOCAL_WECHAT_CLI_EXE%
    goto wechat_cli_config_check
)
if exist "%LOCAL_WECHAT_CLI_BIN_EXE%" (
    set "WXBOT_WECHAT_CLI_EXE=%LOCAL_WECHAT_CLI_BIN_EXE%"
    echo Using project-local wechat-cli: %LOCAL_WECHAT_CLI_BIN_EXE%
    goto wechat_cli_config_check
)
if exist "%LOCAL_WECHAT_CLI_PYENV_EXE%" (
    set "WXBOT_WECHAT_CLI_EXE=%LOCAL_WECHAT_CLI_PYENV_EXE%"
    echo Using project-local wechat-cli: %LOCAL_WECHAT_CLI_PYENV_EXE%
    goto wechat_cli_config_check
)
call :install_wechat_cli
if errorlevel 1 (
    echo [WARNING] Failed to prepare project-local wechat-cli. Trying system wechat-cli as fallback...
    where wechat-cli >nul 2>nul
    if not errorlevel 1 (
        set "WXBOT_WECHAT_CLI_EXE=wechat-cli"
        echo Detected system wechat-cli. WXBot will use it as optional local reader.
        goto wechat_cli_config_check
    )
    echo [WARNING] Failed to prepare wechat-cli. Local fast contact/history reader will be skipped.
    exit /b 0
)
if exist "%LOCAL_WECHAT_CLI_PYENV_EXE%" (
    set "WXBOT_WECHAT_CLI_EXE=%LOCAL_WECHAT_CLI_PYENV_EXE%"
    echo wechat-cli has been installed into: %LOCAL_WECHAT_CLI_ROOT%\pyenv
    goto wechat_cli_config_check
)
echo [WARNING] wechat-cli installation finished but executable was not found.
exit /b 0

:wechat_cli_config_check
if not exist "%USERPROFILE%\.wechat-cli\config.json" (
    goto wechat_cli_init
)
if not exist "%USERPROFILE%\.wechat-cli\all_keys.json" (
    goto wechat_cli_init
)
echo wechat-cli config detected. Dashboard will verify read capability after startup.
exit /b 0

:wechat_cli_init
echo wechat-cli is installed but not initialized. Trying automatic initialization...
"%WXBOT_WECHAT_CLI_EXE%" init
if not errorlevel 1 (
    echo wechat-cli initialized successfully.
    exit /b 0
)
echo [WARNING] wechat-cli automatic detection failed. Trying common Windows WeChat 4.x data directories...
set "WECHAT_CLI_DB_CANDIDATE="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$roots=@((Join-Path $env:USERPROFILE 'Documents\xwechat_files'),(Join-Path $env:USERPROFILE 'Documents\WeChat Files')); $items=@(); foreach($root in $roots){ if(Test-Path -LiteralPath $root){ Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object { $candidate=Join-Path $_.FullName 'db_storage'; if(Test-Path -LiteralPath $candidate){ $items += (Get-Item -LiteralPath $candidate) } } } }; if($items.Count -eq 1){ $items[0].FullName } elseif($items.Count -gt 1){ '__MULTIPLE_DB_STORAGE__' }"`) do (
    set "WECHAT_CLI_DB_CANDIDATE=%%D"
)
if "%WECHAT_CLI_DB_CANDIDATE%"=="__MULTIPLE_DB_STORAGE__" (
    echo [WARNING] Multiple WeChat data directories found. Skip automatic wechat-cli init to avoid binding the wrong account.
    echo [WARNING] Start the robot and use the dashboard status card to complete live account verification.
    exit /b 0
)
if defined WECHAT_CLI_DB_CANDIDATE (
    echo Trying wechat-cli init with db-dir: %WECHAT_CLI_DB_CANDIDATE%
    "%WXBOT_WECHAT_CLI_EXE%" init --db-dir "%WECHAT_CLI_DB_CANDIDATE%"
    if not errorlevel 1 (
        echo wechat-cli initialized successfully.
        exit /b 0
    )
)
echo [WARNING] wechat-cli initialization failed. WXBot will still start and fall back to wxautox4.
exit /b 0

:install_wechat_cli
echo wechat-cli not found. Installing project-local wechat-cli...
if not exist "%LOCAL_WECHAT_CLI_ROOT%" mkdir "%LOCAL_WECHAT_CLI_ROOT%"
if not exist "%LOCAL_WECHAT_CLI_ROOT%\pyenv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -m venv "%LOCAL_WECHAT_CLI_ROOT%\pyenv"
    if errorlevel 1 exit /b 1
)
"%LOCAL_WECHAT_CLI_ROOT%\pyenv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%LOCAL_WECHAT_CLI_ROOT%\pyenv\Scripts\python.exe" -m pip install --upgrade "git+https://github.com/huohuoer/wechat-cli.git"
if errorlevel 1 (
    "%LOCAL_WECHAT_CLI_ROOT%\pyenv\Scripts\python.exe" -m pip install --upgrade "wechat-cli @ https://github.com/huohuoer/wechat-cli/archive/refs/heads/main.zip"
    if errorlevel 1 exit /b 1
)
exit /b 0

:create_venv
if exist "%~dp0runtime\python\python.exe" (
    call :try_python "%~dp0runtime\python\python.exe"
    if not errorlevel 1 exit /b 0
)

py -3.12 -m venv venv >nul 2>nul
if not errorlevel 1 exit /b 0

py -V:Astral/CPython3.12 -m venv venv >nul 2>nul
if not errorlevel 1 exit /b 0

python -c "import sys; exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 (
    python -m venv venv
    if not errorlevel 1 exit /b 0
)

call :create_venv_with_uv
if not errorlevel 1 exit /b 0

for /d %%D in ("%APPDATA%\uv\python\cpython-3.12*") do (
    if exist "%%~fD\python.exe" (
        call :try_python "%%~fD\python.exe"
        if not errorlevel 1 exit /b 0
    )
)

for /d %%D in ("%LOCALAPPDATA%\uv\python\cpython-3.12*") do (
    if exist "%%~fD\python.exe" (
        call :try_python "%%~fD\python.exe"
        if not errorlevel 1 exit /b 0
    )
)

if exist "%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe" (
    call :try_python "%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe"
    if not errorlevel 1 exit /b 0
)

if exist "%USERPROFILE%\workspace\WXBot_Pro\venv\Scripts\python.exe" (
    call :try_python "%USERPROFILE%\workspace\WXBot_Pro\venv\Scripts\python.exe"
    if not errorlevel 1 exit /b 0
)

if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
    call :try_python "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if not errorlevel 1 exit /b 0
)

exit /b 1

:create_venv_with_uv
for /f "usebackq delims=" %%P in (`uv python find 3.12 2^>nul`) do (
    call :try_python "%%P"
    if not errorlevel 1 exit /b 0
)

for %%U in ("%USERPROFILE%\.cargo\bin\uv.exe" "%LOCALAPPDATA%\Programs\uv\uv.exe" "%APPDATA%\uv\uv.exe") do (
    if exist "%%~fU" (
        for /f "usebackq delims=" %%P in (`"%%~fU" python find 3.12 2^>nul`) do (
            call :try_python "%%P"
            if not errorlevel 1 exit /b 0
        )
    )
)

uv python install 3.12 >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`uv python find 3.12 2^>nul`) do (
        call :try_python "%%P"
        if not errorlevel 1 exit /b 0
    )
)

exit /b 1

:try_python
set "PYTHON_CANDIDATE=%~1"
if not exist "%PYTHON_CANDIDATE%" exit /b 1
"%PYTHON_CANDIDATE%" -c "import sys; exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
"%PYTHON_CANDIDATE%" -m venv venv
if errorlevel 1 exit /b 1
exit /b 0

:venv_failed
echo [ERROR] Failed to create venv.
echo The script could not find a usable Python 3.12 runtime.
echo.
echo Detected Python Launcher entries:
py -0p
echo.
echo If uv is installed, try:
echo   uv python install 3.12
echo   uv python find 3.12
echo.
echo Or install Python 3.12 from python.org, then run this file again.
echo.
echo If this is a packaged custom build, please ask the maintainer to package it again with bundled runtime.
pause
exit /b 1

:ensure_ffmpeg
where ffmpeg >nul 2>nul
if errorlevel 1 goto local_ffmpeg
where ffprobe >nul 2>nul
if errorlevel 1 goto local_ffmpeg
echo Detected system ffmpeg and ffprobe. Reusing existing installation.
exit /b 0

:local_ffmpeg
if exist "%LOCAL_FFMPEG_EXE%" if exist "%LOCAL_FFPROBE_EXE%" (
    set "PATH=%LOCAL_FFMPEG_BIN%;%PATH%"
    echo Using project-local ffmpeg: %LOCAL_FFMPEG_BIN%
    exit /b 0
)

call :download_ffmpeg
if errorlevel 1 exit /b 1

if exist "%LOCAL_FFMPEG_EXE%" if exist "%LOCAL_FFPROBE_EXE%" (
    set "PATH=%LOCAL_FFMPEG_BIN%;%PATH%"
    echo ffmpeg has been installed into: %LOCAL_FFMPEG_BIN%
    exit /b 0
)

exit /b 1

:download_ffmpeg
echo ffmpeg/ffprobe not found. Downloading project-local copy from BtbN...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$targetRoot=Join-Path (Get-Location).Path 'venv\tools\ffmpeg';" ^
  "$targetBin=Join-Path $targetRoot 'bin';" ^
  "$tempDir=Join-Path $env:TEMP ('siver_ffmpeg_' + [guid]::NewGuid().ToString('N'));" ^
  "$zipPath=Join-Path $tempDir 'ffmpeg.zip';" ^
  "$extractDir=Join-Path $tempDir 'extract';" ^
  "$urls=@(" ^
  "  'https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip'," ^
  "  'https://mirror.ghproxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip'," ^
  "  'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip'" ^
  " );" ^
  "New-Item -ItemType Directory -Path $tempDir | Out-Null;" ^
  "try {" ^
  "  $downloaded=$false;" ^
  "  foreach ($url in $urls) {" ^
  "    try {" ^
  "      Write-Host ('Trying ffmpeg source: ' + $url);" ^
  "      Invoke-WebRequest -Uri $url -OutFile $zipPath -TimeoutSec 45;" ^
  "      $downloaded=$true;" ^
  "      break;" ^
  "    } catch {" ^
  "      Write-Host ('Download failed: ' + $url);" ^
  "    }" ^
  "  };" ^
  "  if (-not $downloaded) { throw 'Unable to download ffmpeg from all configured sources.' };" ^
  "  Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force;" ^
  "  $innerDir=Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1;" ^
  "  if (-not $innerDir) { throw 'Downloaded ffmpeg archive is missing the expected root directory.' };" ^
  "  if (Test-Path -LiteralPath $targetRoot) { Remove-Item -LiteralPath $targetRoot -Recurse -Force };" ^
  "  New-Item -ItemType Directory -Path $targetRoot | Out-Null;" ^
  "  Copy-Item -Path (Join-Path $innerDir.FullName '*') -Destination $targetRoot -Recurse -Force;" ^
  "  if (-not (Test-Path -LiteralPath (Join-Path $targetBin 'ffmpeg.exe'))) { throw 'ffmpeg.exe missing after extraction.' };" ^
  "  if (-not (Test-Path -LiteralPath (Join-Path $targetBin 'ffprobe.exe'))) { throw 'ffprobe.exe missing after extraction.' };" ^
  "} finally {" ^
  "  if (Test-Path -LiteralPath $tempDir) { Remove-Item -LiteralPath $tempDir -Recurse -Force }" ^
  "}"
if errorlevel 1 exit /b 1
exit /b 0
