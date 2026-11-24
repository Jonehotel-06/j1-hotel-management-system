' Launches the J-ONE Fingerprint Bridge silently without a visible console
' window so reception staff cannot accidentally terminate the bridge.
' Log output is written to %LOCALAPPDATA%\JONE\fingerprint_bridge.log.

Option Explicit
Dim fso, shell, scriptDir, batPath, pywPath, servicePy, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
servicePy = fso.BuildPath(scriptDir, "fingerprint_bridge_service.py")
batPath = fso.BuildPath(scriptDir, "run_fingerprint_bridge.bat")

' Prefer pythonw.exe if available on PATH or standard per-user/system paths.
pywPath = ""
Dim candidates, i, p
candidates = Array( _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe", _
    shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe", _
    "C:\Windows\pyw.exe" _
)

For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        pywPath = candidates(i)
        Exit For
    End If
Next

If pywPath <> "" And fso.FileExists(servicePy) Then
    cmd = """" & pywPath & """ """ & servicePy & """"
Else
    cmd = "cmd.exe /c """"" & batPath & """"""
End If

shell.CurrentDirectory = scriptDir
shell.Run cmd, 0, False
