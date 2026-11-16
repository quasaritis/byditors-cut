@echo off
setlocal
title Byditors Cut
cd /d "%~dp0"
echo.
echo   Byditors Cut
echo   ------------
echo.

rem ---------- 1. Python 3.10+ ----------
set "PY="
set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if defined PY goto have_python
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PYEXE%" goto have_python
set "PYEXE="

echo   Python isn't installed. Installing it now (one time, about 1 minute)...
where winget >nul 2>&1 || goto no_winget
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PYEXE%" goto python_failed

:have_python

rem ---------- 2. ffmpeg ----------
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
where ffmpeg >nul 2>&1 && where ffprobe >nul 2>&1 && goto have_ffmpeg
echo   ffmpeg isn't installed. Installing it now (one time, about 1 minute)...
where winget >nul 2>&1 || goto no_winget
winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
where ffmpeg >nul 2>&1 || goto ffmpeg_failed

:have_ffmpeg

rem ---------- 3. The app's own environment ----------
rem Reinstall when requirements.txt changed since the last setup (an update can add packages).
if exist ".venv\installed.ok" fc /b requirements.txt ".venv\installed.ok" >nul 2>&1 && goto run
echo   Setting up Byditors Cut (one time, a few minutes)...
if exist ".venv\Scripts\python.exe" goto venv_ready
if defined PYEXE ("%PYEXE%" -m venv .venv || goto setup_failed) else (%PY% -m venv .venv || goto setup_failed)
:venv_ready
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto setup_failed
copy /y requirements.txt ".venv\installed.ok" >nul

:run
if not exist .env copy .env.example .env >nul
echo.
echo   Starting... your browser opens at http://127.0.0.1:8777
echo   Keep this window open while you use it. Close it to stop.
echo.
".venv\Scripts\python.exe" -m bcut
pause
exit /b 0

:no_winget
echo.
echo   This PC can't install things automatically (winget is missing).
echo   Install these two, then double-click start.bat again:
echo     Python 3.12:  https://www.python.org/downloads/   (tick "Add python.exe to PATH")
echo     ffmpeg:       https://www.gyan.dev/ffmpeg/builds/
pause
exit /b 1

:python_failed
echo.
echo   Python didn't install. Install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH"), then double-click start.bat again.
pause
exit /b 1

:ffmpeg_failed
echo.
echo   ffmpeg was installed, but Windows needs a fresh window to see it.
echo   Close this window and double-click start.bat again.
pause
exit /b 1

:setup_failed
echo.
echo   Setup didn't finish (usually the internet dropped). Double-click start.bat to try again.
if exist ".venv\installed.ok" del ".venv\installed.ok"
pause
exit /b 1
