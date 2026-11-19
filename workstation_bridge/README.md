# J-ONE Fingerprint Bridge (workstation service)

A browser cannot talk to a USB device. The DigitalPersona U.are.U 4500 is plugged
into the **reception PC**, not into the API server, so this small service runs on
that PC, owns the reader, and exposes four loopback-only endpoints:

| Method | Path          | Purpose                                                        |
|--------|---------------|----------------------------------------------------------------|
| GET    | `/health`     | Liveness, version, platform                                    |
| GET    | `/status`     | Reader state: `connected`, `not_connected`, `driver_missing`, … |
| POST   | `/initialize` | (Re)open the reader after a replug or a driver restart          |
| POST   | `/capture`    | Wait for a finger, return ISO 19794-2 minutiae + an image       |

```
http://127.0.0.1:8787/health
```

It binds to `127.0.0.1` only. The hosted site reaches it because `_headers` /
`vercel.json` allowlist exactly `http://127.0.0.1:*` and `http://localhost:*` in
`connect-src` — no wildcard, no remote origin. Requests carry **no** JWT: the
bridge is not the API and holds no credential. Authorisation (who may clock in,
which staff record a capture belongs to) stays on `https://api.jonehotels.com`,
which receives only the ISO template the bridge returns.

## Run it

```
run_fingerprint_bridge.bat            # by hand, for diagnosis
python fingerprint_bridge_service.py  # same, with explicit args
python fingerprint_bridge_service.py --port 9100 --allow-origin https://jonehotels.com
```

Options: `--host` (default `127.0.0.1`), `--port` (default `8787`, also
`JONE_FINGERPRINT_BRIDGE_PORT`), `--allow-origin` (repeatable).

## Install as a background service

```
powershell -ExecutionPolicy Bypass -File install_service.ps1
```

Installs a Scheduled Task that starts the bridge at logon for the reception
account. See `../docs/WINDOWS_11_FINGERPRINT_SETUP.md` for the full Windows 11
procedure, including driver installation and verification.

## State vocabulary

`connected` · `initializing` · `not_connected` · `driver_missing` ·
`sdk_unavailable` · `permission_denied` · `busy` · `communication_failure`

`driver_missing` / `sdk_unavailable` mean the DigitalPersona driver (which
provides `dpfpdd.dll` and `dpfj.dll`) is not installed — the reader is not a HID
keyboard device and no generic USB driver can replace it.

## Tests

```
python -m unittest discover -s workstation_bridge/tests -v
```
