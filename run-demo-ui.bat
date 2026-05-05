@echo off
setlocal

echo ================================================
echo   Visual Asset Browser -- Demo Mode
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

echo Installing dependencies...
pip install . --quiet || goto install_failed
goto install_ok

:install_failed
echo.
echo ERROR: Dependency installation failed. Check the output above for details.
pause
exit /b 1

:install_ok
echo.
echo Starting demo server at http://localhost:8000
echo No database required in demo mode.
echo Press Ctrl+C to stop.
echo.

start /B cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"
python vab.py server --demo
pause
