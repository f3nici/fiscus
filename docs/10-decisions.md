# 10. Decisions

## 1. Locked

| # | Decision | Notes |
| --- | --- | --- |
| D1 | One container, one process, SQLite on a named volume | Homelab, Dockge. One uvicorn worker. |
| D2 | Python 3.12, FastAPI, SQLAlchemy + Alembic, Jinja2 + HTMX | From the brief. |
| D3 | Finances separate per user; bills are the only shared thing | Ownership enforced in `services/access.py`; tested route by route. |
| D4 | Passkeys first, usernameless and username-first; password opt-in per user by an admin | |
| D5 | Setup links for onboarding, recovery and bootstrap; bootstrap token printed to the logs | Same mechanism for all three. |
| D6 | Shared bill payment is per person | The owner's occurrence is paid by a transaction for the whole bill; a participant's by one for their share, or either by hand. Everyone's calendar, budget and forecast use their own share, so the owner's forecast does not show paying the whole bill and being paid back. |
| D7 | Split modes: equal, percent (basis points), fixed (cents) | Shares always add up to the bill; leftover cents go to the owner first. |
| D8 | Dedup hash: account, date, amount, normalised description, occurrence index within the file | Identical rows in one file survive; overlapping exports collapse. |
| D9 | Loan interest daily on balance less offset, charged monthly on the anniversary; repayment before that day's interest | Matches how Australian lenders state it. |
| D10 | Rate change reprices over the remaining term unless the repayment is fixed by hand | |
| D11 | Discretionary spending: 90-day average per category, excluding matched bill payments and transfers; budget can replace it | |
| D12 | Tax: Australian resident rates, Medicare levy with the low-income phase-in, LITO; HELP and the surcharge out | Rates in `config/tax/<year>.yaml`, checked each July. |
| D13 | Calendar: vue-cal's DOM and stylesheet rendered on the server, CareLane's tint | Month, week, day, agenda; agenda default on a phone. |
| D14 | Service worker caches `/static/*` and `/offline` only | Offline shows the offline screen, never data. |
| D15 | ntfy only, off by default, private LAN allowed, loopback and link-local refused | CareLane blocks private ranges; a homelab ntfy lives in one. |
| D16 | CareLane's dark palette throughout | |
| D17 | Default timezone Australia/Perth | `FISCUS_TIMEZONE`. |
| D18 | PolyForm Noncommercial 1.0.0, copyright Fenici | As Vigilo. |
| D19 | CI pushes images to Docker Hub only when credentials exist | Forks and PRs still build and smoke test. |
| D20 | Strict CSP: no inline script or style | uPlot styles through the CSSOM, which CSP allows. |

## 2. Assumptions

- One household, a handful of users. SQLite and an in-process rate limiter are
  sized for that.
- The reverse proxy terminates TLS and sets `X-Forwarded-For` and
  `X-Forwarded-Proto`.
- ANZ exports are headerless `date, amount, description` (a trailing balance
  column is tolerated).

## 3. Open, for later

- Per-account low-balance thresholds (v1 has one per user).
- Splitting a single imported transaction across categories.
- Importing payslips from PDF.
- A per-category view of discretionary spending in the forecast chart.
- An offset-balance forecast in the lifetime savings (v1 holds today's).
