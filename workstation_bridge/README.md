# J-ONE Workstation Fingerprint Bridge

Local Windows 11 loopback service (`http://127.0.0.1:8787`) connecting the
DigitalPersona U.are.U 4500 USB fingerprint reader to the J-ONE web console
(`https://jonehotels.com`).

## Files

- `fingerprint_bridge_service.py`: Single-instance HTTP loopback bridge, Windows
  SetupAPI / `WM_DEVICECHANGE` USB monitor, auto-init/reconnect loop, and
  DigitalPersona U.are.U `dpfpdd.dll` / `dpfj.dll` ctypes bindings.
- `iso19794_2.py`: Pure-stdlib parser for ISO/IEC 19794-2:2005 and ANSI INCITS
  378-2004 minutiae templates returned by `dpfj.dll`.
- `run_fingerprint_bridge.bat`: Foreground launcher for diagnostics or manual start.
- `launch_bridge_hidden.vbs`: Background windowless launcher used by the logon task.
- `install_service.ps1`: Registers the `J-ONE Fingerprint Bridge` Scheduled Task
  (runs at logon as the signed-in reception operator) and Start Menu shortcut.
- `install_workstation_bridge.bat`: Double-click one-time installer wrapper.

## Quick Setup (Windows 11)

1. Ensure the DigitalPersona U.are.U 4500 driver & SDK runtime (`dpfpdd.dll`,
   `dpfj.dll`) are installed (do not reinstall if already working).
2. Ensure Python 3.10+ is installed with `py` or `python` on `PATH`.
3. Double-click `install_workstation_bridge.bat` (or run:
   `powershell -ExecutionPolicy Bypass -File install_service.ps1 -Force -StartNow`).
4. Verify in PowerShell or browser:
   - `http://127.0.0.1:8787/health`
   - `http://127.0.0.1:8787/status`
   - `http://127.0.0.1:8787/diagnose`

## Running Tests (no physical hardware required)

```bash
python -m unittest discover -s workstation_bridge/tests -v
```
