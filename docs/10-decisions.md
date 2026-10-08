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
| D12 | Tax: Australian resident rates, Medicare levy with the low-income phase-in; LITO (since D31), HELP and the surcharge out | Rates in `config/tax/<year>.yaml`, checked each July. |
| D13 | Calendar: vue-cal's DOM and stylesheet rendered on the server, CareLane's tint | Month, week, day, agenda; agenda default on a phone. |
| D14 | Service worker caches `/static/*` and `/offline` only | Offline shows the offline screen, never data. |
| D15 | ntfy, off by default, private LAN allowed, loopback and link-local refused | CareLane blocks private ranges; a homelab ntfy lives in one. Square (D24) is the only other outside service. |
| D16 | CareLane's dark palette throughout | |
| D17 | Default timezone Australia/Perth | `FISCUS_TIMEZONE`. |
| D18 | PolyForm Noncommercial 1.0.0, copyright Fenici | As Vigilo. |
| D19 | CI pushes images to Docker Hub only when credentials exist | Forks and PRs still build and smoke test. |
| D20 | Strict CSP: no inline script or style | uPlot styles through the CSSOM, which CSP allows. |
| D21 | One home per cost: every recurring cost is a bill; a category's budget is its bills (monthly equivalent of your share) plus what you set aside on top (budget half replaced by D29) | Housing costs were a third place that made a bill and a budget line; migration 0003 folded them into bills. Only the part beyond the bills is stored and used in the forecast, so bills are never counted twice. |

| D22 | PDF statements are read once at upload into a CSV-shaped table (pdfminer.six, positions of characters) and then go through the CSV import | Amounts sit in columns by right edge; header words name them. Scans (no text) are refused. |
| D23 | Casual pay: a schedule marked as varying expects the average received per payday over 13 weeks, from deposits matching its text | Paydays with nothing paid count as zero; the window starts at the first deposit seen. A quarterly pay averages over a year instead. Since D25 the match text is off the form, so a varying schedule expects the typical pay entered; saving it clears any old text. |

| D24 | Square sync: a second outside service, off by default, per user, read only, fixed host | Payments (gross) and fees as separate rows, refunds, payouts out to the bank, into one account as an import, every six hours with a week of overlap; ids make it idempotent. Unpaid invoices show on the calendar, not in the forecast, which already averages business income. The token is stored like the ntfy token. |

| D25 | Spending is budgeted by hand, not imported | Transactions, import, rules and Square are hidden from the menus; the code and data stay, so they can come back. A person takes spending off a budget as they go and types in their account balances (about weekly). Spent = hand entries plus their share of bills marked paid that fall due in the period. The budget month starts on a chosen day. Every budget replaces the spending average in the forecast. |

| D26 | Payslip PDFs fill in the payslip form; nothing is saved until the person adds it | Read with the statement reader's lines and cells, by label (Total Earnings, PAYG, Net Pay, Payment Date, Paid By and common variants), this pay before year to date. The file is read in memory and not kept. Missing figures and gross less tax not equalling net are pointed out. |

| D27 | Payslips record paydays: a payslip linked to a schedule payday replaces its expected pay in the forecast, calendar, budget and tax; unlinked ones are one-offs | One expansion (`services/pay.items`) for all four. A recorded pay counts on the payslip's date. Paydays in the last eight weeks without one are asked for; "no pay" is a zero payslip, which a varying schedule's average counts. Matching is by date within three days, a schedule named like the employer first; the person can change it before saving. Payslips from before this stay one-offs, and past one-offs are left out of the budget and calendar so they do not double a payday. |
| D28 | A budget line spent every few months is saved up | Every 2, 3 or 6 months or yearly. Its monthly share builds up from the budget month it was set (plus what the person says is already saved), and hand spending and bills paid in the category come out of that; it is over only when more is spent than saved. Changing it carries the saved amount forward. The amount is kept as typed for its cycle, so a cycle saves exactly it ($2,480 a year, not 12 x $206.67). The forecast still spends it a month at a time. A budget import keeps each line's cycle from its frequency (quarterly, six-monthly, yearly); a category takes the longest of its lines. |

| D29 | A category's budget is what is allocated to it; bills are paid out of it, not added to it (replaces the budget half of D21) | Adding a bill used to raise its category's budget. Now a bill shows beside the budget and, once marked paid, comes off it as spent; a saved-up line (D28) saves the allocation, not the allocation plus its bills. Migration 0009 added each person's recurring bills to their stored budgets, so totals were unchanged on upgrade. The forecast still adds only the part of a budget beyond its recurring bills, which are forecast from their dates. |

| D30 | Quarterly pay is spread over the budget months | In the budget, a quarterly schedule counts its expected net x 4 / 12 every budget month from its first payday's month, and its paydays and payslips are not counted again. The calendar, forecast and tax keep the real paydays. |

| D31 | Tax is on income less a typed-in deductions total; no business lines and no LITO | Sole trader income comes in as pay, so the tax page shows income (payslips and expected pays) less one deductions figure per financial year that the person types in and updates. Business categories no longer feed it. The low income tax offset is left out: on a part year it showed an offset the full year would not get. The set-aside target is the projected amount owing at 30 June, saved evenly: the share of it due by today, and what a week is still needed to reach it. |
| D32 | Savings goals live inside real accounts, not as extra accounts | A bank may allow one offset, so an account keeps the one balance the bank shows, and goals split it: what each holds is its moves added up, and the rest is unallocated. A goal with a target and a date shows what to put in each month: what was left to save at the start of the budget month over the months left (rounded up). Goals are not budget lines (D33). A goal kept aside (the default) is left out of "free in accounts". The money never leaves its account, so the forecast and offset interest do not change. An amount already saved when the goal is made is dated before the budget month, so it is not that month's saving. |
| D33 | Goals stay out of the budget; one goal can be the tax money | A goal is somewhere to put extra money, not a monthly cost, so it has no budget line and nothing it holds counts as budgeted or spent. What a goal kept aside holds only comes off "free in accounts". A goal marked `is_tax` (one per person) adds what it holds to the tax page's set-aside figure, unless it sits in a set-aside account whose balance already counts it. |
| D34 | A goal can be saved from a budget line | For money budgeted monthly but paid every few months from a pot. Putting aside from the line records a budget spend in its category and a goal move, linked, in one step: the line counts it used and the goal keeps it out of "free in accounts", so the free figure does not change. Removing either half removes both, and deleting the goal removes them all. Taking money out of the goal (to pay the bill) is never a spend; a bill marked paid in that category still is, so the person pays it from the goal instead. Deleting the category unlinks the goal and keeps what it holds. |
| D35 | Budget lines are grouped and ordered by hand, with arrows | Groups are per person and only lay out the budget page: each shows its lines' budget, spent and left. Up and down buttons swap a line with its neighbour among the budget lines in its group (or a group with its neighbour), not drag and drop, so it works the same on a phone and needs no script. A line never moved keeps the old order (income last, bigger budgets first) after the moved ones; a line put in a group goes to its end. Removing a group keeps its lines, ungrouped. A group folds to its totals (tapping its name, or "Totals only" for all), kept on the server so it stays folded on every device; putting a line in a folded group opens it. Home loan repayments stay at the top, outside the groups. |
| D36 | A goal saves from several budget lines; a saved-up line checks its next bill | The link sits on the line (`categories.goal_id`): a line saves into one goal, a goal takes any number, so yearly bills can share one pot. Put aside spends from the line it is pressed on; on the goal page the person picks the line. A saved-up line with a bill still to pay shows the bill's due date and whether the line will hold it by then: what it holds now, a month's budget for each budget month before the one the bill falls in, and what was put aside from it into a goal since its last bill. Bills still unpaid from before the budget month are checked first and shown as overdue (D40). |
| D37 | The transactions, statement import, rules and payment-matching screens are removed (ends the "code stays" half of D25; D8 and D22 go with the import) | Hidden since D25 and reachable only by typing a URL. Gone: the transactions page and CSV export, CSV and PDF statement import with mapping, preview and undo, the Android share target, rules and recategorising, Match payments and matching a bill by hand. Bills are marked paid by hand. The tables and their rows stay; Square sync still writes transactions, applies rules and matches bills. The categories page keeps categories and their flags. |
| D38 | Each job's expected pay is taxed on its own figures; year to date is a share of the year (updates D31) | A schedule's gross and tax come from the schedule (a gross alone means gross less net was withheld), else from that job's payslips over the last year, else nothing withheld, which the page says: right for contract income and the safe side for setting aside. Borrowing another job's PAYG rate hid a tax bill. Year to date is the income so far with its share of the projected year's deductions and tax; tax on a part year alone gets the whole tax-free threshold and looked like a refund. Paydays this year before today without a payslip are listed (they are in neither column), and ntfy can remind about one the day after; today's is projected, as its pay may not be in yet. A payslip saved as a one-off is extra income, not its payday's, so the page says to choose the payday instead. |
| D39 | Typed-in balances are kept by day | Typing a balance moves the opening balance (D25), which rewrote every past day, so an offset entered today claimed a year of savings. Each typed balance is kept, one a day; a past day's balance is the last one on or before it plus later transactions. Offset savings use it and count from the first one, and say so. Accounts never typed in (transactions only) work as before. Today's balances and the forecast are unchanged. |
| D40 | Overdue bills are shown wherever money is planned | Overdue is unpaid within the last 62 days (`bills.OVERDUE_CARRY_DAYS`), the same window everywhere: the dashboard and bills list (with Mark paid), ntfy, the budget and the forecast, which counts them going out tomorrow. An agenda that reaches today lists those due before its first day; later ones are on their own days. A saved-up line checks them before its next bill. |
| D41 | Loan interest around the balance date | A lender's balance leaves out interest accrued since the last charge, so the first charge after the balance date includes it (on that balance and today's offset). The payoff projection steps from the balance date, so repayments since an old balance count; its interest is what is still to come after today. |

## 2. Assumptions

- One household, a handful of users. SQLite and an in-process rate limiter are
  sized for that.
- The reverse proxy terminates TLS and sets `X-Forwarded-For` and
  `X-Forwarded-Proto`.
- ANZ exports are headerless `date, amount, description` (a trailing balance
  column is tolerated).

## 3. Open, for later

- Per-account low-balance thresholds (v1 has one per user).
- A per-category view of discretionary spending in the forecast chart.
- An offset-balance forecast in the lifetime savings (v1 holds today's).
- A one-off payslip dated today or earlier is left out of the budget's
  expected pay (D27), so a bonus entered after it lands never shows there.
- Square's page is unlinked since D25 but its sync still runs for anyone
  connected: bring it back or remove it.
