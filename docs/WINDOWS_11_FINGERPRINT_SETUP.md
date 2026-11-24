# Windows 11 fingerprint setup — DigitalPersona U.are.U 4500

**Audience:** IT / front-desk supervisor setting up a reception workstation.
**Applies to:** jonehotels.com reception terminals that clock staff in and out with a fingerprint.
**Written:** 2026-11-19, after the fingerprint-bridge repair (see `J1_FINGERPRINT_BRIDGE_AND_MEDIA_REPAIR_OUTPUT/`).

---

## 1. Why there is a bridge at all

The U.are.U 4500 is a **USB device attached to the reception PC**. A web page
running in Chrome/Edge **cannot** open a USB device directly, and the hosted API
(`https://api.jonehotels.com`) runs in a data centre where the reader is not
plugged in. Earlier the desk page asked the hosted API to perform the capture —
which is why it could never work, and why the URL defect produced

```
Fetch API cannot load https://api.jonehotels.comhttps//api.jonehotels.com/api/admin/staff-operations/biometrics/bridge/capture/
```

The repair introduces a **workstation bridge**: a small Python service on the
reception PC that owns the reader and answers on `http://127.0.0.1:8787`. The
browser talks to that loopback address only (allowlisted in `connect-src`); the
resulting ISO 19794-2 template is posted to the hosted API, which decides whether
the capture is acceptable and which staff member it belongs to. **No credential
is ever sent to the bridge.**

```
reception PC                                        data centre
┌───────────────────────────┐   HTTPS    ┌──────────────────────────────┐
│ Chrome / Edge             │ ─────────► │ api.jonehotels.com           │
│  js/biometrics.js         │            │  /api/admin/staff-operations │
│        │ loopback HTTP     │            │  /biometrics/... (verify,    │
│        ▼                   │            │   policy, audit)             │
│ Fingerprint Bridge :8787  │            └──────────────────────────────┘
│        │ vendor SDK        │
│        ▼                   │
│ U.are.U 4500 (USB 05BA:000A)│
└───────────────────────────┘
```

---

## 2. Hardware checklist

| Item | Requirement |
|------|-------------|
| Reader | HID Global / DigitalPersona **U.are.U 4500** (USB `VID 0x05BA`, `PID 0x000A`) |
| Port | USB 2.0 port **directly on the PC** (not a hub, not a KVM switch) |
| PC | Windows 10/11 x64, Python 3.10+ |
| Browser | Chrome or Edge (latest) |
| Network | The PC must reach `https://api.jonehotels.com`; the bridge needs no inbound network access |

`VID/PID` is the identity to trust. The reader is **vendor-specific USB**, not a
HID keyboard, so Windows will *not* produce keystrokes from it and it never shows
up as a barcode-scanner-style input device. If Windows Device Manager shows an
unknown device, the vendor driver is missing (§3).

---

## 3. Install the DigitalPersona driver

1. Get the driver package that ships with the U.are.U 4500 (HID Global
   *DigitalPersona U.are.U SDK / Driver*, x64). Do not use a generic USB driver.
2. Run the installer as administrator, accept the licence, finish, then **replug
   the reader**.
3. Confirm the driver landed:

   ```powershell
   Test-Path "C:\Program Files\DigitalPersona\Bin\dpfpdd.dll"
   Test-Path "C:\Program Files\DigitalPersona\Bin\dpfj.dll"
   ```

   Both must print `True`. `dpfpdd.dll` talks to the device; `dpfj.dll` produces
   the ISO 19794-2 feature set the API verifies against.
4. Confirm Windows sees the reader:

   ```powershell
   Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like "*VID_05BA*" } |
     Select-Object Status, Class, FriendlyName, InstanceId | Format-List
   ```

   Expect `Status : OK` and `Class : Biometric` (or `USB`/`Image` depending on
   the driver build). Any other status means the driver step failed —
   **stop here**, the bridge will report `driver_missing`.

---

## 4. Install the bridge

1. Copy the `workstation_bridge` folder (from `JONE_FINGERPRINT_PERMANENT_REPAIR_OUTPUT/updated_files/workstation_bridge/`)
   to `C:\JONE\workstation_bridge`.
2. No Python packages need installing: the bridge (including the
   ISO/IEC 19794-2 template parser, `iso19794_2.py`) uses only the Python
   standard library. Keep the complete `workstation_bridge` folder together
   — commit `dfde0ce` deleted `iso19794_2.py`, which crashed the bridge on
   startup and caused "the fingerprint bridge is not reachable".
   The bridge finds `dpfpdd.dll` / `dpfj.dll` in `C:\Program Files\DigitalPersona\Bin`,
   `C:\Program Files\HID Global\DigitalPersona\U.are.U SDK\Windows\Lib\x64`
   (and the standard DigitalPersona / HID Global `x64` / `win32` SDK folders),
   `System32`, or a folder named by `JONE_DPFP_DLL_DIR` /
   `JONE_DIGITALPERSONA_DLL_DIR`.

3. **One-time double-click workstation setup (recommended):**
   Double-click `install_workstation_bridge.bat` (or run
   `powershell -ExecutionPolicy Bypass -File install_service.ps1 -Force -StartNow`).
   This registers the Windows 11 Scheduled Task **J-ONE Fingerprint Bridge**
   running hidden via `launch_bridge_hidden.vbs` / `pythonw.exe` at logon for
   the signed-in workstation user (so closing any terminal cannot kill it),
   creates the Start Menu shortcut **J-ONE Fingerprint Bridge**, starts the
   bridge immediately, and verifies `http://127.0.0.1:8787/health` and `/status`.

4. Optional foreground diagnostic test:

   ```powershell
   cd C:\JONE\workstation_bridge
   .\run_fingerprint_bridge.bat
   ```

   Expect log lines naming the service (`2.1.0`), `127.0.0.1:8787`, and the
   allowed origins. From a second window, check:

   ```powershell
   curl.exe http://127.0.0.1:8787/health
   curl.exe http://127.0.0.1:8787/status
   ```

   A working reader answers `/status` with `"status":"connected"`,
   `"connected":true`, `"ui_label":"Fingerprint reader ready"`, and a
   `device_serial`. Plugging or unplugging the USB reader is automatically
   detected within ~1 second and streamed over `GET /events` (`text/event-stream`).

---

## 5. Verify from the browser (this is the part that proves the repair)

1. Sign in at `https://jonehotels.com` on that PC and open the attendance /
   clock-in screen.
2. Open DevTools → **Network**, filter `8787`.
3. Press *Capture* (or place a finger). You must see exactly these requests:

   | Request | Host | Result |
   |---------|------|--------|
   | `GET /status` | `127.0.0.1:8787` | `200` `{"status":"connected"}` |
   | `POST /initialize` (only if the reader was idle/unplugged) | `127.0.0.1:8787` | `200` |
   | `POST /capture` | `127.0.0.1:8787` | `200` with `minutiae` |
   | `POST /api/admin/staff-operations/biometrics/verify/` (or the punch endpoint) | `api.jonehotels.com` | `200` |

4. **Fail conditions to look for**
   * A request to `api.jonehotels.com/api/.../bridge/capture/` — the old,
     impossible path. It must not appear at all; if it does, the deployed
     `js/biometrics.js` is stale (hard-refresh with Ctrl+F5, and confirm the
     static-host cache was purged).
   * `Permission was denied for this request to access the `loopback` address
     space` (Chromium 138+, **Local Network Access**). The bridge can be
     running perfectly and the request still never leaves the browser. This is
     NOT "the bridge is not running". Check, in this order:
       - the site's response headers contain
         `Permissions-Policy: … local-network-access=(self)`
         (`curl -sI https://jonehotels.com/ | grep -i permissions-policy`);
       - the page shows a "Blocked by this browser" reader badge with an
         **Allow local network access** button — press it and accept the
         browser prompt;
       - if you previously dismissed the prompt, re-allow it from the lock
         icon → *Local network access* → *Allow*, then reload.
     Ten minutes on a fresh profile normally settles this; the same block
     happens on every site, so a new bridge or a new PC changes nothing.
   * `status` says `busy` — a capture is already in flight (another tab, or a
     finger resting on the sensor). Wait for it to finish; the reader answers
     one capture at a time by design.
   * `net::ERR_CONNECTION_REFUSED` on `127.0.0.1:8787` — the bridge is not
     running (§4.3) or listens on another port.
   * A CSP violation mentioning `connect-src` — the deployed `_headers` /
     `vercel.json` is stale; both must contain
     `connect-src 'self' https://api.jonehotels.com http://127.0.0.1:* http://localhost:*;`.
   * A mixed-content warning — the page is HTTPS and the bridge is HTTPS too;
     the bridge is plain HTTP **on loopback**, which browsers permit. A
     non-loopback bridge address is refused on purpose by `js/config.js`.

5. Physically confirm the reader: place a finger → the reader's LED flashes and
   `/capture` returns within a second. A capture with `"quality"` well under 60
   usually means a dirty platen — clean it with a dry lint-free cloth.

---

## 6. Operating notes

* The bridge window can stay minimised; with the Scheduled Task it is invisible.
  Stop it from Task Manager → *J-ONE Fingerprint Bridge* → **End task** if you
  need to free port 8787.
* Port 8787 must be free on that PC only. It is not opened in Windows Firewall
  (a loopback bind needs no rule) — **do not** create an inbound rule for it.
* To move the bridge to another port, set `JONE_FINGERPRINT_BRIDGE_PORT` (or pass
  `--port`) **and** set `FINGERPRINT_BRIDGE_URL` in the deployment's
  `runtime-config.js`. `js/config.js` rejects non-loopback and path-carrying
  values, falling back to `http://127.0.0.1:8787`.
* Several reception PCs each run their own bridge; nothing is shared between
  them, and the server cannot reach any of them. If a screen is opened on a PC
  without a reader, the client reports that honestly and offers the supervised
  fallback — it never fabricates a fingerprint.

---

## 7. Troubleshooting matrix

`GET http://127.0.0.1:8787/status` is the first thing to read in every case.

| `status` | Meaning | Action |
|----------|---------|--------|
| `connected` | Reader open and idle | Nothing — capture should work |
| `initializing` | Driver enumerating after `/initialize` | Wait ~2 s, retry once |
| `not_connected` | No reader on USB | Replug directly into the PC; check §3.4 |
| `driver_missing` | `dpfpdd.dll` not found / no device node | Reinstall the DigitalPersona driver, replug, reboot |
| `sdk_unavailable` | Driver present, `dpfj.dll` missing or unloadable | Install the complete SDK (not just the minimal driver) |
| `permission_denied` | Windows denied access to the device | Run the bridge as the signed-in user (Scheduled Task), not as `SYSTEM`; check biometric policy in `gpedit.msc` |
| `busy` | Another process/tab holds the reader | Close other capture windows; restart the bridge |
| `communication_failure` | Device dropped mid-capture | Replug; if it repeats, replace the cable/reader itself |
| `service_unavailable` | The DLLs **are** installed (`driver_installed: true`) but `dpfpdd_init()` did not answer — in practice the DigitalPersona runtime service is stopped, still starting, or another program owns the reader | Start the *DigitalPersona* service (`services.msc`), replug the reader, press **Retry**. Do **not** reinstall the driver: the driver is already there. `/status` shows the raw `init_status` code |
| Reader panel says *"This browser has not been allowed to reach the reader yet"* | Nobody has granted the site local network access yet. This is not a bridge fault | Press **Check fingerprint station** (Workforce / Terminals / Staff Profile). The browser can only ask for the grant inside a click, which is why the panel cannot report the reader before the first press |
| HTTP 409 `BRIDGE_NOT_COLOCATED` from `api.jonehotels.com` | The hosted API was asked to capture — the reader is not there | Client must call `127.0.0.1:8787`; purge the stale frontend bundle |
| `net::ERR_CONNECTION_REFUSED` | Bridge not running | Start it (§4.3) |
| CSP `connect-src` violation | Hosting headers do not allowlist loopback | Deploy the repaired `_headers` / `vercel.json` |
| CORS error: *Permission was denied … `loopback` address space* | The BROWSER blocked Local Network Access. The bridge is not at fault. | Allow local network access for the site (see 5.4); confirm `Permissions-Policy: … local-network-access=(self)` is deployed. Do **not** reinstall the driver. |
| The bridge answers `/status` with `busy` | A capture already holds the reader | Wait for the current capture; the second attempt is refused, not queued |
| Reader replugged and `communication_failure` persists | Stale USB handle | Press **Re-initialise reader** (or restart the bridge). The bridge now re-opens the handle itself after `DPFPDD_DEVICE_*`; if the message still appears, run `--diagnose` |

---

## 8. What was changed in the 2026-11-20 repair (for the record)

* `frontend/js/biometrics.js`
  * **Local Network Access.** Chromium 138+ refuses a public page's `fetch()` to
    loopback until `local-network-access` is granted. The transport now asks for
    that permission under the operator's click (`requestLocalNetworkAccess()`),
    remembers a denial for the page's lifetime, and reports
    `BRIDGE_PERMISSION_BLOCKED` / `STATUS_PERMISSION_BLOCKED` with instructions
    instead of "start the bridge" — which cannot fix a permission denial.
  * **Device detection.** `isMobileBrowserContext()` now matches positive
    mobile/tablet platform tokens only (`Mobile` alone no longer appears in the
    test, because some Windows browsers carry it in their compatibility string,
    and a false positive disables capture on the reception PC). An operator
    override (`setWorkstationMode('desktop'|'mobile')`, shown as *Use reader on
    this device* / *This is a phone or tablet*) covers Windows tablets.
  * **`describeReader()`** returns one honest picture — `mobile`,
    `permission_blocked`, `bridge_unreachable`, `bridge_timeout`,
    `reader_not_ready`, `ready` — with `source` (`browser` / `bridge` /
    `policy`). The Workforce and Terminals pages render it as a reader badge,
    the detail, and the one action that can help.
* `frontend/dashboard/workforce.html`, `frontend/dashboard/terminals.html` —
  the reader panel and its controls (Check reader / Allow local network access /
  Re-initialise reader / workstation-mode override). Nothing about attendance
  authorisation changed: the server still enforces the desk key, registered
  address and supervised-fallback rules, and the fallback card still works from
  a phone or tablet when policy allows it.
* `frontend/_headers`, `frontend/vercel.json` and the `configuration/frontend/`
  copies — `Permissions-Policy` gains `local-network-access=(self)`. Without it
  the browser cannot grant the page loopback access at all.
* `frontend/sw.js` — `biometrics.js`, `operations.js`, `table-fit.js` and
  `runtime-config.js` are precached, so the staff shell cannot keep serving an
  older cached biometric module after a release.
* `backend/apps/staff_operations/services/fingerprint_bridge.py` — the
  server-side adapter's default port was 8765 while the workstation bridge (the
  only component that can own the USB reader) listens on 8787; corrected to
  8787 so the two agree. This adapter still requires
  `FINGERPRINT_BRIDGE_COLOCATED=1` and is not used by the desk pages.
* `workstation_bridge/fingerprint_bridge_service.py`
  * a stale USB handle (reader replugged / driver restarted) is now closed and
    re-opened once, inside the capture lock, instead of failing every later
    read until the bridge is restarted;
  * `/status` reports `busy` while a capture is in flight and never blocks
    behind it;
  * `/initialize` answers `busy` immediately instead of hanging for the whole
    capture timeout.

## 9. What was changed in the earlier 2026-11-19 repair (for the record)

* `frontend/js/api.js` — `resolveUrl()` normalises instead of concatenating: a
  URL that already carries a scheme (or already starts with the API base) passes
  through untouched, so `BASE + absolute URL` can no longer produce
  `https://api.jonehotels.comhttps//…`. Every relative endpoint behaves exactly
  as before.
* `frontend/js/config.js` — `FINGERPRINT_BRIDGE_URL` (default
  `http://127.0.0.1:8787`), validated as a loopback origin with no path, and
  re-validated after the runtime-config merge.
* `frontend/js/biometrics.js` — captures go to the bridge (`/status`,
  `/initialize`, `/capture`) with `credentials: "omit"`, `cache: "no-store"`,
  a bounded timeout and one recovery attempt; the legacy hosted-API transport is
  gone; errors are specific (`BRIDGE_UNREACHABLE`, `READER_BUSY`, …) and the
  supervised fallback is only ever an explicit, authorised action.
* `frontend/_headers`, `frontend/vercel.json` — `connect-src` allowlists
  `http://127.0.0.1:*` and `http://localhost:*` next to `'self'` and
  `https://api.jonehotels.com`. No wildcard, no remote plain-HTTP origin.
* `backend/apps/staff_operations/views_bridge.py` — the old server-side bridge
  routes now answer `409 BRIDGE_NOT_COLOCATED` unless the API actually runs on
  the reader's machine (`FINGERPRINT_BRIDGE_COLOCATED`). Authentication and
  capability checks still run first, so unauthorised callers see the same 401/403
  as before.
* `workstation_bridge/` — the bridge service itself, ISO 19794-2 encoding,
  `run_fingerprint_bridge.bat`, `install_service.ps1`, tests.

---

## 10. What was changed in the 2026-11-22 repair (for the record)

The client's error was the *unreachable* sentence, reported from a **mobile
browser** while the reader was on a Windows 11 workstation. Three separate
defects produced exactly that one sentence, so the investigation had to remove
all three before the message could be trusted:

1. **Nothing the page told the operator to do could be done.**
   * The message said *Start → J-ONE Fingerprint Bridge*. No program with that
     name existed: the installer registered a Scheduled Task and nothing else.
     `install_service.ps1` now also creates a **Start Menu shortcut** with that
     exact name, and accepts `-StartNow`.
   * `run_fingerprint_bridge.bat` now refuses to start silently: it names a
     missing Python, an already-running bridge (and opens `/status`), or the
     missing `dpfpdd.dll` / `dpfj.dll` before the window can just close.

2. **"Not allowed yet" and "not running" printed the same sentence.** A page
   that has not been granted Local Network Access cannot read the permission
   state at all (Chromium throws `SecurityError`), and `js/biometrics.js`
   treated that as `unsupported` → "start the bridge". It now reports
   `prompt` ("not asked yet") separately, and when a status read is refused it
   probes `/capture` with a deliberately invalid body — a POST preflight is the
   one request the browser has always allowed — so a running bridge is never
   reported as missing. No capture happens and nothing is sent to the API.

3. **The reader panel could not be used before a biometric action.**
   Workforce / Terminals / Staff Profile only asked for the grant on a capture
   click, so the panel that exists to report the reader could report nothing.
   Each page now has a **Check fingerprint station** control that runs the whole
   detection sequence (permission → `/health` → `/status` → `/initialize` only
   if needed) inside that click (`frontend/js/reader-check.js`).

4. **The driver/service distinction was missing.** `dpfpdd_init()` failing was
   reported as `driver_missing`, sending technicians to reinstall a driver that
   was already present. It is now `service_unavailable`, with its own sentence
   and its raw `init_status` code.

5. **On a phone the page now says so first.** The mobile check
   (`workstationMode()` / `WRONG_WORKSTATION_MESSAGE`) already existed and is
   unchanged: `127.0.0.1` on a phone is the phone. Fingerprint capture is only
   supported on the reception Windows workstation; every other permitted
   attendance action (including the supervised fallback, when policy allows it)
   still works from a phone, and the page says exactly that.

---

## 11. What was changed in the 2026-11-24 permanent repair

1. **Restored missing bridge files deleted in `dfde0ce`:** Restored `iso19794_2.py`
   (supporting both 24-byte ISO 19794-2:2005 and 26-byte ANSI 378-2004 headers),
   `run_fingerprint_bridge.bat`, `README.md`, and the unit/recovery test suites.
2. **Fixed DigitalPersona `dpfpdd.h` / `dpfj.h` C ABI defects:** Fixed `_DPFPDD_DEV_INFO`
   1024-byte `name` + `descr` layout, `DPFPDD_E_MORE_DATA` (`0x05BA000D`) buffer-grow
   retry in `dpfpdd_query_devices`, `dpfpdd_open(dev_name, &dev)` argument order,
   6-argument `dpfpdd_capture`, `500` DPI / `PIXEL_BUFFER` capture parameters,
   `DPFJ_FMD_ISO_19794_2_2005` (`0x01010001`), and `dpfpdd_init()` retry recovery.
3. **Windows-native USB plug-and-play & service detection:** Added `SetupAPI`/`CfgMgr32`
   (`VID_05BA`), hidden `WM_DEVICECHANGE` listener, `DpHost` SCM service check,
   automatic background initialization/reconnection, and `GET /events` SSE stream.
4. **Windowless startup & one-click installer:** Added `launch_bridge_hidden.vbs`
   and `install_workstation_bridge.bat` alongside updated `install_service.ps1`.
5. **Live frontend monitoring & capture button gating:** Updated `biometrics.js`,
   `reader-check.js`, `workforce.html`, `terminals.html`, `staff-profile.html`,
   and `sw.js` (`v1.1.36`) to stream live state transitions and disable capture
   controls until `"Fingerprint reader ready"`.

