# Fiscus: instructions for Claude Code

Self-hosted, multi-user personal finance for an Australian household. One
Docker container on a homelab behind a reverse proxy with HTTPS, installable as
a PWA on phones. No external calls, no telemetry, no CDN.

**Status: v1 complete.** Passkeys, setup links and recovery, users and admin;
accounts with balances typed in and kept by day (D39), moved by bills marked
paid, payslips and spending taken off the budget until the next is typed in
(D42, D45, D49); categories; bills with
shares and states; pay schedules and payslips; the calendar; home loan and
offset; forecasting with scenarios, its trend and what is free to spend
after goals, lines turned off staying off (D46, D47, D50); the budget (bills paid out of it); bill
import; the tax estimate, each job taxed on its own figures (D38); the
dashboard; ntfy reminders, payslips to put in among them; backups; payslip
PDFs, single or in bulk, recording schedule paydays; casual pay; Square sync;
savings goals inside accounts, out of the budget, one of them the tax money,
any of them saved from budget lines, one goal from several (D32 to D34, D36);
saved-up lines say if they will cover their bills in the year ahead, overdue
ones first, and what they hold is typed in or lined up with them (D36, D40,
D43, D44); budget lines in groups and in an order set by hand (D35). Since
D25 the budget is kept by hand (spending taken off, balances typed in); the
transactions, statement import, rules and matching screens were removed in
D37 (their tables stay; Square, bills marked paid, payslips and budget
spends write to them; Square's page is unlinked). Next steps are in `docs/10-decisions.md` §3.

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
  Bills are shared and are reached only through the caller's `BillShare`
  (`visible_bill`), and only the owner edits one (`owned_bill`). Any new route
  that takes an id gets a line in `tests/test_isolation.py`.
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
  touches an `Owned` row bumps its user (and every participant of a touched
  bill) in `services/versioning.py`. Bulk `DELETE`/`UPDATE` statements skip
  the ORM, so pair them with an ORM change to an owned row, as Square's sync
  does after clearing its invoices.
- **The audit log is append-only**, enforced by SQLite triggers. Logins,
  failed logins, every credential change, setup links and admin actions.
- **Passkeys refuse to run unless the origin is HTTPS** (localhost excepted).
  `Settings.passkeys_allowed` decides; the routes answer 503 otherwise.
- **A user cannot revoke their last passkey** unless an admin-enabled password
  is set. Password sign-in is off for everyone by default.
- **Setup links are single use, time-limited and stored as hashes.** A new
  link for a person revokes their previous one; disabling a person revokes
  theirs. Bootstrap, onboarding and recovery are the same mechanism.
- **Break-glass recovery is a CLI** (`python -m fiscus.cli recovery-link`),
  never an HTTP endpoint without a session.
- **CSP is strict**: `script-src 'self'`, `style-src 'self'`. No inline
  scripts, no `style=""` attributes (a test fails on one), no `eval` (so no
  htmx `js:` values). Data for scripts goes in a `data-*` attribute as JSON,
  autoescaped. Not a `<script type="application/json">`: htmx drops every
  script tag when it swaps a boosted page.
- **Everything frontend is vendored** in `fiscus/static/vendor` with its
  licence and a line in its README. Nothing loads from another origin.
- **No external calls** except two, both off by default and per user: the
  user's own ntfy server, and Square's API. ntfy refuses loopback, link-local
  (cloud metadata) and unspecified addresses, and allows private LAN ranges,
  because that is where a homelab ntfy lives. Square only ever goes to the
  fixed `connect.squareup.com` host (no address from a form), reads only, and
  tests never reach it (`conftest.no_square`).
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
- Verify web changes in a real browser (Playwright, Chromium) and read the
  console before calling it done. `tests/test_browser.py` is the pattern.
- Verify container changes by running the built image (`scripts/smoke.sh`),
  not by trusting a green check.
- Comments and commit messages short and factual.

## Layout

```
fiscus/
  config.py clock.py money.py db.py models.py web.py app.py jobs.py cli.py
  domain/     recurrence, splits, uploads, rules, matching, loan, forecast,
              tax, bill_import, budget_import, pdftext, payslip, pay,
              square, goals, saving, arrange.
              Pure, no I/O.
  services/   access (ownership), auth (sessions, links, WebAuthn), users,
              ledger, pay, bills, forecasting, loans, taxes, budgeting,
              budget_import, budget_groups, bill_import, calendar_events,
              reminders, ntfy, square, backup, versioning, audit, goals
  routes/     one module per screen
  templates/  Jinja2 + HTMX
  static/     css, js (app, webauthn, calendar, forecast-chart, sw.src.js),
              vendor, fonts, icons
  migrations/ Alembic
config/tax/   one YAML per financial year
tests/        pytest; authenticator.py is a software passkey
scripts/      smoke.sh, generate_icons.py, icon.svg
```

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.1 + Alembic on SQLite (WAL), Jinja2 +
HTMX, py_webauthn, argon2-cffi, httpx (ntfy and Square only), openpyxl + defusedxml
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
- **iOS has no `beforeinstallprompt`.** Install is a Share-menu instruction
  (README). File uploads use the file picker, which works in standalone mode.
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
