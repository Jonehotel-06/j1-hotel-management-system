# J-ONE Reader Agent (Windows)

The native component that reads the DigitalPersona 4500 for J-ONE fingerprint
attendance. It is the only part of the integration that touches the sensor.

**What it is not:** it is not a server. There is no port, no `127.0.0.1:8787`,
no bridge, no service to start every morning and no PowerShell step for the
receptionist. It only makes outbound HTTPS calls to the J-ONE API.

## How it is started (both automatic)

1. **At logon** — the installer adds a per-user `Run` entry.
2. **On demand** — the dashboard hands
   `jone-reader:capture?session=…&api=…&action=…` to Windows; the registered
   URL-scheme handler launches the agent, which is how a fingerprint action
   begins. If an agent is already running, the new launch drops the request in
   the spool folder and exits (single instance).

## Files

| File | Role |
|---|---|
| `jone_reader_agent.py` | CLI + run loop + `jone-reader:` URI handling |
| `agent_core.py` | device state machine, signed protocol client, identify/enrol flows |
| `agent_store.py` | DPAPI-protected config + gallery cache, request spool, log, mutex |
| `dp_sdk.py` | `dpfpdd.dll` / `dpfj.dll` ctypes bindings (capture, FMD, `dpfj_identify`) |
| `install_reader_agent.ps1` / `.bat` | one-time per-user install |
| `uninstall_reader_agent.ps1` | removal (leaves driver + J-ONE records alone) |

## Install (once, ~2 minutes)

```
install_reader_agent.bat
```

Then, in the dashboard on that PC: **Workforce → Fingerprint reader → Set up
fingerprint reader on this computer** (one click; it provisions this PC's key).

Requirements (one-time, see `reports/04_DRIVER_SDK_REQUIREMENTS_AND_INSTALL.md`):

* DigitalPersona 4500 **legacy** driver 4.1.1.221 (not the WBF/Windows Hello driver),
* DigitalPersona **U.are.U SDK** x64 runtime (`dpfpdd.dll`, `dpfj.dll`),
* Python 3.10+ (standard library only — nothing to `pip install`).

## Support commands

```
python jone_reader_agent.py --diagnose           # SDK / USB / driver mode as JSON
python jone_reader_agent.py --status             # current device state
python jone_reader_agent.py --self-test-capture  # read one finger, send nothing
python jone_reader_agent.py --run                # foreground run (diagnostics)
```

State and logs: `%LOCALAPPDATA%\JONE\ReaderAgent\` (`config.bin`, `gallery.bin`,
`logs\agent.log`). Config and gallery are DPAPI-protected to the Windows user;
the log contains no images and no templates.

## Data handling

* Fingerprint **images** are never written to disk, logged or transmitted — they
  live in memory just long enough for `dpfj_create_fmd_from_raw`.
* Only an ISO/IEC 19794-2:2005 FMD (~1 KB) is sent, over HTTPS, to the J-ONE API
  that stores it, and cached locally (DPAPI) so identification does not need a
  round trip per scan.
* The agent reports **measurements** (which template matched, vendor
  dissimilarity score); the API applies the threshold and the attendance rules
  and is the only thing that can mark a fingerprint action successful.

## Tests

```
python -m unittest discover -s tests -v            # 20 unit tests, no hardware
```

Hardware acceptance: `tests/HARDWARE_ACCEPTANCE.md` in this delivery.
