@echo off
setlocal EnableDelayedExpansion

echo ================================================
echo   Visual Asset Browser
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Setting up virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies (first run may take several minutes^)...
pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet
pip install ".[local_llm]" --quiet

echo.
echo To use a GPU instead, run after setup:
echo   .venv\Scripts\activate.bat
echo   pip install torch --index-url https://download.pytorch.org/whl/cu121
echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

python vab.py server
pause
