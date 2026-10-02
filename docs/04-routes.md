# 4. Routes

All pages are server-rendered HTML. Unsafe methods need the CSRF token (form
field `csrf_token` or header `X-CSRF-Token`) and a same-origin `Origin` or
`Referer`. Ids that are not yours are 404.

## Public

| Method | Path | |
| --- | --- | --- |
| GET | /login | Sign-in page (passkey, username-first, optional password) |
| POST | /auth/passkey/options, /auth/passkey/verify | WebAuthn sign-in (JSON) |
| POST | /auth/password | Password fallback |
| GET | /setup/{token} | Setup link page |
| POST | /setup/{token}/options, /setup/{token}/verify | First passkey from a link (JSON) |
| GET | /healthz, /readyz | Liveness; readiness (database, migrations, writable /data) |
| GET | /manifest.webmanifest, /sw.js, /offline | PWA |

Rate limited: everything under `/auth/` and `/setup/`, by IP and by username.

## Signed in

| Area | Paths |
| --- | --- |
| Dashboard | `/`, `/more` |
| Accounts | `/accounts`, `/accounts/new`, `/accounts/{id}`, `/accounts/{id}/edit`, `POST /accounts[/{id}]`, `POST /accounts/{id}/delete` |
| Import | `/import`, `POST /import`, `/import/{id}` (mapping or preview), `POST /import/{id}/mapping`, `/account`, `/commit`, `/undo`, `/discard`, `POST /share-target` |
| Transactions | `/transactions`, `POST /transactions`, `POST /transactions/{id}/category`, `/delete`, `/transactions/export.csv` |
| Categories and rules | `/categories`, `POST /categories[/{id}]`, `/delete`, `POST /rules[/{id}]`, `/delete`, `POST /rules/rerun`, `/recategorise`, `POST /recategorise/bulk` |
| Bills | `/bills`, `/bills/new`, `/bills/{id}`, `/edit`, `POST /bills[/{id}]`, `/delete`, `/my-share`, `POST /bills/rematch`, `/bills/{id}/occurrence/{date}`, `POST .../paid`, `POST .../unpaid` |
| Pay | `/pay`, `POST /pay/schedules[/{id}]`, `/delete`, `POST /payslips`, `/payslips/{id}/delete` |
| Calendar | `/calendar?view=month\|week\|day\|agenda&date=` (HTMX partial when `HX-Target: cal`) |
| Loans | `/loans`, `/loans/new`, `/loans/{id}`, `/edit`, `POST /loans[/{id}]`, `/rates`, `/rates/{id}/delete`, `/offsets`, `/offsets/{account}/unlink`, `/delete` |
| Forecast | `/forecast?months=&scenario_id=`, `POST /transfers`, `/transfers/{id}/delete`, `/scenarios`, `POST /scenarios`, `/scenarios/{id}`, `/delete`, `/adjustments`, `/adjustments/{id}/delete` |
| Budget | `/budget?month=YYYY-MM`, `POST /budget`, `/budget/template.csv`, `POST /budget/import`, `/budget/import/{file}?sheet=`, `POST .../apply` |
| Housing | `/housing`, `POST /housing[/{id}]`, `/delete`, `/housing/template.csv`, `/housing/template.json`, `POST /housing/import`, `/housing/import/{file}`, `POST .../apply` |
| Tax | `/tax` |
| Settings | `/settings`, `POST /settings/passkeys/options`, `/verify`, `/{id}/rename`, `/{id}/revoke`, `POST /settings/password`, `/password/clear`, `POST /settings/forecast`, `POST /settings/ntfy`, `/ntfy/test`, `/ntfy/clear-token` |
| Sign out | `POST /auth/logout` |

## Admin

`/admin/users`, `POST /admin/users`, `POST /admin/users/{id}/invite`,
`/revoke-link`, `/active`, `/password`, `/admin`; `/admin/audit`;
`/admin/backups`, `POST /admin/backups/run`, `POST /admin/backups/download`.
Non-admins get a 404.
