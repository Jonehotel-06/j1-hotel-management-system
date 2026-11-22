@echo off
REM J-ONE Fingerprint Bridge - foreground launcher (Windows 11 workstation).
REM Runs the bridge on http://127.0.0.1:8787 for the signed-in user.
REM Keep this window open while fingerprint capture is in use, or install
REM the logon task with install_service.ps1 and close it.
REM
REM This launcher checks the three things that make the desk page report
REM "the fingerprint bridge is not reachable" BEFORE it starts anything, and
REM names the real cause instead of leaving the operator with a dead window:
REM
REM   1. Python 3 is installed (the bridge is a Python program);
REM   2. a bridge is not already running on the port (a second copy cannot
REM      bind the port and would exit immediately);
REM   3. the DigitalPersona DLLs are present (dpfpdd.dll / dpfj.dll). The
REM      bridge still starts without them - /status and /diagnose then report
REM      driver_missing / sdk_unavailable - but the operator is told now.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=8787"
if not "%~1"=="" (
    if "%~1"=="--port" set "PORT=%~2"
)
set "BRIDGE_URL=http://127.0.0.1:%PORT%"

REM ---- 1. Python ------------------------------------------------------------
set "PYCMD="
where py >nul 2>nul
if %ERRORLEVEL%==0 set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul
    if !ERRORLEVEL!==0 set "PYCMD=python"
)
if not defined PYCMD (
    echo.
    echo J-ONE Fingerprint Bridge cannot start: Python 3 was not found.
    echo Install 64-bit Python 3.10 or newer from python.org, tick
    echo "Add python.exe to PATH", then run this file again.
    echo See docs\WINDOWS_11_FINGERPRINT_SETUP.md section 4.
    echo.
    pause
    exit /b 1
)

REM ---- 2. A bridge that is already running ----------------------------------
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%BRIDGE_URL%/health' -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>nul
if %ERRORLEVEL%==0 (
    echo.
    echo A J-ONE Fingerprint Bridge is ALREADY running on %BRIDGE_URL%.
    echo Do not start a second copy. Check it with:
    echo     curl.exe %BRIDGE_URL%/status
    echo     curl.exe %BRIDGE_URL%/diagnose
    echo.
    start "" "%BRIDGE_URL%/status"
    pause
    exit /b 0
)

REM ---- 3. DigitalPersona driver/SDK ----------------------------------------
set "DLLMISSING="
for %%D in (
    "%ProgramFiles%\DigitalPersona\Bin"
    "%ProgramFiles(x86)%\DigitalPersona\Bin"
    "%ProgramW6432%\DigitalPersona\U.are.U SDK\Windows\Lib\x64"
    "%ProgramFiles%\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64"
) do (
    if exist "%%~D\dpfpdd.dll" if exist "%%~D\dpfj.dll" set "DLLMISSING="
)
if not defined DLLMISSING (
    set "FOUND="
    for %%D in (
        "%ProgramFiles%\DigitalPersona\Bin"
        "%ProgramFiles(x86)%\DigitalPersona\Bin"
        "%ProgramW6432%\DigitalPersona\U.are.U SDK\Windows\Lib\x64"
        "%ProgramFiles%\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64"
    ) do (
        if exist "%%~D\dpfpdd.dll" if exist "%%~D\dpfj.dll" set "FOUND=1"
    )
    if not defined FOUND set "DLLMISSING=1"
)
if defined DLLMISSING (
    echo.
    echo WARNING: the DigitalPersona DLLs (dpfpdd.dll / dpfj.dll) were not found
    echo in the usual install folders. The bridge will start, but /status will
    echo report driver_missing and no fingerprint can be read until the
    echo DigitalPersona U.are.U driver/SDK is installed on this workstation.
    echo See docs\WINDOWS_11_FINGERPRINT_SETUP.md section 3.
    echo.
)

REM ---- Start ---------------------------------------------------------------
echo Starting the J-ONE Fingerprint Bridge on %BRIDGE_URL% ...
%PYCMD% fingerprint_bridge_service.py %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo The fingerprint bridge stopped with exit code %EXITCODE%.
    echo If it reports that the port is in use, the bridge is probably
    echo already running - check %BRIDGE_URL%/health instead of starting a
    echo second copy.
    echo For a full device/SDK report run:
    echo     %PYCMD% fingerprint_bridge_service.py --diagnose
    pause
)
exit /b %EXITCODE%
