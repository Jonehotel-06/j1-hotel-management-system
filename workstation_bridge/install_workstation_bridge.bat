@echo off
setlocal
echo ========================================================================
echo   J-ONE Fingerprint Bridge - One-Time Workstation Setup (Windows 11)
echo ========================================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1" -Force -StartNow
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Setup finished successfully.
) else (
    echo Setup exited with code %RC%. Review the messages above.
)
pause
exit /b %RC%
