# Fiscus: instructions for Claude Code

Self-hosted personal finance for an Australian household, kept by one person
(D52). One Docker container on a homelab behind a reverse proxy with HTTPS,
installable as a PWA on phones. No external calls, no telemetry, no CDN.

**Status: v1 complete.** Passkeys (user verification required, D60), the
bootstrap link and CLI recovery links, other sessions ended by revoking a
passkey or using a recovery link (D56);
one person, with no users page, nothing shared and no password (D52);
accounts with balances typed in and kept by day (D39), moved by bills marked
paid, payslips and spending taken off the budget until the next is typed in
(D42, D45, D49); categories; bills with states, each paid from an account and
filed under a category, one that varies expecting the average of its last
three paid (D66); pay schedules and payslips (listed ten a page); the
calendar (the month by default, D54); home loan and offset (one loan opens
straight into it); forecasting with scenarios, its trend and what is free to
spend as the budget has it, lines turned off staying off (D46, D47, D50,
D51); the budget (each line its bills plus an estimate on top, D64; a line's
entries in its Edit panel);
bill import; the tax estimate, each job taxed on its own figures (D38); the
dashboard; ntfy reminders through ntfy.sh or a server of the person's own
(D53), payslips to put in among them; backups, downloaded from Settings;
payslip PDFs, single or in bulk, recording schedule paydays; casual pay;
savings goals inside accounts, out of the budget, one of them
the tax money, any of them saved from budget lines, one goal from several
(D32 to D34, D36); saved-up lines say if they will cover their bills in the
year ahead, overdue ones first, and what they hold is typed in or lined up
with them (D36, D40, D43, D44), a monthly line holding a yearly bill among
them (D63); budget lines in groups and in an order set by
hand (D35); bottom tabs the person chooses, every screen in a menu that
slides in, and pages sliding between screens (D67). Since D25 the budget is kept by hand (spending taken off,
balances typed in); the transactions, statement import, rules and matching
screens went in D37 and Square sync in D55. Bills marked paid, payslips and
budget spends still write transactions; rows those features left stay. Next
steps are in `docs/10-decisions.md` §3.

## Read before building

1. `docs/01-product.md`: what it does
2. `docs/02-architecture.md`: stack, layout and why
3. `docs/03-data-model.md`: schema
4. `docs/05-forecast.md`: before touching the forecast, loans or bill dates
5. `docs/10-decisions.md`: what is locked

`docs/10` is the authority on decisions. If something here contradicts it,
doc 10 wins.

## Hard rules

- **Ownership is enforced on every query.** Per-user reads go through
  `fiscus/services/access.py` (`owned`, `get_owned`, `maybe_owned`). A row that
  belongs to someone else is a **404, never a 403**, so ids cannot be probed.
  One person keeps Fiscus (D52), but a person left from an earlier version
  still sees only their own. Bills are owned rows like any other, not shared.
  Any new route that takes an id gets a line in `tests/test_isolation.py`.
- **A route takes the database as `db: Session = DB`** (`fiscus/web.py`).
  It commits before the response goes out, so a failed commit is the error
  page, and an unsafe request holds SQLite's write lock from its start, so
  what it checks still holds when it writes (D59). Never `Depends(get_db)`:
  two declarations would be two sessions.
- **Money is integer cents. Rates are decimal strings.** Never a float in a
  stored value or a calculation. `fiscus/money.py` parses and formats.
- **"Today" comes from the clock**, `ctx.clock.today()`, in
  `FISCUS_TIMEZONE`. Nothing calls `date.today()` or `datetime.now()`.
- **One recurrence expansion.** `fiscus/domain/recurrence.py` decides when
  anything recurring happens. The calendar, forecast, bill status, budget and
  reminders all call it, so they cannot disagree about a due date.
- **Domain logic in `fiscus/domain/` does no I/O.** It takes values and
  returns values, and carries most of the tests. Services fetch, domain
  computes, routes render.
- **Scenarios never write.** `apply_scenario` returns a changed copy of the
  forecast input. `tests/test_isolation.py` checksums every finance table
  across a scenario run.
- **The service worker caches the shell and nothing else**: `/static/*` and
  `/offline`. Pages, fragments, JSON and downloads are network only, and every
  non-static response is sent `Cache-Control: no-store`. Offline shows the
  offline screen, never stale figures. `tests/test_pwa_ops.py` and
  `tests/test_browser.py` hold this.
- **The forecast cache is keyed on `users.data_version`.** A flush that
  touches an `Owned` row bumps its user in `services/versioning.py`. Bulk
  `DELETE`/`UPDATE` statements skip the ORM, so pair them with an ORM change
  to an owned row.
- **The audit log is append-only**, enforced by SQLite triggers. Logins,
  failed logins, every credential change, setup links, backups and CLI
  actions. No page shows it since D52.
- **Passkeys refuse to run unless the origin is HTTPS** (localhost excepted).
  `Settings.passkeys_allowed` decides; the routes answer 503 otherwise.
- **A user cannot revoke their last passkey.** There is no password sign-in
  (D52), and `users` has no password columns since migration 0026.
- **Setup links are single use, time-limited and stored as hashes.** A new
  link for a person revokes their previous one. Bootstrap and recovery are the
  same mechanism; only the bootstrap print and the CLI issue one.
- **Break-glass recovery is a CLI** (`python -m fiscus.cli recovery-link`),
  never an HTTP endpoint without a session.
- **CSP is strict**: `script-src 'self'`, `style-src 'self'`. No inline
  scripts, no `style=""` attributes (a test fails on one), no `eval` (so no
  htmx `js:` values). Data for scripts goes in a `data-*` attribute as JSON,
  autoescaped. Not a `<script type="application/json">`: htmx drops every
  script tag when it swaps a boosted page.
- **Everything frontend is vendored** in `fiscus/static/vendor` with its
  licence and a line in its README. Nothing loads from another origin.
- **No external calls** except one, off by default and per user: the ntfy
  server the user names (ntfy.sh or their own, D53). It refuses loopback,
  link-local (cloud metadata) and unspecified addresses, and allows public
  servers and private LAN ranges, where a homelab ntfy lives. Square sync
  went in D55.
- **Every tax figure is labelled an estimate.** Rates live in
  `config/tax/<year>.yaml`, one per financial year, checked each July.
- Australian English in all user-facing strings. No em dashes in prose.
- **Source-available, not open source.** PolyForm Noncommercial 1.0.0,
  copyright Fenici. Do not relicense or add a dependency whose licence would
  conflict.

## Workflow

- Never commit to main. Branch per change with a descriptive name.
- Tests with the feature, not after.
- Commit, push, open a PR, then watch CI until it is green.
- A merge to main is pushed as the `beta` image. A release changes `VERSION`
  (the only place the version is set), then a GitHub release publishes that
  version and `latest` (D65).
- Verify web changes in a real browser (Playwright, Chromium) and read the
  console before calling it done. `tests/test_browser.py` is the pattern.
- Verify container changes by running the built image (`scripts/smoke.sh`),
  not by trusting a green check.
- Comments and commit messages short and factual.

## Layout

```
fiscus/
  config.py clock.py money.py db.py models.py web.py app.py jobs.py cli.py
  nav.py      the screens: sidebar, menu and bottom tabs (D67)
  domain/     recurrence, splits (migrations only), uploads, loan, forecast,
              tax, bill_import, budget_import, pdftext, payslip, pay, goals,
              saving, arrange.
              Pure, no I/O.
  services/   access (ownership), auth (sessions, links, WebAuthn), users,
              ledger, pay, bills, forecasting, loans, taxes, budgeting,
              budget_import, budget_groups, bill_import, calendar_events,
              reminders, ntfy, backup, versioning, audit, goals
  routes/     one module per screen
  templates/  Jinja2 + HTMX
  static/     css, js (app, webauthn, calendar, forecast-chart, sw.src.js),
              vendor, fonts, icons
  migrations/ Alembic
config/tax/   one YAML per financial year
VERSION       the version, set nowhere else (D65)
tests/        pytest; authenticator.py is a software passkey
docs/         design notes (01 to 10), guide.md for running it, screenshots/
scripts/      smoke.sh, generate_icons.py, icon.svg, screenshots.py (README
              screenshots from a made-up household)
```

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.1 + Alembic on SQLite (WAL), Jinja2 +
HTMX, py_webauthn, httpx (ntfy only), openpyxl + defusedxml
(budget XLSX import), pdfminer.six (payslip PDFs). uPlot for the forecast
chart, vue-cal's stylesheet for the calendar. pytest, ruff, mypy --strict,
djlint, Playwright.

## Verifying

```bash
uv venv -p 3.12 .venv && . .venv/bin/activate
uv pip install --require-hashes -r requirements-dev.lock
ruff format --check fiscus tests scripts && mypy && ruff check fiscus tests scripts
djlint fiscus/templates --lint
pytest                                   # includes the Chromium test
docker build -t fiscus:local . && scripts/smoke.sh fiscus:local
```

Run locally without Docker:

```bash
FISCUS_DATA_DIR=./data FISCUS_BASE_URL=http://localhost:8000 python -m fiscus
```

## Watch out for

- **One uvicorn worker, always.** SQLite, the in-memory rate limiter and the
  background jobs assume a single process. `python -m fiscus` enforces it.
- **Route order matters** for literal paths beside `{id}` paths
  (`/payslips/read` before `/payslips/{slip_id}`; the `bill_import` router before
  `bills`, for `/bills/import` beside `/bills/{bill_id}`). FastAPI matches the
  first, then fails validation.
- **The calendar uses vue-cal's class names on server-rendered markup.** If a
  layout looks wrong, check `static/vendor/vuecal-4.10.2.css` before writing
  new CSS: a vue-cal rule is probably applying (`.vuecal--no-time
  .vuecal__bg` is `display: flex`, which once laid the agenda out sideways).
- **The service worker only reloads the page after the person accepts an
  update.** The first install also fires `controllerchange`; reloading then
  cut across a passkey registration once.
- **iOS installed PWAs need a user gesture for WebAuthn.** Every
  `navigator.credentials` call starts from a click, except the conditional
  (autofill) request on the login page, which iOS allows.
- **Pages slide only on a boosted GET to another screen** (`app.js`, D67):
  it sets `transition:true` on that one swap. Saves, sheets and fragments
  never slide. `.topbar`, `.bottom-nav` and `.sidebar` have a
  `view-transition-name` so they stay put while the page moves; a new fixed
  bar needs one too, or it slides with the page.
- **iOS has no `beforeinstallprompt`.** Install is a Share-menu instruction
  (`docs/guide.md`). File uploads use the file picker, which works in
  standalone mode.
- **A balance is the last one typed in plus transactions dated after its
  day, and those dated its day put in after it was typed** (`ledger.balances`,
  D42, D45). Anything else that moves money writes a transaction linked to its
  row (`ledger.follow`), never the opening balance, and one dated before a
  typed balance, or on its day and put in before it, changes nothing. Tests
  that type a balance and put pay in on the same day move the clock between.
- **A test that patches the clock** uses `env.clock.set(...)`; the default is
  10:00 on Thursday 1 October 2026 in Perth.
- Templates see `user` as a `UserView` snapshot, not the ORM object, so an
  error page after a rollback can still render.
- **One person (D52).** No users page, invites or audit view; the admin flag
  only gates backups in Settings. Bills carry their own `account_id` and
  `category_id`; `bill_shares` is gone (migration 0025 gave each person their
  share as a bill of their own). `domain/splits.py` stays only because
  migrations 0003, 0009 and 0025 work out shares with it. Anyone left from
  before is removed with `python -m fiscus.cli remove-user`.
