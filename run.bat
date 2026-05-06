@echo off
REM Unified launcher for VAB
REM Usage:
REM   run.bat                    - production mode with pre-built UI
REM   run.bat --demo             - demo mode with pre-built UI
REM   run.bat --dev-ui           - production mode with Vite dev server (hot-reload)
REM   run.bat --demo --dev-ui    - demo mode with Vite dev server

setlocal enabledelayedexpansion

set DEMO=false
set DEV_UI=false

REM Parse arguments
:parse_args
if "%~1"=="" goto end_parse
if "%~1"=="--demo" (
    set DEMO=true
    shift
    goto parse_args
)
if "%~1"=="--dev-ui" (
    set DEV_UI=true
    shift
    goto parse_args
)
if "%~1"=="--help" goto show_help
if "%~1"=="-h" goto show_help
echo Unknown option: %~1
echo Use --help for usage information
exit /b 1

:show_help
echo Usage: run.bat [--demo] [--dev-ui]
echo.
echo Options:
echo   --demo     Run in demo mode (mock data, no database)
echo   --dev-ui   Run with Vite dev server for UI development (requires ui/src/)
echo.
echo Examples:
echo   run.bat                    # production mode with pre-built UI
echo   run.bat --demo             # demo mode with pre-built UI
echo   run.bat --dev-ui           # production mode with Vite dev server
echo   run.bat --demo --dev-ui    # demo mode with Vite dev server
exit /b 0

:end_parse

set PYTHON=.venv\Scripts\python.exe

REM Build command
if "%DEV_UI%"=="true" (
    REM Use dev.py for Vite dev server
    if "%DEMO%"=="true" (
        %PYTHON% dev.py --demo
    ) else (
        %PYTHON% dev.py
    )
) else (
    REM Use vab.py server for pre-built UI
    if "%DEMO%"=="true" (
        %PYTHON% vab.py server --demo
    ) else (
        %PYTHON% vab.py server
    )
)

pause
