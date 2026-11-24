#requires -Version 5.1
<#
.SYNOPSIS
    One-time setup for the J-ONE Fingerprint Bridge on a Windows 11 reception
    or enrolment workstation.

.DESCRIPTION
    Preserves all existing working DigitalPersona drivers and Windows biometric
    services without reinstalling or overwriting them. Registers a Scheduled
    Task ("J-ONE Fingerprint Bridge") running as the signed-in reception user
    (InteractiveToken, LeastPrivilege) so the bridge starts automatically at
    Windows logon, survives closed terminal windows via silent background launch,
    enforces single-instance execution on 127.0.0.1:8787, and restarts on fault.

.PARAMETER BridgeDir
    Folder containing fingerprint_bridge_service.py, iso19794_2.py, and
    run_fingerprint_bridge.bat. Defaults to the folder containing this script
    when run from workstation_bridge\, otherwise %ProgramData%\JONE\workstation_bridge.

.PARAMETER TaskName
    Scheduled Task name. Default: "J-ONE Fingerprint Bridge".

.PARAMETER Uninstall
    Removes the Scheduled Task without deleting drivers or bridge files.

.PARAMETER Force
    Replaces an existing Scheduled Task of the same name without prompting.

.PARAMETER StartNow
    Starts the bridge immediately after registering the task and verifies
    http://127.0.0.1:8787/health and /status.
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

if (-not $BridgeDir) {
    $scriptDir = Split-Path -Parent $PSCommandPath
    if (Test-Path (Join-Path $scriptDir 'fingerprint_bridge_service.py')) {
        $BridgeDir = $scriptDir
    } else {
        $BridgeDir = Join-Path $env:ProgramData 'JONE\workstation_bridge'
    }
}
$resolved = Resolve-Path $BridgeDir -ErrorAction SilentlyContinue
if ($resolved) { $BridgeDir = $resolved.Path }

$Bat = Join-Path $BridgeDir 'run_fingerprint_bridge.bat'
$HiddenVbs = Join-Path $BridgeDir 'launch_bridge_hidden.vbs'
$Service = Join-Path $BridgeDir 'fingerprint_bridge_service.py'
$IsoParser = Join-Path $BridgeDir 'iso19794_2.py'

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

Write-Status "Checking prerequisites in $BridgeDir"

$missing = @()
if (-not (Test-Path $Bat -PathType Leaf)) { $missing += 'run_fingerprint_bridge.bat' }
if (-not (Test-Path $Service -PathType Leaf)) { $missing += 'fingerprint_bridge_service.py' }
if (-not (Test-Path $IsoParser -PathType Leaf)) { $missing += 'iso19794_2.py' }
if ($missing.Count) {
    Write-Status "Missing bridge files: $($missing -join ', '). Copy the whole workstation_bridge folder first."
    exit 1
}

$pythonCmd = if (Get-Command py -ErrorAction SilentlyContinue) { 'py -3' } elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { $null }
if (-not $pythonCmd) {
    Write-Warn 'Python 3 was not found on PATH. Install Python 3.10+ (with "Add python.exe to PATH") so the bridge can run.'
} else {
    Write-Ok "Python available: $pythonCmd"
}

# Read-only check for DigitalPersona runtime DLLs (never modifies or reinstalls drivers).
$dllRoots = @(
    "${env:ProgramFiles}\DigitalPersona\Bin",
    "${env:ProgramFiles}\DigitalPersona\U.are.U SDK\Windows\Lib\x64",
    "${env:ProgramFiles}\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64",
    "${env:ProgramFiles}\HID Global\Authentication\Bin",
    "${env:ProgramFiles(x86)}\DigitalPersona\Bin",
    "${env:SystemRoot}\System32"
)
foreach ($dll in @('dpfpdd.dll', 'dpfj.dll')) {
    $found = $null
    foreach ($root in $dllRoots) {
        $candidate = Join-Path $root $dll
        if (Test-Path $candidate -PathType Leaf) { $found = $candidate; break }
    }
    if ($found) { Write-Ok "$dll found at $found" }
    else { Write-Warn "$dll not found in standard DigitalPersona folders — install the DigitalPersona U.are.U Runtime/SDK once if not yet installed." }
}

# Read-only check for attached DigitalPersona USB device (VID_05BA) and Windows service.
try {
    $dpDevices = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match 'VID_05BA' }
    if ($dpDevices) {
        foreach ($d in $dpDevices) {
            Write-Ok "DigitalPersona USB device detected: $($d.FriendlyName) [$($d.Status)]"
        }
    } else {
        Write-Warn 'No DigitalPersona USB reader (VID_05BA) is currently plugged in; the bridge will detect it automatically when plugged in.'
    }
} catch {
    Write-Status "PnP device query skipped: $($_.Exception.Message)"
}

foreach ($svcName in @('DpHost', 'DpHostW', 'WbioSrvc')) {
    $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -eq 'Running') { Write-Ok "Windows service '$svcName' is Running." }
        else { Write-Warn "Windows service '$svcName' is $($svc.Status). Start it in Services if the reader reports service_unavailable." }
    }
}

# Prefer windowless VBS launcher so closing a terminal cannot kill the bridge.
if (Test-Path $HiddenVbs -PathType Leaf) {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument """$HiddenVbs""" -WorkingDirectory $BridgeDir
} else {
    $action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory $BridgeDir
}

# Trigger at logon + short 10s delay so USB PnP and DpHost finish booting.
$trigger = New-ScheduledTaskTrigger -AtLogOn -Delay (New-TimeSpan -Seconds 10)
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
    -MultipleInstances IgnoreNew

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

try {
    if ($Force) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -User "$env:USERDOMAIN\$env:USERNAME" `
        -Force:$Force -ErrorAction Stop | Out-Null
    Write-Ok "Scheduled Task '$TaskName' registered — starts automatically at logon for $env:USERDOMAIN\$env:USERNAME."
} catch {
    Write-Status "Failed to register Scheduled Task: $($_.Exception.Message)"
    exit 1
}

try {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    if (Test-Path $startMenu) {
        $linkPath = Join-Path $startMenu 'J-ONE Fingerprint Bridge.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($linkPath)
        if (Test-Path $HiddenVbs -PathType Leaf) {
            $shortcut.TargetPath = 'wscript.exe'
            $shortcut.Arguments = """$HiddenVbs"""
        } else {
            $shortcut.TargetPath = $Bat
        }
        $shortcut.WorkingDirectory = $BridgeDir
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'Starts the J-ONE fingerprint bridge for the DigitalPersona 4500 reader.'
        $shortcut.Save()
        Write-Ok "Start Menu shortcut created: $linkPath"
    }
} catch {
    Write-Warn "Start Menu shortcut could not be created ($($_.Exception.Message))."
}

if ($StartNow) {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $healthy = $false
        for ($i = 0; $i -lt 8; $i++) {
            Start-Sleep -Seconds 1
            try {
                $probe = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 2
                if ($probe.StatusCode -eq 200) {
                    $healthy = $true
                    break
                }
            } catch {
                # still starting
            }
        }
        if ($healthy) {
            Write-Ok 'Bridge is running and answered http://127.0.0.1:8787/health.'
            try {
                $st = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/status' -TimeoutSec 3
                Write-Status ("Current /status: " + $st.Content)
            } catch {}
        } else {
            Write-Warn "Task started, but http://127.0.0.1:8787/health did not answer within 8s. Run `"$Bat`" once in a terminal or check %LOCALAPPDATA%\JONE\fingerprint_bridge.log."
        }
    } catch {
        Write-Warn "Task registered, but Start-ScheduledTask failed: $($_.Exception.Message)"
    }
}

Write-Status ''
Write-Status 'Setup complete. Bridge runs automatically at logon and stays resident in the background.'
Write-Status "Logs: $env:LOCALAPPDATA\JONE\fingerprint_bridge.log"
exit 0
