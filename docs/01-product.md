# 1. Product

Fiscus is a self-hosted personal finance tracker for one Australian household.
It runs as a single container on a homelab, behind a reverse proxy with HTTPS,
and is used mostly from phones as an installed PWA.

## Principles

- **Your data stays home.** No bank feeds, no external APIs, no telemetry, no
  CDN. The only outbound call is to the household's own ntfy server, and only
  when someone turns reminders on.
- **Finances are separate per person.** Each user has their own accounts,
  transactions, categories, rules, payslips, budget, loans, forecast and tax.
  The one thing two people can share is a bill.
- **Passkeys first.** Sign-in is a passkey on your phone, laptop or security
  key. A password is an opt-in fallback an admin turns on per person.
- **Estimates, labelled.** The forecast and the tax figures are estimates
  from your own data, and every screen says so.

## Features (v1)

| Area | What it does |
| --- | --- |
| Accounts | Transaction, offset, savings and credit card accounts with opening balances. One can be the tax set-aside account; offsets link to a loan. |
| Import | ANZ CSV detected automatically; any other bank through a column mapping saved per account. Preview before commit, dedup across overlapping exports, an import log and undo. Upload from the phone's file picker, or Android's share sheet. |
| Categories and rules | Per-user categories flagged income, business, deductible or transfer. "Contains" or regex rules with priorities, applied on import and re-runnable. A one-tap recategorise screen for phones. |
| Bills | Weekly to yearly or one-off, with an end date. Shared with other users by equal, percentage or fixed split. Matched automatically to imported payments (amount tolerance, date window, description hint), or by hand. Paid, due soon, upcoming and overdue states. |
| Pay | Pay schedules (net, optional gross and withholding) and payslips. A payslip dated after today, such as a bonus, is one-off income in the forecast, calendar and tax projection. |
| Calendar | CareLane's calendar: month, week, day and agenda views. Bills at your share, shared bills, paydays and loan repayments, coloured by type and state, with the forecast balance at the foot of every future day. Agenda by default on a phone. |
| Home loan | Amortised repayment, daily interest on loan less offset charged monthly, rate history, offset savings this month and this financial year, payoff with and without the offset. |
| Forecast | Day by day for 1 to 24 months per account and combined, with paydays, bills, loan repayments and offset interest, planned transfers and average spending. A low-balance warning. Scenarios layered over it without touching real data. |
| Budget and housing | Monthly budget against actual. A budget spreadsheet (XLSX or CSV with Item, Amount, Frequency) can be imported with a line-by-line preview. Housing costs entered by hand or imported (CSV or JSON, with templates, mapping, preview and a diff on re-import); each becomes a budget line and a recurring bill. |
| Tax | Australian financial year. Salary from payslips, sole trader profit from business categories, resident rates and Medicare from a versioned YAML, year to date and projected to 30 June, against the set-aside balance. |
| Dashboard | Balances, spending this month, the next 14 days, budget status, tax position, offset savings and a 3-month sparkline with any low-balance warning. |
| PWA | Installable on iOS, Android and desktop. The service worker caches the app shell only; offline shows an offline screen. Bottom navigation when installed. Update prompt. |
| Reminders | Optional ntfy pushes for bills due soon, overdue bills and low balances. |
| Ops | One container, a named volume, health and ready checks, nightly backups with retention, admin download, CLI restore, CSV export. |

## Out of scope for v1

Live bank feeds, investments, multiple currencies, a native app.
