@echo off
setlocal

echo ================================================
echo   Visual Asset Browser - Demo Dev Mode
echo ================================================
echo   API server  ->  http://localhost:8000  (demo)
echo   UI (Vite)   ->  http://localhost:5173
echo ================================================
echo.
echo Tip: run 'make install' first if you haven't set up the environment.
echo.

REM Start the API server in demo mode in a separate console window
start "VAB API Server (demo)" python vab.py server --demo

REM Give the server a moment to bind before opening the browser
timeout /t 4 /nobreak >nul

REM Open the browser to the Vite dev server
start http://localhost:5173

REM Run the Vite dev server in this window
cd ui\src && npm run dev
