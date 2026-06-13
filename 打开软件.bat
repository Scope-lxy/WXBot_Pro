@echo off
setlocal

cd /d "%~dp0"
set "LOCAL_FFMPEG_ROOT=%CD%\venv\tools\ffmpeg"
set "LOCAL_FFMPEG_BIN=%LOCAL_FFMPEG_ROOT%\bin"
set "LOCAL_FFMPEG_EXE=%CD%\venv\tools\ffmpeg\bin\ffmpeg.exe"
set "LOCAL_FFPROBE_EXE=%CD%\venv\tools\ffmpeg\bin\ffprobe.exe"
set "FFMPEG_RELEASE_PATH=https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
set PIP_DEPENDENCIES="flask" "pywin32" "openai" "requests" "schedule" "wxautox4>=40.1.14" "cozepy" "websockets"

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

if not exist "data\config" (
    mkdir data\config
)

call :ensure_ffmpeg
if errorlevel 1 (
    echo [WARNING] Failed to prepare ffmpeg/ffprobe. Voice replies may fall back to text.
)

echo Starting WXBot Pro...
echo Working directory: %cd%

"venv\Scripts\python.exe" web_server.py

echo.
echo WXBot Pro has stopped.
pause
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
