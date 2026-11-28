<#
    J-ONE Reader Agent -- one-time installer for the reception PC (Windows 11).

    This runs ONCE, by whoever sets the computer up (technician/administrator).
    It is NOT something the receptionist ever starts, and it installs no server:
    no port is opened, nothing listens, nothing needs restarting each morning.

    What it does
      1. copies the agent into %LOCALAPPDATA%\JONE\ReaderAgent\app
      2. registers the "jone-reader:" URL scheme for the CURRENT user, so the
         dashboard can start the agent on demand (no manual launch)
      3. registers the agent to start at logon, so the reader is live and its
         state shows on the dashboard without anyone doing anything
      4. creates a Start Menu shortcut for support ("J-ONE Fingerprint Reader")
      5. runs the agent's own diagnostics and prints exactly what is missing
         (SDK runtime / driver mode / reader)

    No administrator rights are required (everything is per-user).

    Usage:
        powershell -ExecutionPolicy Bypass -File install_reader_agent.ps1
        powershell -ExecutionPolicy Bypass -File install_reader_agent.ps1 -ProvisionPayload "<code>"
#>
[CmdletBinding()]
param(
    [string]$ProvisionPayload = "",
    [string]$Python = "",
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:LOCALAPPDATA "JONE\ReaderAgent"
$appDir = Join-Path $root "app"

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }

Write-Step "J-ONE Reader Agent installer"

# ---------------------------------------------------------------- Python ----
if (-not $Python) {
    foreach ($candidate in @("py", "python", "python3")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            if ($candidate -eq "py") {
                $probe = & py -3 -c "import sys; print(sys.executable)" 2>$null
            } else {
                $probe = & $candidate -c "import sys; print(sys.executable)" 2>$null
            }
            if ($LASTEXITCODE -eq 0 -and $probe) { $Python = $probe; break }
        }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    Write-Warn "Python 3.10+ was not found on this computer."
    Write-Warn "Install Python 3 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'),"
    Write-Warn "then run this installer again. Nothing else on the reception PC needs Python."
    exit 2
}
$version = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
$major, $minor = $version.Split(".")
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 10)) {
    Write-Warn "Python $version is too old; the agent needs 3.10 or newer."
    exit 2
}
Write-Ok "Python $version at $Python"
$pythonw = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $Python }

# ---------------------------------------------------------------- files -----
Write-Step "Stopping any previous J-ONE Reader Agent process"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*jone_reader_agent.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

Write-Step "Installing agent files to $appDir"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
foreach ($file in @("jone_reader_agent.py", "agent_core.py", "agent_store.py", "dp_sdk.py")) {
    $from = Join-Path $source $file
    if (-not (Test-Path $from)) { throw "Missing agent file: $file (run the installer from the workstation_agent folder)" }
    Copy-Item $from (Join-Path $appDir $file) -Force
}
$entry = Join-Path $appDir "jone_reader_agent.py"
Write-Ok "4 files copied"

# ---------------------------------------------------------------- scheme ----
Write-Step "Registering the jone-reader: URL scheme (current user)"
$scheme = "HKCU:\Software\Classes\jone-reader"
New-Item -Path $scheme -Force | Out-Null
Set-ItemProperty -Path $scheme -Name "(Default)" -Value "URL:J-ONE Reader Agent"
Set-ItemProperty -Path $scheme -Name "URL Protocol" -Value ""
New-Item -Path "$scheme\shell\open\command" -Force | Out-Null
$command = '"{0}" "{1}" --uri "%1"' -f $pythonw, $entry
Set-ItemProperty -Path "$scheme\shell\open\command" -Name "(Default)" -Value $command
Write-Ok "jone-reader: -> $pythonw"

# ---------------------------------------------------------------- autostart -
if (-not $NoAutoStart) {
    Write-Step "Registering the agent to start at logon"
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    if (-not (Test-Path $runKey)) { New-Item -Path $runKey -Force | Out-Null }
    Set-ItemProperty -Path $runKey -Name "JONE-Reader-Agent" -Value ('"{0}" "{1}" --run' -f $pythonw, $entry)
    Write-Ok "Starts with Windows, no manual step"
}

# --------------------------------------------------------- startup folder ---
# Repair (2026-11-26): the logon Run entry is the primary auto-start, but some
# managed Windows images disable or clean HKCU Run entries, and then the reader
# silently stops being available after a reboot with no error anywhere. The
# Startup folder entry is the second, independent auto-start so a reboot can
# never leave the desk without a reader. Both launch the same supervised loop.
if (-not $NoAutoStart) {
    Write-Step "Adding the Startup-folder auto-start (second, independent trigger)"
    $startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
    $cmdPath = Join-Path $startupDir "JONE-Reader-Agent.cmd"
    $cmdBody = '@echo off' + "`r`n" + '"' + $pythonw + '" "' + $entry + '" --run' + "`r`n"
    Set-Content -Path $cmdPath -Value $cmdBody -Encoding ASCII
    Write-Ok "Startup entry written: $cmdPath"
}

# ---------------------------------------------------------------- shortcut --
Write-Step "Creating the Start Menu shortcut"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $startMenu "J-ONE Fingerprint Reader.lnk"))
$link.TargetPath = $pythonw
$link.Arguments = '"{0}" --run' -f $entry
$link.WorkingDirectory = $appDir
$link.Description = "Starts the J-ONE fingerprint reader agent for this computer"
$link.Save()
Write-Ok "Shortcut created (only needed if Windows blocked the logon entry)"

# ---------------------------------------------------------------- provision -
if ($ProvisionPayload) {
    Write-Step "Applying the one-click setup code"
    & $Python $entry --provision $ProvisionPayload
}

# ---------------------------------------------------------------- verify ----
Write-Step "Checking the reader, the driver mode and the SDK"
& $Python $entry --diagnose

if (-not $NoAutoStart) {
    Write-Step "Starting the background reader agent now"
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}" --run' -f $entry) -WorkingDirectory $appDir -WindowStyle Hidden
    Write-Ok "Reader agent is running in the background"
}

Write-Host ""
Write-Host "NEXT STEP (once):" -ForegroundColor Cyan
Write-Host "  Open the J-ONE dashboard on this computer (Workforce > Fingerprint reader)"
Write-Host "  and press 'Set up fingerprint reader on this computer'."
Write-Host "  That one click provisions this PC. After that the reader is used with no setup,"
Write-Host "  no scripts and no services to start -- unplugging and replugging it is picked up"
Write-Host "  automatically."
Write-Host ""
Write-Host "If the diagnostics above report the Windows Biometric Framework (WBF/Windows Hello)"
Write-Host "driver, install the legacy DigitalPersona 4500 driver (4.1.1.221) -- see"
Write-Host "reports/04_DRIVER_SDK_REQUIREMENTS_AND_INSTALL.md."
