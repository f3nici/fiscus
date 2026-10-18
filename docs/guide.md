# Fiscus guide

Setting up, running and looking after Fiscus. The [README](../README.md) has
the short version.

- [Setup](#setup)
- [First run: the bootstrap link](#first-run-the-bootstrap-link)
- [Passkeys and recovery](#passkeys-and-recovery)
- [Installing the app on iOS and Android](#installing-the-app-on-ios-and-android)
- [Budget import from a spreadsheet](#budget-import-from-a-spreadsheet)
- [Importing bills from a file](#importing-bills-from-a-file)
- [Reminders with ntfy](#reminders-with-ntfy)
- [Backup and restore](#backup-and-restore)
- [Configuration](#configuration)
- [Tax tables](#tax-tables)
- [Developing](#developing)

## Setup

You need Docker, a domain or subdomain (for example `fiscus.home.example.com`)
and a reverse proxy that serves it over HTTPS. **HTTPS is not optional:**
browsers only allow passkeys and service workers on HTTPS (or `localhost`), and
Fiscus refuses to start a passkey ceremony otherwise.

### With Dockge

1. Create a new stack called `fiscus`.
2. Paste the contents of [`docker-compose.yml`](../docker-compose.yml).
3. In the stack's `.env`, set at least:

   ```env
   FISCUS_BASE_URL=https://fiscus.home.example.com
   ```

   See [`.env.example`](../.env.example) for the rest.
4. Deploy.

### With plain Docker Compose

```bash
mkdir fiscus && cd fiscus
curl -O https://raw.githubusercontent.com/f3nici/fiscus/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/f3nici/fiscus/main/.env.example
# edit .env: FISCUS_BASE_URL
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

Compose publishes the port on `127.0.0.1` only (`FISCUS_HOST_BIND`), so the
proxy reaches Fiscus and nothing else on the network can, over plain HTTP,
around it. NPMplus on the same host (it runs with host networking) forwards
to `127.0.0.1:8000`. A proxy on another machine needs `FISCUS_HOST_BIND=0.0.0.0`;
a proxy container on a shared Docker network reaches `fiscus:8000` without a
published port at all.

Fiscus only believes `X-Forwarded-For` from addresses in
`FISCUS_TRUSTED_PROXIES` (default `172.16.0.0/12`, the usual Docker
networks). If NPMplus reaches Fiscus from elsewhere, set it to NPMplus's
address as Fiscus sees it. That keeps sign-in rate limits and the audit log
accurate.

Check it is up:

```bash
curl https://fiscus.home.example.com/readyz
# {"status":"ready","version":"1.0.0","build":"...","checks":{"database":true,"migrations":true,"data_dir_writable":true}}
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

## Passkeys and recovery

Fiscus is kept by one person (D52): the account made with the bootstrap link
is the only one, and its admin. There is no users page and no password
sign-in; passkeys are the way in.

**Add another device**: Settings, Passkeys, "Add a passkey on this device",
on the device you are signed in on. Name each one (phone, laptop, security
key) so you can tell them apart; Settings shows when each was last used. To
add a device you are not holding, sign in on it with a passkey you already
have (most browsers offer a QR code to scan with your phone), then add one
there. A passkey has to check who is holding it (a face, fingerprint, PIN or
screen lock, D60), so a security key needs a PIN set.

**Revoke a passkey**: Settings, Passkeys, Manage, Revoke. You cannot revoke
your last one. Revoking signs out every other device, so a lost phone that
was still signed in is signed out too (D56).

**Where you are signed in**: Settings, Signed in, lists each device with when
it was last seen. "Sign out everywhere else" ends all but the one you are
using. From the server, this ends every one:

```bash
docker compose exec fiscus python -m fiscus.cli sign-out <username>
```

**Lost every passkey**: get a recovery link on the server, open it on your
new device, create a passkey, then revoke the lost ones in Settings. Using
the link signs you out everywhere else first.

```bash
docker compose exec fiscus python -m fiscus.cli recovery-link <username>
```

**People added before**: earlier versions could add more people and share
bills with them. On upgrade a shared bill became one bill each, at each
person's share, so no figure moved. To remove someone and everything of
theirs (take a backup first; it asks for their username again):

```bash
docker compose exec fiscus python -m fiscus.cli users
docker compose exec fiscus python -m fiscus.cli remove-user <username>
```

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
"Total" are skipped, and each line keeps its amount and how often it is
spent. A line spent every few months (quarterly, yearly) is saved up a month
at a time, so the month you pay it is not over budget. So is a monthly line
with a bill in it paid less often (a phone plan and its yearly device cover):
what it does not spend carries into the next month until the bill is due.

The preview lists every line. Untick any you do not want, change the category
a line goes to (lines sharing a category are added together), or adjust its
amount and how often. Some lines start unticked, with the reason shown:

- one-off costs (stamp duty and other settlement costs), which are not monthly;
- the mortgage repayment when you have a home loan in Fiscus, which tracks it;
- lines whose amount or frequency could not be read.

Choose whether imported amounts replace or add to the budget you have, or
clear it first so only the file's lines are in it. Nothing is saved until you
press Import. An imported amount is the
category's whole budget: bills already in that category count towards it, and
what is left over is budgeted on top of them. A line is never budgeted below
its bills. Anything that should appear on the calendar and in the forecast is a
bill; add it on the Bills page (or import it, below). The Budget page also has
a CSV template.

## Importing bills from a file

Every recurring cost lives in one place, **Bills**: rent, rates, insurance,
strata, water, power, subscriptions. Each bill is on the calendar, in the
forecast, and counts towards its category's budget at a monthly rate (weekly
x 52 / 12, quarterly / 3 and so on). You never enter a cost twice.

Type bills in one at a time, or Bills, **Import**, and upload a CSV or JSON
file. Download a template from that page in either format.

**CSV**

```csv
name,amount,frequency,category,due_date
Council rates,520.00,quarterly,Housing,2026-11-15
Home insurance,1850.00,yearly,Insurance,2027-03-01
Water,310.00,quarterly,Utilities,
```

**JSON**

```json
{
  "bills": [
    {"name": "Council rates", "amount": "520.00", "frequency": "quarterly", "category": "Housing", "due_date": "2026-11-15"},
    {"name": "Home insurance", "amount": "1850.00", "frequency": "yearly", "category": "Insurance", "due_date": "2027-03-01"}
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

A CSV with other column names gets a mapping step. Every import shows a
preview first, compared with the bills you own by name: what will be added,
what will change, and what is not in the file. Bills not in the file are left
alone unless you tick the box, and then they are stopped rather than deleted,
so their payment history stays. A file from an earlier version with
shared_with and split_percent columns still imports: those are left out and
each bill comes in at its full amount. So does a housing costs file.

## Reminders with ntfy

Fiscus can push reminders to your phone through [ntfy](https://docs.ntfy.sh):
the public ntfy.sh, or a server of your own. Off by default.

1. Install the ntfy app on your phone and subscribe to a topic. On ntfy.sh
   anyone who knows a topic can read it, so pick one that is hard to guess
   (`fiscus-alex-7f3k9q`), or reserve it with an account and use an access
   token. A server of your own (for example `binwiederhier/ntfy` in Dockge)
   works the same way.
2. In Fiscus: Settings, Reminders. Tick "Send me reminders", enter the server
   (`https://ntfy.sh`, `http://192.168.1.20:8080` or
   `https://ntfy.home.example.com`), the topic and any token, choose what to
   hear about, Save, then "Send a test". A topic's address as ntfy shows it
   (`ntfy.sh/fiscus-alex-7f3k9q`) works in the server box too.

You get one push per bill due within your chosen number of days, one per
overdue bill, one per low-balance warning, and one per payday that passed
without a payslip (the day after, for the last week's paydays), each once, at
the hour you choose. Messages contain the bill or employer name, date and
amount only. Public servers and private LAN addresses are allowed; the
container's own loopback and link-local addresses are not.

## Backup and restore

The whole database is copied to `/data/backups` every night at 02:00 (local
time, `FISCUS_BACKUP_HOUR`) and kept for 14 days
(`FISCUS_BACKUP_RETENTION_DAYS`). The copy uses SQLite's online backup, so it
is consistent while Fiscus runs.

**Download one**: Settings, Backups, "Download a backup now". Keep a copy off
the homelab: it holds all your finances, so store it like you would a bank
statement.

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
| `FISCUS_BASE_URL` | required | Your HTTPS address. Setup links use it. |
| `FISCUS_ORIGIN` | from base URL | The origin browsers see, for WebAuthn |
| `FISCUS_RP_ID` | origin's host | WebAuthn relying party ID (your domain) |
| `FISCUS_TRUSTED_PROXIES` | `172.16.0.0/12` in compose | Who may set forwarded headers |
| `FISCUS_TIMEZONE` | `Australia/Perth` | Decides what "today" is |
| `FISCUS_BACKUP_HOUR` | `2` | Local hour for the nightly backup |
| `FISCUS_BACKUP_RETENTION_DAYS` | `14` | |
| `FISCUS_SETUP_LINK_HOURS` | `48` | Recovery links |
| `FISCUS_BOOTSTRAP_HOURS` | `24` | The first-run link |
| `FISCUS_SESSION_IDLE_DAYS` | `30` | Sliding |
| `FISCUS_SESSION_ABSOLUTE_DAYS` | `90` | |
| `FISCUS_LOG_LEVEL` | `info` | |
| `FISCUS_HOST_BIND` | `127.0.0.1` | Host address in compose; `0.0.0.0` for a proxy elsewhere |
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

The README's screenshots come from a made-up household:
`python scripts/screenshots.py` seeds it through the app's forms on a fixed
date and writes `docs/screenshots/`. Run it when a screen changes. It needs
Chromium; set `CHROMIUM_PATH` if Playwright's own is not installed.

Read [`CLAUDE.md`](../CLAUDE.md) and [the design notes](.) before changing anything:
the rules there (ownership on every query, money in cents, no data in the
service worker cache) are tested.

### CI and releases

Every push runs the checks, builds the image and smoke tests it. With
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` set as repository secrets, each
build is also pushed as `f3nici/fiscus:sha-<commit>`, a branch's as
`:<branch>`, and every merge to `main` as `:beta`.

Publishing a GitHub release runs the **Release** workflow: the checks again,
then the image as the release's version, without a leading `v` (release
`v1.0.0` is `f3nici/fiscus:1.0.0`), and `:latest` moved to it. A pre-release
gets its version only. The version has to be the one in `fiscus/__init__.py`
and `pyproject.toml`, so bump both before releasing. Running Release by hand
from the Actions tab rebuilds a version, for example from its tag.

| Tag | What it is |
| --- | --- |
| `latest` | The newest release; what `docker-compose.yml` pulls |
| `1.0.0` | A release, to pin in `.env` |
| `beta` | The newest merge to `main`, not yet released |
