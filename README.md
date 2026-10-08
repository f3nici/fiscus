# Fiscus

Self-hosted personal finance for an Australian household. Each person's
accounts, budget, forecast and tax stay their own; bills can be shared. Runs
as one Docker container on your homelab, signs in with passkeys, and installs
on your phone as an app. Nothing leaves your network.

- Account balances typed in as you go, with the history kept
- A budget kept by hand: take spending off as you go, with saved-up lines for
  yearly and quarterly costs and savings goals inside your accounts
- Bills, shared and split, marked paid in a tap
- Payslips, typed in or read from their PDFs, with a reminder for any not in
- A calendar of bills and paydays with your projected balance on every day
- Home loan with offset: interest saved and payoff date
- Forecast 1 to 24 months ahead, with what-if scenarios
- An Australian tax estimate against your set-aside
- Optional reminders through your own ntfy server
- Optional sync from a Square seller account (payments, fees, payouts, unpaid invoices)

Every forecast and tax figure is an estimate from your own data, not
financial or tax advice.

## Contents

- [Setup](#setup)
- [First run: the bootstrap link](#first-run-the-bootstrap-link)
- [Adding people and passkeys](#adding-people-and-passkeys)
- [Installing the app on iOS and Android](#installing-the-app-on-ios-and-android)
- [Budget import from a spreadsheet](#budget-import-from-a-spreadsheet)
- [Importing bills from a file](#importing-bills-from-a-file)
- [Reminders with ntfy](#reminders-with-ntfy)
- [Square](#square)
- [Backup and restore](#backup-and-restore)
- [Configuration](#configuration)
- [Tax tables](#tax-tables)
- [Developing](#developing)
- [Licence](#licence)

## Setup

You need Docker, a domain or subdomain (for example `fiscus.home.example.com`)
and a reverse proxy that serves it over HTTPS. **HTTPS is not optional:**
browsers only allow passkeys and service workers on HTTPS (or `localhost`), and
Fiscus refuses to start a passkey ceremony otherwise.

### With Dockge

1. Create a new stack called `fiscus`.
2. Paste the contents of [`docker-compose.yml`](docker-compose.yml).
3. In the stack's `.env`, set at least:

   ```env
   FISCUS_SECRET_KEY=<output of: openssl rand -hex 32>
   FISCUS_BASE_URL=https://fiscus.home.example.com
   ```

   See [`.env.example`](.env.example) for the rest.
4. Deploy.

### With plain Docker Compose

```bash
mkdir fiscus && cd fiscus
curl -O https://raw.githubusercontent.com/f3nici/fiscus/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/f3nici/fiscus/main/.env.example
# edit .env: FISCUS_SECRET_KEY and FISCUS_BASE_URL
docker compose up -d
```

To build from source instead of pulling the published image:

```bash
git clone https://github.com/f3nici/fiscus && cd fiscus
cp .env.example .env   # and edit it
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Reverse proxy (NPMplus)

Add a proxy host for your domain pointing at the Fiscus container on port
8000, with an SSL certificate and "Force SSL" on. Websockets are not needed.

Fiscus only believes `X-Forwarded-For` from addresses in
`FISCUS_TRUSTED_PROXIES` (default `172.16.0.0/12`, the usual Docker
networks). If NPMplus reaches Fiscus from elsewhere, set it to NPMplus's
address as Fiscus sees it. That keeps sign-in rate limits and the audit log
accurate.

Check it is up:

```bash
curl https://fiscus.home.example.com/readyz
# {"status":"ready","version":"0.1.0","build":"...","checks":{"database":true,"migrations":true,"data_dir_writable":true}}
```

## First run: the bootstrap link

On first start, with no users, Fiscus prints a one-time link to its logs:

```
  No users yet. Register the first admin passkey at:

    https://fiscus.home.example.com/setup/Xa1...

  This bootstrap link expires 2026-10-03T00:02 UTC and is replaced on every restart
  until somebody uses it.
```

Find it with `docker compose logs fiscus` (or the Dockge log view). Open it
**on the device you want to sign in with** (usually your phone), pick a
username, and create a passkey. That account is the admin. The link stops
working as soon as it is used, and a new one is printed on each restart until
someone does.

If you lose it before using it, restart the container or run:

```bash
docker compose exec fiscus python -m fiscus.cli bootstrap
```

## Adding people and passkeys

**Add a person** (admin): Admin, Users, "Add a user". Fiscus shows a one-time
setup link, valid for 48 hours. Send it to them privately; they open it on
their phone and create their passkey. Their finances are separate from yours.

**Add another device** (anyone): Settings, Passkeys, "Add a passkey on this
device", on the device you are signed in on. Name each one (phone, laptop,
security key) so you can tell them apart; Settings shows when each was last
used. To add a device you are not holding, sign in on that device with a
recovery link from the admin.

**Revoke a passkey**: Settings, Passkeys, Manage, Revoke. You cannot revoke
your last one unless you also have a password set.

**Lost every passkey**: the admin presses "Recovery link" next to your name
and sends it to you. Open it on your new device, create a passkey, then
revoke the lost ones in Settings. If the admin is the one locked out:

```bash
docker compose exec fiscus python -m fiscus.cli recovery-link <username>
```

**Password fallback**: off for everyone by default. An admin can allow it per
person (Admin, Users, Password); that person then sets one in Settings.

**Disable a person**: Admin, Users, Disable. They are signed out everywhere
and any pending link stops working. Nothing of theirs is deleted.

## Installing the app on iOS and Android

Fiscus is a Progressive Web App: install it from the browser and it opens
full-screen with a bottom navigation bar, like a native app. The app shell is
cached; your figures never are. Offline, it shows an offline screen rather
than old numbers.

**iPhone and iPad (Safari)**

1. Open your Fiscus address in **Safari** and sign in.
2. Tap the Share button, then **Add to Home Screen**, then **Add**.
3. Open Fiscus from the home screen. Your passkey works inside the app
   (Face ID or Touch ID). iOS 16 or later is needed for passkeys.

File uploads (budget spreadsheets, bill files, payslip PDFs) use the file
picker, which opens the Files app, including iCloud Drive and Downloads.

**Android (Chrome)**

1. Open your Fiscus address in **Chrome** and sign in.
2. Tap the menu (three dots), then **Install app** (or **Add to Home
   screen**).
3. Open Fiscus from the home screen or app drawer.

**Desktop (Chrome, Edge)**: the install icon in the address bar.

**Updates**: when a new version is deployed the app shows "A new version of
Fiscus is ready" with a Reload button.

## Budget import from a spreadsheet

If you already keep a household budget in Excel, bring it in: Budget, **Import
spreadsheet**, choose the `.xlsx` (or a CSV), Upload and preview.

Fiscus looks for a header row with **Item**, **Amount** and **Frequency**
columns (Notes is optional) on any sheet, so a workbook with an assumptions tab
in front of the budget tab is fine. Formulas are read through the values Excel
last calculated, so save the file in Excel before uploading. Rows of their own
like `HOME & PROPERTY` are treated as section headings, rows starting with
"Total" are skipped, and each line is turned into a monthly amount (weekly x 52
/ 12, annual / 12, daily x 365 / 12).

The preview lists every line. Untick any you do not want, change the category
a line goes to (lines sharing a category are added together), or adjust its
monthly figure. Some lines start unticked, with the reason shown:

- one-off costs (stamp duty and other settlement costs), which are not monthly;
- the mortgage repayment when you have a home loan in Fiscus, which tracks it;
- lines whose amount or frequency could not be read.

Choose whether imported amounts replace or add to budgets you already have,
and whether the forecast should use them instead of your 3-month averages.
Nothing is saved until you press Import. An imported amount is the
category's whole budget: bills already in that category count towards it, not
on top. Anything that should appear on the calendar and in the forecast is a
bill; add it on the Bills page (or import it, below). The Budget page also has
a CSV template.

## Importing bills from a file

Every recurring cost lives in one place, **Bills**: rent, rates, insurance,
strata, water, power, subscriptions. Each bill is on the calendar, in the
forecast, and counts towards its category's budget at a monthly rate (your
share, weekly x 52 / 12, quarterly / 3 and so on). You never enter a cost twice.

Type bills in one at a time, or Bills, **Import**, and upload a CSV or JSON
file. Download a template from that page in either format.

**CSV**

```csv
name,amount,frequency,category,due_date,shared_with,split_percent
Council rates,520.00,quarterly,Housing,2026-11-15,,
Home insurance,1850.00,yearly,Insurance,2027-03-01,partner,50
Water,310.00,quarterly,Utilities,,partner,50
```

**JSON**

```json
{
  "bills": [
    {"name": "Council rates", "amount": "520.00", "frequency": "quarterly", "category": "Housing", "due_date": "2026-11-15"},
    {"name": "Home insurance", "amount": "1850.00", "frequency": "yearly", "category": "Insurance", "due_date": "2027-03-01", "shared_with": "partner", "split_percent": 50}
  ]
}
```

| Field | Required | Notes |
| --- | --- | --- |
| name | yes | How a re-import matches a bill you own |
| amount | yes | The whole bill each time it is due |
| frequency | yes | weekly, fortnightly, monthly, bimonthly (every 2 months), quarterly, yearly or one-off |
| category | no | Created if it does not exist |
| due_date | no (yes for one-off) | YYYY-MM-DD, the next time it is due; left out, an existing bill keeps its own |
| shared_with | no | Another Fiscus user's username |
| split_percent | no | Their share, default 50 |

A CSV with other column names gets a mapping step. Every import shows a
preview first, compared with the bills you own by name: what will be added,
what will change, and what is not in the file. Bills not in the file are left
alone unless you tick the box, and then they are stopped rather than deleted,
so their payment history stays. A bill split in a way a file cannot describe
(fixed amounts, or more than two people) keeps its split. A housing costs file
from an earlier version imports as it is.

**Upgrading from a version with a Housing costs page**: on first start the
housing items go and the bills they had already made stay, unchanged. Each
budget keeps the same total; Fiscus stores only the part above that category's
bills. To keep a copy from just before, run `python -m fiscus.cli backup`
first (see [Backup and restore](#backup-and-restore)).

## Reminders with ntfy

Fiscus can push reminders to your phone through your own
[ntfy](https://docs.ntfy.sh) server. Off by default, set per person, and no
third-party push service is involved.

1. Run ntfy on your homelab (for example `binwiederhier/ntfy` in Dockge) and
   install the ntfy app on your phone, pointed at your server.
2. Subscribe the app to a topic, for example `fiscus-alex`. Use an access
   token if your server requires one.
3. In Fiscus: Settings, Reminders. Tick "Send me reminders", enter the server
   (`http://192.168.1.20:8080` or `https://ntfy.home.example.com`), the topic
   and the token, choose what to hear about, Save, then "Send a test".

You get one push per bill due within your chosen number of days, one per
overdue bill, one per low-balance warning, and one per payday that passed
without a payslip (the day after, for the last week's paydays), each once, at
the hour you choose. Messages contain the bill or employer name, date and
amount only. Private LAN
addresses are allowed; the container's own loopback and link-local addresses
are not.

## Square

If you take payments or send invoices through Square, Fiscus can pull them in.
It only reads, only talks to Square's API, and is off until you connect it.

1. In the [Square Developer Console](https://developer.squareup.com/apps),
   create an application (any name), open **Credentials**, switch to
   **Production** and copy the access token.
2. In Fiscus, add an account for the Square balance (for example "Square",
   opening balance 0), and categories: one marked income and business for
   payments, one marked business for fees, and a transfer category for payouts.
3. Open `/square` (it has been out of the menus since the budget became
   hand-kept), tick **Sync from Square**,
   paste the token, choose the account, categories and the date to fetch
   from, Save, then **Sync now**.

After that it syncs every six hours. Each payment is income at its full
amount, with Square's fee as a separate expense; the forecast averages them
as business income. Refunds come out, and a payout is money leaving the
Square account, so put payouts in the transfer category. Bills are matched to
synced payments. Unpaid invoices are listed on the Square page and on the
calendar on their due date. For the tax estimate, enter the income as pay
(D31).

Disconnect forgets the token; transactions already synced stay. Anyone with
the token can read your Square data, so treat it like a password; it is kept
in the Fiscus database and therefore in its backups.

## Backup and restore

The whole database is copied to `/data/backups` every night at 02:00 (local
time, `FISCUS_BACKUP_HOUR`) and kept for 14 days
(`FISCUS_BACKUP_RETENTION_DAYS`). The copy uses SQLite's online backup, so it
is consistent while Fiscus runs.

**Download one** (admin): Admin, Backups, "Download a backup now". Keep a copy
off the homelab: it holds every user's finances, so store it like you would
a bank statement.

**Back up the volume** with your usual tool if you prefer; `/data` is
everything (`fiscus.db`, `backups/`, `uploads/`).

**Restore**:

```bash
docker compose stop fiscus
# copy the file you want into the volume if it is not already in /data/backups
docker compose run --rm fiscus python -m fiscus.cli restore /data/backups/fiscus-20261001-020000.db
docker compose start fiscus
```

Restore checks the file is a sound Fiscus database, keeps a copy of the
current one in `/data/backups` (`fiscus-pre-restore-...`), then replaces it.
Migrations run on the next start.

## Configuration

All configuration is environment variables (set in `.env`):

| Variable | Default | |
| --- | --- | --- |
| `FISCUS_SECRET_KEY` | required | `openssl rand -hex 32` |
| `FISCUS_BASE_URL` | required | Your HTTPS address. Setup links use it. |
| `FISCUS_ORIGIN` | from base URL | The origin browsers see, for WebAuthn |
| `FISCUS_RP_ID` | origin's host | WebAuthn relying party ID (your domain) |
| `FISCUS_TRUSTED_PROXIES` | `172.16.0.0/12` in compose | Who may set forwarded headers |
| `FISCUS_TIMEZONE` | `Australia/Perth` | Decides what "today" is |
| `FISCUS_BACKUP_HOUR` | `2` | Local hour for the nightly backup |
| `FISCUS_BACKUP_RETENTION_DAYS` | `14` | |
| `FISCUS_SETUP_LINK_HOURS` | `48` | Onboarding and recovery links |
| `FISCUS_BOOTSTRAP_HOURS` | `24` | The first-run link |
| `FISCUS_SESSION_IDLE_DAYS` | `30` | Sliding |
| `FISCUS_SESSION_ABSOLUTE_DAYS` | `90` | |
| `FISCUS_LOG_LEVEL` | `info` | |
| `FISCUS_HOST_PORT` | `8000` | Host port in compose |

Health: `/healthz` (liveness, used by the container healthcheck) and
`/readyz` (database, migrations, writable volume).

## Tax tables

Tax rates live in `config/tax/<financial year>.yaml`, one file per year (1
July to 30 June). The current year is 2026-27. **Check the file against the
ATO's published resident rates every July**; each file links to the pages.
To add a year without rebuilding, mount a directory over `/app/config/tax`
in the container.

## Developing

```bash
uv venv -p 3.12 .venv && . .venv/bin/activate
uv pip install --require-hashes -r requirements-dev.lock
FISCUS_DATA_DIR=./data FISCUS_BASE_URL=http://localhost:8000 python -m fiscus
```

Checks, as CI runs them:

```bash
ruff format --check fiscus tests scripts
mypy
ruff check fiscus tests scripts
djlint fiscus/templates --lint
pytest                     # includes a real Chromium test of passkeys and the service worker
docker build -t fiscus:local . && scripts/smoke.sh fiscus:local
```

Dependencies are pinned exactly in `pyproject.toml` and locked with hashes in
`requirements.lock` and `requirements-dev.lock`; regenerate with
`uv pip compile pyproject.toml --generate-hashes -o requirements.lock` (add
`--extra dev` for the dev lock). Frontend libraries are vendored in
`fiscus/static/vendor` (see its README).

Read [`CLAUDE.md`](CLAUDE.md) and [`docs/`](docs) before changing anything:
the rules there (ownership on every query, money in cents, no data in the
service worker cache) are tested.

### CI and releases

Every push runs the checks, builds the image and smoke tests it. With
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` set as repository secrets, branch
builds are also pushed as `f3nici/fiscus:<branch>` and `:sha-<commit>`, and
`main` as `:latest`. The **Release** workflow publishes a named tag (for
example `v1.0.0`) to pin in `.env`.

## Licence

Source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE),
copyright Fenici. Free for noncommercial use, including running it for your
own household.

Vendored: htmx (0BSD), uPlot (MIT), vue-cal's stylesheet (MIT), Inter (OFL).
