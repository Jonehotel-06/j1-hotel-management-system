# Fingerprint attendance — reader integration guide

**Physical-device acceptance status: BLOCKED.** No fingerprint reader is
attached to any environment used to build this work. Everything below is
verified against a **simulated (mocked) reader** driving the same software
boundary a real device uses. Do not cite the mock results as proof that
physical hardware was tested.

## Architecture

```
 Fingerprint reader (USB/serial)          J-ONE backend
 ┌──────────────────────────┐             ┌────────────────────────────────────┐
 │ Vendor SDK / local       │  HTTPS JSON │ services/fingerprint_device.py     │
 │ capture agent on the     │ ──────────► │  register_device / report_status   │
 │ reception workstation    │             │  submit_device_capture             │
 │  - captures minutiae     │             │        │                           │
 │  - NEVER stores images   │             │        ▼                           │
 └──────────────────────────┘             │ services/fingerprint_service.py    │
                                          │  ISO/IEC 19794-2 minutiae match    │
                                          │  (templates only — no raw images)  │
                                          │        │                           │
                                          │        ▼                           │
                                          │ services/staff_operations_service  │
                                          │  clock_attendance (state machine)  │
                                          └────────────────────────────────────┘
```

Supported device class: **fingerprint reader** (minutiae capture). Facial
recognition is a separate, optional module and is not the required hardware.

## Device contract (`services/fingerprint_device.py`)

| Call | Purpose |
|---|---|
| `register_device(serial, label, workstation, actor)` | Bind a reader to an authorised workstation (`attendance.manage` required). |
| `report_device_status(serial, status, actor, note)` | Heartbeat / fault reporting (`ACTIVE` / `DISABLED` / `FAULT`). |
| `submit_device_capture(...)` | Submit one capture event. |

A capture event is:

```json
{
  "serial_number": "READER-001",
  "event_nonce": "unique-per-capture-id",
  "captured_at": "2026-10-11T09:00:00+01:00",
  "action": "CLOCK_IN",
  "staff": "<identified staff>",
  "probe": [{"x": 100, "y": 150, "angle": 30, "type": "ending", "quality": 90}, ...]
}
```

Enforced security properties:

* Only **ACTIVE** devices bound to a registered workstation may submit.
* **Replay protection** — per-device `event_nonce` uniqueness; a replayed
  event is refused and only the original is stored.
* **Freshness window** — `captured_at` must be within ±10 minutes of server
  time; captured-but-delayed events cannot be replayed later.
* **Identity comes from the biometric match** — the agent never asserts who
  the employee is without a probe matching an ACTIVE approved template.
* **Duplicate clock-ins** are refused by the attendance state machine (clock
  out first); both attempts remain in the audit log.
* **No raw fingerprint images** are accepted or stored anywhere — only
  validated ISO/IEC 19794-2 minutiae sets are processed and outcomes stored.
* Every attempt is written to the append-only `FingerprintDeviceEvent` log
  (device, staff, action, timestamps, outcome, score, attendance reference).

## Enrolment workflow (unchanged)

1. Staff member submits a minutiae template (`fingerprint/enrollment/`).
2. Supervisor approves (`fingerprint/enrollments/{id}/approve/`) → ACTIVE.
3. Verification and clock-in use the approved template only.
4. Templates expire (retention policy) and are cleared on expiry.

## Mock integration tests

`backend/tests/test_fingerprint_device_mock.py` (8 tests) drives this exact
boundary with a simulated reader: verified clock-in, replay refusal, stale
timestamp refusal, unknown/disabled device refusal, non-match audit-only,
duplicate clock-in refusal, registration RBAC, append-only event log.

Run: `python manage.py test tests.test_fingerprint_device_mock tests.test_fingerprint_biometrics`

## Physical-device acceptance — BLOCKED checklist

To close the hardware item:

1. Attach a supported fingerprint reader to the reception workstation.
2. Implement/attach the vendor SDK or local capture agent against the device
   contract above (the agent speaks HTTPS JSON to this API from the
   workstation).
3. Enrol one consented staff member; clock in and out twice (morning/evening).
4. Confirm: attendance rows, `FingerprintDeviceEvent` rows, audit entries, the
   replay refusal on a re-sent nonce, and supervisor device-status visibility.
5. Record the device make/model/firmware in `FingerprintDevice` and update the
   final QA report section F with the physical result.

Until steps 1–5 are executed on real hardware, the physical-device status
remains **BLOCKED** — not PASS.
