# 2. Architecture

## Stack and why

| Choice | Why |
| --- | --- |
| Python 3.12, FastAPI | Small, typed, fast enough for a household; the brief asked for it. |
| SQLite (WAL) via SQLAlchemy 2.1, Alembic | One file on one volume. A household's data is tiny. The online backup API gives safe hot backups. |
| Jinja2 + HTMX | Server-rendered pages that work without a build step; HTMX for in-place updates (calendar paging, recategorise, event sheets). |
| Vanilla JS only where needed | WebAuthn, the calendar's gestures, the forecast chart, the service worker. About 600 lines in total. |
| uPlot (vendored) | 45 KB, fast with 730 points per series, overlays a scenario cleanly. |
| vue-cal 4 stylesheet (vendored) | CareLane's calendar is vue-cal. Rendering vue-cal's DOM on the server and reusing its CSS reproduces the layout exactly without Vue. |
| py_webauthn | Maintained, does the verification, leaves storage to us. |
| argon2-cffi | argon2id for the optional password. |
| One uvicorn worker | SQLite, in-memory rate limits and in-process jobs all assume one process. |

## Request flow

```
browser ─HTTPS─ NPMplus ─HTTP─ uvicorn (proxy headers from trusted IPs only)
                                  │
                         middleware: HEAD→GET, CSRF cookie, security headers,
                                     Cache-Control: no-store on everything not /static
                                  │
                         dependency: csrf_guard (every unsafe method)
                                  │
                         route ─ current_user (session cookie → sessions table)
                           │
                           ├─ services/* (fetch, always through access.owned)
                           │     └─ domain/* (pure calculation)
                           └─ render(template) or redirect
```

## Layers

- `domain/`: pure functions and dataclasses. Recurrence, splits, CSV parsing
  and dedup, rules, bill matching, loan maths, the forecast engine and
  scenarios, tax, bill import parsing and diffs. No database, no clock.
- `services/`: database work for one concern each. Every per-user query goes
  through `access.owned` / `get_owned`.
- `routes/`: one module per screen. Thin: parse the form, call a service,
  render.
- `jobs.py`: an asyncio loop inside the app, ticking once a minute. Hourly
  housekeeping and reminders; the nightly backup at `FISCUS_BACKUP_HOUR`.

## Frontend

`base.html` loads every script once with `defer`. Pages are boosted by HTMX,
so scripts initialise through `htmx.onLoad`. Nothing inline: the CSP is
`script-src 'self'; style-src 'self'`.

The service worker source is `static/js/sw.src.js`; `/sw.js` prepends a
version (app version, build hash and a digest of every static file) and the
shell list, so any asset change produces a new worker and the update prompt.

## Deployment

One image, one volume (`/data`: `fiscus.db`, `uploads/`, `backups/`), port
8000. Migrations run at start-up. The image runs as uid 10001.
