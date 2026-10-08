@echo off
rem homeant.cmd - bare-name Windows entry (PATHEXT resolves "homeant" here)
rem Thin wrapper: all logic lives in scripts/homeant.py
setlocal EnableExtensions
set "HAC_DIR=%~dp0"
set "HAC_PY=%HAC_DIR%.venv\Scripts\python.exe"
set "HAC_MAIN=%HAC_DIR%scripts\homeant.py"

if not exist "%HAC_PY%" (
    echo [homeant] ERROR: .venv not found - run 'uv sync' first 1>&2
    exit /b 1
)
"%HAC_PY%" "%HAC_MAIN%" %*
exit /b %ERRORLEVEL%
