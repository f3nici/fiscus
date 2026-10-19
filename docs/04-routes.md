# 4. Routes

All pages are server-rendered HTML. Unsafe methods need the CSRF token (form
field `csrf_token` or header `X-CSRF-Token`) and a same-origin `Origin` or
`Referer`. Ids that are not yours are 404.

## Public

| Method | Path | |
| --- | --- | --- |
| GET | /login | Sign-in page (passkey, usernameless or username-first) |
| POST | /auth/passkey/options, /auth/passkey/verify | WebAuthn sign-in (JSON) |
| GET | /setup/{token} | Setup link page |
| POST | /setup/{token}/options, /setup/{token}/verify | First passkey from a link (JSON) |
| GET | /healthz, /readyz | Liveness; readiness (database, migrations, writable /data) |
| GET | /manifest.webmanifest, /sw.js, /offline | PWA |

Rate limited: everything under `/auth/` and `/setup/`, by IP and by username.

## Signed in

| Area | Paths |
| --- | --- |
| Dashboard | `/`, `/more` |
| Accounts | `/accounts`, `/accounts/new`, `/accounts/{id}`, `/accounts/{id}/edit`, `POST /accounts[/{id}]`, `POST /accounts/balances`, `POST /accounts/{id}/balance`, `POST /accounts/{id}/delete` |
| Categories | `/categories`, `POST /categories[/{id}]`, `/delete` |
| Bills | `/bills`, `/bills/new`, `/bills/{id}`, `/edit`, `POST /bills[/{id}]`, `/delete`, `/bills/{id}/occurrence/{date}`, `POST .../paid` (`amount`, what a bill that varies came to, D66), `POST .../unpaid` (`back` returns to a path on this site only) |
| Pay | `/pay?page=` (payslips ten a page), `POST /pay/schedules[/{id}]`, `/delete`, `/pay?slip={id}` and `/pay?payday={schedule}:{date}` (the payslip form), `POST /payslips[/{id}]`, `POST /payslips/read` (PDFs fill the form or the review list), `POST /payslips/bulk`, `POST /pay/schedules/{id}/no-pay`, `/payslips/{id}/delete` |
| Calendar | `/calendar?view=month\|week\|day\|agenda&date=` (HTMX partial when `HX-Target: cal`) |
| Loans | `/loans` (straight to the loan when there is one), `/loans/new`, `/loans/{id}`, `/edit`, `POST /loans[/{id}]`, `/rates`, `/rates/{id}/delete`, `/offsets`, `/offsets/{account}/unlink`, `/delete` |
| Forecast | `/forecast?months=&scenario_id=`, `POST /transfers`, `/transfers/{id}/delete`, `/scenarios`, `POST /scenarios`, `/scenarios/{id}`, `/delete`, `/adjustments`, `/adjustments/{id}/delete` |
| Budget | `/budget?month=YYYY-MM`, `POST /budget`, `POST /budget/spend`, `/spend/{id}/delete`, `/start-day`, `POST /budget/{category}/move`, `/remove`, `/saved`, `POST /budget/groups/fold`, `POST /budget/groups`, `POST /budget/groups/{id}`, `/move`, `/delete`, `/budget/template.csv`, `POST /budget/import`, `/budget/import/{file}?sheet=`, `POST .../apply` |
| Bill import | `/bills/import`, `POST /bills/import`, `/bills/template.csv`, `/bills/template.json`, `/bills/import/{file}`, `POST .../apply`; `/housing` redirects to `/bills` |
| Goals | `/goals`, `POST /goals`, `/goals/{id}`, `POST /goals/{id}`, `/move`, `/moves/{move}/delete`, `/done`, `/delete` |
| Tax | `/tax`, `POST /tax/deductions` |
| Settings | `/settings`, `POST /settings/passkeys/options`, `/verify`, `/{id}/rename`, `/{id}/revoke`, `POST /settings/forecast`, `POST /settings/ntfy`, `/ntfy/test`, `/ntfy/clear-token`; the admin's `POST /settings/backups/run`, `/backups/download` (404 for anyone else) |
| Sign out | `POST /auth/logout` |
