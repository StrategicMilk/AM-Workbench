@echo off
:: Vetinari Startup Script (Windows)
:: Usage: start.bat [options]
::   start.bat                          -- Start with dashboard
::   start.bat --goal "My goal here"    -- Run a specific goal
::   start.bat --no-dashboard           -- CLI only
::   start.bat serve                    -- Dashboard only

setlocal EnableExtensions

:: Change to script directory
cd /d "%~dp0"
set "SCRIPT_DIR=%CD%"
set "VENV_DIR=%SCRIPT_DIR%\.venv312"
set "VETINARI_PYTHON=%VENV_DIR%\Scripts\python.exe"

if not exist "%VETINARI_PYTHON%" (
    echo [ERROR] Canonical environment not found at "%VENV_DIR%".
    echo         Create or repair .venv312 before starting Vetinari.
    exit /b 1
)

:: Normalize PATH so child processes resolve the same interpreter.
set "PATH=%VENV_DIR%\Scripts;%PATH%"

:: Set default environment variables if not already set
if not defined VETINARI_MODELS_DIR if exist "%SCRIPT_DIR%\models" (
    set "VETINARI_MODELS_DIR=%SCRIPT_DIR%\models"
)

echo ============================================================
echo  VETINARI AI Orchestration System
"%VETINARI_PYTHON%" -c "import sys, vetinari; print(' Interpreter: ' + sys.executable); print(' Python: ' + sys.version.split()[0]); print(' Vetinari: ' + vetinari.__version__)"
echo ============================================================

:: Run Vetinari — propagate child exit code to caller
if "%~1"=="" (
    "%VETINARI_PYTHON%" -m vetinari start %*
    exit /b %errorlevel%
) else if /i "%~1"=="serve" (
    shift
    "%VETINARI_PYTHON%" -m vetinari serve %*
    exit /b %errorlevel%
) else if /i "%~1"=="run" (
    shift
    "%VETINARI_PYTHON%" -m vetinari run %*
    exit /b %errorlevel%
) else if /i "%~1"=="status" (
    "%VETINARI_PYTHON%" -m vetinari status
    exit /b %errorlevel%
) else if /i "%~1"=="review" (
    :: review is an interactive command — pause so the user can read the output
    "%VETINARI_PYTHON%" -m vetinari review
    set VETINARI_EXITCODE=%errorlevel%
    pause
    exit /b %VETINARI_EXITCODE%
) else (
    "%VETINARI_PYTHON%" -m vetinari start %*
    exit /b %errorlevel%
)
