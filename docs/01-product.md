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
| Accounts | Transaction, offset, savings and credit card accounts. Balances are typed in, all at once or one at a time, with the date each was last updated. One can be the tax set-aside account; offsets link to a loan. |
| Import | Hidden from the menus since D25, still reachable at `/import`. A tab under Transactions. ANZ CSV detected automatically; any other bank through a column mapping saved per account. PDF statements read from their transaction table (amount columns by position and header, yearless dates from the statement period), with their own saved mapping when the header is unclear. Preview before commit, dedup across overlapping exports, an import log and undo. Upload from the phone's file picker, or Android's share sheet. |
| Categories and rules | Per-user categories flagged income, business, deductible or transfer. "Contains" or regex rules with priorities, applied on import and re-runnable. A one-tap recategorise screen for phones. |
| Bills | Weekly to yearly or one-off, with an end date. Shared with other users by equal, percentage or fixed split. Matched automatically to imported payments (amount tolerance, date window, description hint), or by hand. Paid, due soon, upcoming and overdue states. Rates, insurance and other housing costs are bills too. Imported from CSV or JSON (templates, mapping, a preview and a diff on re-import). |
| Pay | Pay schedules (weekly, fortnightly, monthly or quarterly; net, optional gross and withholding) and payslips. The budget spreads quarterly pay over the months. A payslip records a payday of a schedule and replaces its expected pay everywhere, or is a one-off (bonus, back pay) on top. Paydays in the last eight weeks without a payslip are listed on Pay and the dashboard to put in, or to mark as no pay. A payslip PDF fills in the form; several at once (up to 40) open a list to check, each matched to its payday by date. Files are not kept. Payslips can be edited. |
| Calendar | CareLane's calendar: month, week, day and agenda views. Bills at your share, shared bills, paydays and loan repayments, coloured by type and state, with the forecast balance at the foot of every future day. Agenda by default on a phone. |
| Home loan | Amortised repayment, daily interest on loan less offset charged monthly, rate history, offset savings this month and this financial year, payoff with and without the offset. |
| Forecast | Day by day for 1 to 24 months per account and combined, with paydays, bills, loan repayments and offset interest, planned transfers and average spending. A low-balance warning. Scenarios layered over it without touching real data. |
| Budget | A budget month that starts on the day each person picks (payday works well). Spending is taken off a category by hand, and a bill marked paid comes off its category by itself. Shows expected pay against the total budgeted, what is unbudgeted (extra), what is still to spend, and what is free in the accounts once that is set aside. A category's budget is what is allocated to it; bills in it are paid out of it rather than added, so a recurring cost is entered once, as a bill, and comes off the budget when marked paid. A budget spreadsheet (XLSX or CSV with Item, Amount, Frequency) can be imported with a line-by-line preview. Lines can be grouped under headings (Bills, Living), each with its own totals, and lines and groups put in order with up and down arrows. |
| Goals | Money set aside inside an account for something: a trip, general savings, an emergency fund. An account keeps its one real balance and shows the goals in it and what is unallocated. Goals are not in the budget: what a goal keeps aside only comes off what the budget calls free in accounts (per goal). A goal with a target and a date shows what to put in each month to get there (what is left to save over the months left); money is put in or taken out by hand. One goal can be marked as the tax money, and what it holds counts as set aside for tax. A goal can be saved from a budget line (utilities paid quarterly, say): a Put aside button on that line records the money as spent on it and puts it in the goal at once, so the budget shows it used and Free in accounts does not go up; paying the bill out of the goal is not spent again. It never leaves the account, so the forecast and an offset are unchanged. |
| Tax | Australian financial year. Income from payslips and expected pays, less a deductions total typed in (D31), resident rates and Medicare from a versioned YAML, year to date and projected to 30 June, against the set-aside balance. |
| Dashboard | Balances, unbudgeted money and what is left in each budget, the next 14 days, budget status, tax position, offset savings and a 3-month sparkline with any low-balance warning. |
| PWA | Installable on iOS, Android and desktop. The service worker caches the app shell only; offline shows an offline screen. Bottom navigation when installed. Update prompt. |
| Reminders | Optional ntfy pushes for bills due soon, overdue bills and low balances. |
| Square | Optional, per person. Payments, fees, refunds and payouts from a Square seller account synced into an account every six hours, categorised as chosen; unpaid invoices listed and shown on the calendar. |
| Ops | One container, a named volume, health and ready checks, nightly backups with retention, admin download, CLI restore, CSV export. |

## Out of scope for v1

Live bank feeds, investments, multiple currencies, a native app.
