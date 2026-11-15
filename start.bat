@echo off
REM Byditors Cut — double-click to start (Windows)
cd /d "%~dp0"
if not exist .venv (
  echo First run: setting things up, this takes a few minutes...
  python -m venv .venv || (echo Python 3.10+ is needed: https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env copy .env.example .env >nul
.venv\Scripts\python -m bcut
pause
