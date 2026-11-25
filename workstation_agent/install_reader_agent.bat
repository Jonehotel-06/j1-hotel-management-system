@echo off
REM J-ONE Reader Agent -- double-click install for the reception PC (Windows 11).
REM Run ONCE when the computer is set up. No administrator rights needed.
REM It installs no server and opens no port: the agent only reads the reader and
REM talks HTTPS to the J-ONE API.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_reader_agent.ps1" %*
echo.
pause
