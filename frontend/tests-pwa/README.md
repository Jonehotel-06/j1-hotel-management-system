# PWA verification suite

Browser-driven checks (Playwright + headless Chromium) for the J-ONE PWA. They exercise a **real**
service worker, real Cache Storage and real offline mode — not mocks.

```bash
pip install playwright && python3 -m playwright install --with-deps chromium

# serve the frontend first (localhost is a secure context, so the SW registers)
cd frontend && python3 dev_server.py 8080 http://127.0.0.1:8000

cd tests-pwa
python3 test_pwa.py            # manifest/icons/SW scope/caching/offline on every page
python3 test_regression.py     # existing site + auth guard + no token leakage
python3 test_update.py         # version bump: new SW waits, old caches purged
python3 test_ios.py            # iOS Add-to-Home-Screen vs Android install prompt
python3 test_a11y.py           # accessibility of the PWA-specific UI (axe-core)
python3 test_paystack.py       # payment traffic is never intercepted or cached
python3 test_installability.py # Chrome's own manifest parse (CDP)
python3 test_sticky.py         # sticky blurred topbar/header, both themes
python3 test_stickyhdr.py      # header/topbar stays pinned while scrolling (all pages)
python3 test_multi_room_ui.py  # multi-room quantity, payment auto-continue, mobile cards
```

`test_multi_room_ui.py` stubs the booking APIs at the network boundary (so it creates no real
inventory) and guards three regressions: the room quantity surviving selection → summary → draft →
refresh → back-navigation with a server-quoted total; a backend-VERIFIED payment auto-continuing to
Step 6 while failed/pending/abandoned/errored verifications never navigate and never claim success;
and the room cards fitting 320–430px with no horizontal overflow, desktop layout unchanged.

The safety-critical assertions are in `test_pwa.py` and `test_paystack.py`: no `/api/` or
`/dashboard/` entry may appear in any cache, offline booking/payment must fail rather than return a
cached "success", and the offline fallback must be `offline.html` (never `index.html`).

`test_sticky.py` stubs a staff session in `localStorage`/`sessionStorage` to get past the auth
guard. Note the dashboard scrolls inside `.dash-main`, **not** the window, so it asserts against
that container.

`test_stickyhdr.py` guards a subtle regression: `overflow-x:hidden` on `html`/`body` makes the body
a scroll container, which silently breaks `position:sticky` for every descendant. `audit-fixes.css`
uses `overflow-x:clip` instead. If the header ever starts scrolling away again, check that rule
first.

`test_update.py` temporarily edits `../sw.js` to simulate a deploy and restores it in a `finally`
block.
