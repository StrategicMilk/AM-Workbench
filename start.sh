#!/bin/bash
# Vetinari Startup Script (Linux/macOS)
# Usage: ./start.sh [options]
#   ./start.sh                          -- Start with dashboard
#   ./start.sh --goal "My goal here"    -- Run a specific goal
#   ./start.sh --no-dashboard           -- CLI only
#   ./start.sh serve                    -- Dashboard only

set -e

# Change to script directory
cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"
VENV_DIR="$SCRIPT_DIR/.venv312"
PYTHON_EXE="$VENV_DIR/bin/python"

if [ ! -x "$PYTHON_EXE" ]; then
    echo "[ERROR] Canonical environment not found at $VENV_DIR."
    echo "        Create or repair .venv312 before starting Vetinari."
    exit 1
fi

# Normalize PATH so child processes resolve the same interpreter.
export PATH="$VENV_DIR/bin:$PATH"
export VETINARI_PYTHON="$PYTHON_EXE"

# Set default environment variables if not already set
if [ -z "${VETINARI_MODELS_DIR:-}" ] && [ -d "$SCRIPT_DIR/models" ]; then
    export VETINARI_MODELS_DIR="$SCRIPT_DIR/models"
fi

echo "============================================================"
echo " VETINARI AI Orchestration System"
"$PYTHON_EXE" -c 'import sys, vetinari; print(f" Interpreter: {sys.executable}"); print(f" Python: {sys.version.split()[0]}"); print(f" Vetinari: {vetinari.__version__}")'
echo "============================================================"

# Run Vetinari
if [ "$1" = "serve" ]; then
    shift
    "$PYTHON_EXE" -m vetinari serve "$@"
elif [ "$1" = "run" ]; then
    shift
    "$PYTHON_EXE" -m vetinari run "$@"
elif [ "$1" = "status" ]; then
    "$PYTHON_EXE" -m vetinari status
elif [ "$1" = "review" ]; then
    "$PYTHON_EXE" -m vetinari review
else
    "$PYTHON_EXE" -m vetinari start "$@"
fi
