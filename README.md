# J-ONE HOTEL & LODGE

Booking website, guest-facing Progressive Web App and staff management
dashboard for J-ONE Hotel & Lodge.

- Hotel: **J-ONE HOTEL & LODGE**, Plot 566 Mgbowo Street, off Ezike Street
- Phone: **+234 803 211 2874** · Email: **jonathanonu76@gmail.com**
- Currency: **NGN** · Timezone: **Africa/Lagos**

---

## What is in this repository

| Directory | What it is |
|---|---|
| [`backend/`](backend/) | Django + Django REST Framework JSON API. Reservations, stays, folios/finance, POS, guest services, operations, inventory, workforce, payments, reporting, and verified portal access. |
| [`frontend/`](frontend/) | Static HTML/CSS/vanilla-JS public site, staff dashboard, and verified-email guest portal. `build.py` handles release versioning and public chrome. |
| [`docs/`](docs/) | [Setup](docs/SETUP.md) and [usage](docs/USAGE.md) guides. |
| [`backend/docs/`](backend/docs/) | API reference, architecture notes, booking-flow spec and the OpenAPI 3 schema. |

The API and static frontend deploy independently to any compatible application
and static host, and communicate only over HTTPS/HTTP JSON. They remain one
application boundary: public, management, and guest-portal interfaces all use
the same authoritative Django API and database.

## How it fits together

Django is used **strictly as a JSON API**. The frontend never touches Django
templates, template context or the database; the backend never renders a page
the guest sees (its only HTML is transactional email).

The service worker is deliberately conservative and **never caches any `/api/`
response**. Availability, quotes, bookings, payments and authentication always
go straight to the backend, which stays the single source of truth. Offer
pricing in particular is always re-quoted server-side — the browser never
computes a discount it then trusts.

```
 Public browser          Staff browser          Verified guest browser
        │                     │                         │
        ▼                     ▼                         ▼
 frontend/          frontend/dashboard/         frontend/portal/
        │                     │                         │
        └─────────────────────┴──────► HTTPS JSON ◄─────┘
                                           │
                                  backend/ (Django REST API)
                                           │
                         ┌─────────────────┼─────────────────┐
                         ▼                 ▼                 ▼
                      MySQL            Paystack         Email provider
                                  (SMTP or HTTPS API)
```

## Quick start

Full instructions — prerequisites, environment variables, seed data,
deployment — are in **[docs/SETUP.md](docs/SETUP.md)**. The short version:

```bash
# API  → http://127.0.0.1:8000
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in the values you need
python manage.py migrate
python manage.py runserver

# Site → http://127.0.0.1:5500
cd frontend
python dev_server.py
```

Development defaults are safe: SQLite, console email (nothing leaves the
machine), Paystack test keys, no Redis required.

## Day-to-day operation

Taking bookings, handling payments and refunds, checking guests in (including
late and missed first-night arrivals), operating stays/folios/POS/service queues,
using inventory and workforce tools, and handling verified guest-portal access
are covered in **[docs/USAGE.md](docs/USAGE.md)**.

## Testing

```bash
cd backend  && python manage.py test        # Django test suite
cd backend  && python manage.py check
cd frontend && python validate.py           # HTML + JS syntax across all pages
cd frontend && node --test tests-js/        # frontend unit tests
```

## Releasing the frontend

`frontend/build.py` stamps the version into `version.json`, `js/version.js` and
the service-worker cache name, then releases public/dashboard assets and stamps
portal assets. Portal pages intentionally keep their memory-session bootstrap
without reload-capable PWA/update scripts. Bump on every deploy, or returning
visitors can keep an old cached shell:

```bash
cd frontend && python build.py --bump patch
```

## License

Proprietary. All rights reserved by J-ONE Hotel & Lodge.
