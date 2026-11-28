<#
    J-ONE Reader Agent -- remove the agent from this computer.

    Removes the jone-reader: URL scheme, the logon entry, the Start Menu
    shortcut and the local state (config + cached templates, both DPAPI
    protected). It does NOT touch the DigitalPersona driver, the reader, the
    J-ONE backend records, or the browser. Revoke the agent in the dashboard
    afterwards if the PC is being retired.
#>
[CmdletBinding()]
param([switch]$KeepState)

$ErrorActionPreference = "Continue"
Write-Host "==> Removing the J-ONE Reader Agent" -ForegroundColor Cyan

Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*jone_reader_agent.py*" } |
    ForEach-Object { Write-Host "    stopping process $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-Item -Path "HKCU:\Software\Classes\jone-reader" -Recurse -Force -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "JONE-Reader-Agent" -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\J-ONE Fingerprint Reader.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\JONE-Reader-Agent.cmd") -Force -ErrorAction SilentlyContinue

if (-not $KeepState) {
    Remove-Item -Path (Join-Path $env:LOCALAPPDATA "JONE\ReaderAgent") -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "    local state removed" -ForegroundColor Green
}

Write-Host "Done. The fingerprint reader driver and the J-ONE records were not touched." -ForegroundColor Green
