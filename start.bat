@echo off
rem Install and run the price search agent - Windows.
rem
rem This file is ASCII-only ON PURPOSE. A .bat is read by cmd in the OEM codepage (cp866 here),
rem while the repo is UTF-8, so any Cyrillic in this file turns into garbage in the console.
rem Keep every message below in English - do not "translate back".
rem
rem What it does: checks Python, creates .venv if missing, installs dependencies (only when they
rem changed), downloads Chromium for the browser mode, prepares .env and starts the web UI.
rem Re-running does not reinstall anything it does not have to.
rem
rem Run: double-click this file, or type start.bat in the command prompt.
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem No chcp call here on purpose: Python 3.6+ writes to the Windows console through the Unicode
rem API, so the Russian output of setup_env.py and the server log shows up correctly regardless
rem of the codepage. Switching codepages would only risk breaking set /p and echo|set /p.

set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-stamp"
set "BROWSER_STAMP=%VENV%\.browser-stamp"

rem ---- 1. Python --------------------------------------------------------------
rem Look for 3.11+ : first via the py launcher, then plain python from PATH.
set "PY="
for %%V in (3.14 3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo [X] Python 3.11 or newer is required ^(3.14 recommended^).
  echo     Download: https://www.python.org/downloads/windows/
  echo     During setup make sure "Add python.exe to PATH" is checked.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%V in ('%PY% -c "import sys; print(\"%%d.%%d\" %% sys.version_info[:2])"') do set "PY_VER=%%V"
echo ^> Python !PY_VER! ^(!PY!^)

rem ---- 2. Virtual environment -------------------------------------------------
rem Reuse an existing environment only when it is sound: built by a suitable Python and not
rem broken. Otherwise the script used to reach the last step and die with "No module named
rem uvicorn" - a message that hides the real cause (a .venv left from an older Python).
set "VENV_OK="
if exist "%VPY%" (
  "%VPY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 && set "VENV_OK=1"
)
if not defined VENV_OK (
  if exist "%VENV%" (
    echo [!] %VENV% is unusable ^(no python, or version below 3.11^) - recreating
    rmdir /s /q "%VENV%"
  )
  echo ^> Creating virtual environment %VENV%
  %PY% -m venv "%VENV%"
  if errorlevel 1 (
    echo [X] Could not create %VENV%.
    pause
    exit /b 1
  )
) else (
  echo ^> Virtual environment already present
)

rem ---- 3. Dependencies (only when requirements.txt changed) -------------------
rem The fingerprint is written TO A FILE instead of being captured with for /f: cmd parses a
rem command with nested quotes its own way and silently produced an empty string on paths with
rem spaces or non-ASCII characters. An empty fingerprint equals the empty stamp of a fresh clone,
rem so the install branch was never taken and startup crashed on "No module named uvicorn".
set "WANT="
set "WANTFILE=%VENV%\.deps-want"
"%VPY%" -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())" > "%WANTFILE%" 2>nul
if exist "%WANTFILE%" set /p WANT=<"%WANTFILE%"
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"

rem Skip the install only when all three hold: fingerprint computed, equal to the stamp, and the
rem key packages actually import. Any doubt means install - a spare minute beats a crash.
set "DEPS_OK="
"%VPY%" -c "import uvicorn, fastapi, pydantic, openai, playwright" >nul 2>&1 && set "DEPS_OK=1"
if defined WANT if "!WANT!"=="!HAVE!" if defined DEPS_OK (
  echo ^> Dependencies are in place - skipping install
  goto :deps_done
)
if not defined WANT echo [!] Could not compute the requirements.txt fingerprint - installing anyway
if defined WANT if "!WANT!"=="!HAVE!" echo [!] Fingerprint matches but packages do not import - reinstalling
echo ^> Installing dependencies ^(first run takes a few minutes^)
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [X] Dependency installation failed ^(see the output above^).
  pause
  exit /b 1
)
"%VPY%" -c "import uvicorn, fastapi, pydantic, openai, playwright" >nul 2>&1
if errorlevel 1 (
  echo [X] Dependencies installed but do not import. The real error follows:
  "%VPY%" -c "import uvicorn"
  pause
  exit /b 1
)
rem Write the stamp only with a computed fingerprint: an empty one would claim "all installed"
rem on the next run while confirming nothing.
if defined WANT >"%STAMP%" echo|set /p="!WANT!"
:deps_done

rem ---- 4. Chromium for the browser mode ---------------------------------------
rem A separate download (~150 MB per engine): the pip package ships no browser. Without it only
rem the fast http mode works and protected sites will not open.
if not exist "%BROWSER_STAMP%" (
  echo ^> Downloading Chromium for playwright and patchright
  "%VPY%" -m playwright install chromium
  if errorlevel 1 echo [!] playwright: Chromium download failed - browser mode unavailable
  "%VPY%" -m patchright install chromium
  if errorlevel 1 echo [!] patchright: Chromium download failed - anti-detect unavailable
  echo ok>"%BROWSER_STAMP%"
) else (
  echo ^> Chromium already downloaded
)

rem ---- 5. Settings file -------------------------------------------------------
rem Prepares .env (creates it when missing, strips comments, blanks placeholders) and asks
rem NOTHING: both tokens are entered in the web UI and apply without a restart. One
rem implementation for both systems - scripts/setup_env.py.
echo ^> Checking settings ^(.env^)
"%VPY%" scripts\setup_env.py
if errorlevel 1 echo [!] Could not prepare .env - set the keys manually

rem ---- 6. Start ---------------------------------------------------------------
if not defined WEBUI_PORT set "WEBUI_PORT=8770"
echo ^> Starting the web UI: http://127.0.0.1:%WEBUI_PORT%/  ^(the browser opens by itself^)
echo ^> Stop with Ctrl+C in this window
"%VPY%" -m webui.run
if errorlevel 1 pause
