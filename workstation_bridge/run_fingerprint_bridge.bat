@echo off
setlocal EnableExtensions
title J-ONE Fingerprint Bridge

set "SCRIPT_DIR=%~dp0"
set "SERVICE_PY=%SCRIPT_DIR%fingerprint_bridge_service.py"

if not exist "%SERVICE_PY%" (
    echo [J-ONE Fingerprint Bridge] ERROR: fingerprint_bridge_service.py not found in "%SCRIPT_DIR%".
    exit /b 1
)

if "%JONE_BRIDGE_HOST%"=="" set "JONE_BRIDGE_HOST=127.0.0.1"
if "%JONE_BRIDGE_PORT%"=="" set "JONE_BRIDGE_PORT=8787"

set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY_CMD=python"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) else if exist "C:\Python312\python.exe" (
        set "PY_CMD=C:\Python312\python.exe"
    ) else if exist "C:\Python311\python.exe" (
        set "PY_CMD=C:\Python311\python.exe"
    )
)

if "%PY_CMD%"=="" (
    echo [J-ONE Fingerprint Bridge] ERROR: Python 3.10+ was not found on PATH.
    echo Install Python 3 from https://www.python.org/downloads/windows/ and check "Add python.exe to PATH".
    exit /b 1
)

echo [J-ONE Fingerprint Bridge] Starting on http://%JONE_BRIDGE_HOST%:%JONE_BRIDGE_PORT% ...
%PY_CMD% "%SERVICE_PY%" --host "%JONE_BRIDGE_HOST%" --port "%JONE_BRIDGE_PORT%" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [J-ONE Fingerprint Bridge] Exited with code %EXIT_CODE%. Check %%LOCALAPPDATA%%\JONE\fingerprint_bridge.log
)
exit /b %EXIT_CODE%
