@echo off
setlocal

echo ================================================
echo   Visual Asset Browser - Dev Mode
echo ================================================
echo   API server  ->  http://localhost:8000
echo   UI (Vite)   ->  http://localhost:5173
echo ================================================
echo.
echo Tip: run 'make install' first if you haven't set up the environment.
echo.

REM Start the API server in a separate console window
start "VAB API Server" python vab.py server

REM Give the server a moment to bind before opening the browser
timeout /t 4 /nobreak >nul

REM Open the browser to the Vite dev server
start http://localhost:5173

REM Run the Vite dev server in this window (Ctrl+C here stops Vite;
REM close the "VAB API Server" window separately to stop the API)
cd ui\src && npm run dev
