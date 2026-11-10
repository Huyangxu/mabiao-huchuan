@echo off
setlocal
cd /d "%~dp0"

rem Prefer the official Windows Python launcher; fall back to "python" in PATH.
rem Using one interpreter for both pip and the app avoids installing into a
rem different Python (e.g. one bundled with an editor).
set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.10+ not found. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% -c "import curl_cffi" >nul 2>nul
if errorlevel 1 (
  echo Installing dependency: curl_cffi
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. See messages above.
    pause
    exit /b 1
  )
)

echo Starting meterhub ... open http://127.0.0.1:8788
%PY% -m meterhub serve
echo.
echo meterhub stopped.
pause
