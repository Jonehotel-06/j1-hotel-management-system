#requires -Version 5.1
<#
.SYNOPSIS
    Installs (or removes) the J-ONE Fingerprint Bridge as a Windows Scheduled Task
    that starts at logon for the signed-in reception user and restarts itself if it stops.

.DESCRIPTION
    The DigitalPersona U.are.U 4500 reader is a per-session USB device: only the
    interactive logon session of the reception operator can own it, so the bridge
    MUST NOT run as SYSTEM or as a traditional Windows service. This script
    registers a Scheduled Task named "J-ONE Fingerprint Bridge" that:

      * starts run_fingerprint_bridge.bat at the user's logon,
      * runs as that same user (interactive token, least privilege),
      * restarts the bridge up to 3 times if the process or the PC misbehaves,
      * never launches a second copy while one is already running (port 8787).

    It also verifies the prerequisites the bridge needs (Python, the vendor
    DLLs dpfpdd.dll/dpfj.dll, the workstation_bridge folder) and reports them
    plainly — it never hides a missing driver or SDK.

.PARAMETER BridgeDir
    Folder that contains run_fingerprint_bridge.bat and fingerprint_bridge_service.py.
    Default: C:\JONE\workstation_bridge (the layout documented in
    docs/WINDOWS_11_FINGERPRINT_SETUP.md §4). When this script is launched from
    inside a workstation_bridge folder, that folder is used automatically.

.PARAMETER TaskName
    Scheduled Task name. Default: "J-ONE Fingerprint Bridge".

.PARAMETER Uninstall
    Removes the Scheduled Task instead of creating it. Does not delete the
    workstation_bridge folder or any DLLs.

.PARAMETER Force
    Overwrites an existing task of the same name without prompting.

.PARAMETER StartNow
    Starts the bridge immediately after registering the task, instead of waiting
    for the next logon. The task is idempotent (MultipleInstances=IgnoreNew, and
    the bridge itself refuses a second copy on the same port), so this never
    leaves two bridges running.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_service.ps1
    Installs the task for C:\JONE\workstation_bridge.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_service.ps1 -Uninstall
    Removes the "J-ONE Fingerprint Bridge" task.

.NOTES
    Run as the reception operator (the account that will use the reader), not as
    Administrator/SYSTEM. Elevation is only needed if BridgeDir itself must be
    created under a protected location.
#>
[CmdletBinding()]
param(
    [string]$BridgeDir = '',
    [string]$TaskName = 'J-ONE Fingerprint Bridge',
    [switch]$Uninstall,
    [switch]$Force,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$LogPrefix = '[J-ONE Fingerprint Bridge installer] '

function Write-Status($message) { Write-Host ($LogPrefix + $message) }
function Write-Warn ($message) { Write-Host ($LogPrefix + 'WARNING: ' + $message) }
function Write-Ok  ($message) { Write-Host ($LogPrefix + 'OK: ' + $message) }

# Resolve the bridge folder: explicit parameter > script's own folder > default.
if (-not $BridgeDir) {
    $scriptDir = Split-Path -Parent $PSCommandPath
    if ((Split-Path -Leaf $scriptDir) -eq 'workstation_bridge') {
        $BridgeDir = $scriptDir
    } else {
        $BridgeDir = Join-Path $env:ProgramData 'JONE\workstation_bridge'
    }
}
$BridgeDir = Resolve-Path $BridgeDir -ErrorAction SilentlyContinue
if (-not $BridgeDir) { $BridgeDir = [System.IO.Path]::GetFullPath('') }

$Bat = Join-Path $BridgeDir 'run_fingerprint_bridge.bat'
$Service = Join-Path $BridgeDir 'fingerprint_bridge_service.py'

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Ok "Scheduled Task '$TaskName' removed."
    } catch {
        if ($_.Exception.Message -like '*not found*') {
            Write-Status "Scheduled Task '$TaskName' was not registered."
        } else {
            Write-Status "Could not remove the task: $($_.Exception.Message)"
            exit 1
        }
    }
    exit 0
}

# ---- Prerequisites --------------------------------------------------------
Write-Status "Checking prerequisites for the fingerprint bridge in $BridgeDir"

$missing = @()
if (-not (Test-Path $Bat -PathType Leaf)) { $missing += 'run_fingerprint_bridge.bat' }
if (-not (Test-Path $Service -PathType Leaf)) { $missing += 'fingerprint_bridge_service.py' }
if ($missing.Count) {
    Write-Status "Missing bridge files: $($missing -join ', '). Copy the whole workstation_bridge folder first."
    exit 1
}

# Python launcher or interpreter.
$pythonCmd = if (Get-Command py -ErrorAction SilentlyContinue) { 'py -3' } elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { $null }
if (-not $pythonCmd) {
    Write-Warn 'Python was not found on PATH. The task will still be registered, but the bridge cannot start until Python 3.10+ is installed.'
} else {
    Write-Ok "Python available: $pythonCmd"
}

# The vendor SDK DLLs — the bridge looks in the standard SDK/installer folders
# and System32; report what can be found so a missing driver is obvious.
$dllRoots = @(
    "${env:ProgramFiles}\DigitalPersona\Bin",
    "${env:ProgramFiles(x86)}\DigitalPersona\Bin",
    "${env:ProgramW6432}\DigitalPersona\U.are.U SDK\Windows\Lib\x64",
    "${env:ProgramFiles}\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64",
    "${env:SystemRoot}\System32"
)
$dllStatus = @{}
foreach ($dll in @('dpfpdd.dll','dpfj.dll')) {
    $found = $null
    foreach ($root in $dllRoots) {
        $candidate = Join-Path $root $dll
        if (Test-Path $candidate -PathType Leaf) { $found = $candidate; break }
    }
    $dllStatus[$dll] = $found
    if ($found) { Write-Ok "$dll found at $found" }
    else { Write-Warn "$dll not found in the usual SDK locations — install the DigitalPersona U.are.U driver/SDK (see docs/WINDOWS_11_FINGERPRINT_SETUP.md §3) before the bridge can capture fingerprints." }
}

# ---- Task definition ------------------------------------------------------
$action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $BridgeDir

# Logon trigger: starts with the reception session. A short delay lets the
# desktop and the USB stack settle before the bridge claims the reader.
$trigger = New-ScheduledTaskTrigger -AtLogOn -Delay (New-TimeSpan -Seconds 30)
$trigger.Enabled = $true

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType InteractiveToken -RunLevel LeastPrivilege

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -DontHardTerminate:$false

# Windows 11 / Server 2019+: compatibility for the telemetry-free task we need.
try { $settings | Add-Type -ErrorAction SilentlyContinue } catch {}
$xml = $settings.Xml

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

# ---- Register -------------------------------------------------------------
try {
    if ($Force) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -User "$env:USERDOMAIN\$env:USERNAME" `
        -Force:$Force -ErrorAction Stop
    Write-Ok "Scheduled Task '$TaskName' registered — starts at logon for $env:USERDOMAIN\$env:USERNAME."
} catch {
    Write-Status "Failed to register the task: $($_.Exception.Message)"
    Write-Status "Run this script as the reception operator (not SYSTEM). Administrative rights may be needed to write to $BridgeDir."
    exit 1
}

# ---- Start Menu shortcut ---------------------------------------------------
# The desk page tells the operator "Start -> J-ONE Fingerprint Bridge". Before
# this, no program with that name existed anywhere on the workstation: the only
# entry point was a .bat file inside the bridge folder, so the one instruction
# the page gave could not be followed. The shortcut is a link to the same
# foreground launcher the Scheduled Task runs.
try {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    if (Test-Path $startMenu) {
        $linkPath = Join-Path $startMenu 'J-ONE Fingerprint Bridge.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($linkPath)
        $shortcut.TargetPath = $Bat
        $shortcut.WorkingDirectory = $BridgeDir
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'Starts the J-ONE fingerprint bridge for the DigitalPersona 4500 reader on this workstation.'
        $shortcut.Save()
        Write-Ok "Start Menu shortcut created: $linkPath"
    } else {
        Write-Warn "The Start Menu folder was not found; no shortcut was created. Start the bridge with $Bat instead."
    }
} catch {
    Write-Warn "The Start Menu shortcut could not be created ($($_.Exception.Message)). Start the bridge with $Bat instead."
}

if ($StartNow) {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Start-Sleep -Seconds 3
        $probe = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 5
        if ($probe.StatusCode -eq 200) { Write-Ok 'The bridge answered /health after starting.' }
        else { Write-Warn "The bridge started but /health answered HTTP $($probe.StatusCode)." }
    } catch {
        Write-Warn "The task was registered but the bridge did not answer /health: $($_.Exception.Message). Start it once by hand with $Bat to see the reason."
    }
}

Write-Status ''
Write-Status "Next steps:"
Write-Status "  1. Confirm the bridge works in the foreground:"
Write-Status "     cd \"$BridgeDir\""
Write-Status "     .\\run_fingerprint_bridge.bat"
Write-Status "  2. In another window:  curl.exe http://127.0.0.1:8787/health"
Write-Status "  3. Log off and log back on — the task starts the bridge automatically."
Write-Status "  4. To stop the bridge: Task Manager -> 'J-ONE Fingerprint Bridge' -> End task."
Write-Status "  5. To remove the task: powershell -ExecutionPolicy Bypass -File install_service.ps1 -Uninstall"
Write-Status "  6. Or press the Windows key and type 'J-ONE Fingerprint Bridge' to start it by hand."
exit 0
