@echo off
rem DISPOSITION: Retained helper — launches the canonical project Python
rem interpreter (.venv312) with any arguments forwarded verbatim.
rem
rem Purpose: lets operators run "python.cmd -m vetinari serve" or
rem "python.cmd scripts/foo.py" from the repo root without first activating
rem the virtual environment in their shell.
rem
rem Usage:
rem   python.cmd -m vetinari serve
rem   python.cmd scripts/memory_cli.py search "query"
setlocal
set "SCRIPT_DIR=%~dp0"
set "VETINARI_PYTHON=%SCRIPT_DIR%.venv312\Scripts\python.exe"

if not exist "%VETINARI_PYTHON%" (
    echo [ERROR] Canonical environment not found at "%VETINARI_PYTHON%".
    exit /b 1
)

"%VETINARI_PYTHON%" %*
