@echo off
REM J-ONE Fingerprint Bridge — foreground launcher (Windows 11 workstation).
REM Runs the bridge on http://127.0.0.1:8787 for the signed-in user.
REM Keep this window open while fingerprint capture is in use, or install
REM the logon task with install_service.ps1 and close it.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 fingerprint_bridge_service.py %*
) else (
    python fingerprint_bridge_service.py %*
)
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo The fingerprint bridge stopped with exit code %EXITCODE%.
    echo If it reports that the port is in use, the bridge is probably
    echo already running - check http://127.0.0.1:8787/health instead of
    echo starting a second copy.
    pause
)
exit /b %EXITCODE%
