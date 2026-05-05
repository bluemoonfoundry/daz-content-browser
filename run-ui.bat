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
pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet || goto install_failed
pip install ".[local_llm]" --quiet || goto install_failed
goto install_ok

:install_failed
echo.
echo ERROR: Dependency installation failed. Check the output above for details.
pause
exit /b 1

:install_ok
echo.
echo To use a GPU instead, run after setup:
echo   .venv\Scripts\activate.bat
echo   pip install torch --index-url https://download.pytorch.org/whl/cu121
echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

start /B cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"
python vab.py server
pause
