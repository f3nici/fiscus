# 1. Product

Fiscus is a self-hosted personal finance tracker for one Australian household,
kept by one person (D52).
It runs as a single container on a homelab, behind a reverse proxy with HTTPS,
and is used mostly from phones as an installed PWA.

## Principles

- **Your data stays home.** No bank feeds, no telemetry, no CDN. The only
  outbound calls are to an ntfy server (ntfy.sh or the household's own) when
  reminders are on.
- **One person keeps it.** Accounts, categories, bills, payslips, budget,
  goals, loans, forecast and tax all belong to the one account. There is no
  users page and nothing is shared (D52); rows are still owned, so a person
  left from an earlier version sees only their own.
- **Passkeys only.** Sign-in is a passkey on your phone, laptop or security
  key. A lost one is replaced with a recovery link from the CLI.
- **Estimates, labelled.** The forecast and the tax figures are estimates
  from your own data, and every screen says so.

## Features (v1)

| Area | What it does |
| --- | --- |
| Accounts | Transaction, offset, savings and credit card accounts. Balances are typed in, all at once or one at a time, with the date each was last updated, and kept by day (D39). Until the next one is typed in, a bill marked paid comes off its account, a payslip's net goes into its account and spending taken off the budget comes off the account it was paid from, and the account page lists them (D42, D49). One can be the tax set-aside account; offsets link to a loan. |
| Categories | Per-user categories flagged income, business, deductible or transfer; budget lines and bills are filed under them. |
| Bills | Weekly to yearly or one-off, with an end date, paid from an account and filed under a category. One whose amount varies (power, water, gas) is marked paid at what it came to and expects the average of its last 3 paid, in the budget, forecast and calendar (D66). Marked paid by hand; paid, due soon, upcoming and overdue states, overdue ones also on the dashboard and the agenda (D40). Rates, insurance and other housing costs are bills too. Imported from CSV or JSON (templates, mapping, a preview and a diff on re-import). |
| Pay | Pay schedules (weekly, fortnightly, monthly or quarterly; net, optional gross and withholding) and payslips, listed ten a page, newest first. The budget spreads quarterly pay over the months. A payslip records a payday of a schedule and replaces its expected pay everywhere, or is a one-off (bonus, back pay) on top. Paydays in the last eight weeks without a payslip are listed on Pay and the dashboard to put in, or to mark as no pay; any this financial year are listed on the tax page, and ntfy can remind about one the day after. A payslip PDF fills in the form; several at once (up to 40) open a list to check, each matched to its payday by date. Files are not kept. Payslips can be edited. |
| Calendar | CareLane's calendar: month, week, day and agenda views. Bills, paydays and loan repayments, coloured by type and state, with the forecast balance at the foot of every future day. Opens on the month, on a phone too. |
| Home loan | Opens straight into the loan when there is only one. Amortised repayment, daily interest on loan less offset charged monthly, rate history, offset savings this month and this financial year from the offset balance known each day, payoff with and without the offset. |
| Forecast | Day by day for 1 to 24 months per account and combined, with paydays, bills, loan repayments and offset interest, planned transfers and average spending. The combined balance's trend (how much it rises or falls a month), and what is free to spend beside it, which is the budget's Free in accounts now day by day (accounts less goals and what is still budgeted for the rest of each budget month, D51), each with a trend, as is a scenario compared. A line turned off in the chart's legend stays off, on every device, until turned back on (D50). A low-balance warning. Scenarios (a new income or bill, a pay or rate change, a one-off, an extra repayment) layered over it without touching real data, one or several ticked together (D69). |
| Budget | A budget month that starts on the day each person picks (payday works well). Spending is taken off a category by hand, and comes off the account it was paid from too (or none, for cash), so spending within a budget leaves what is free in the accounts as it was (D49); a bill marked paid comes off its category by itself. A line's entries this month are listed in its Edit panel, to remove a mistake. Shows expected pay against the total budgeted, what is unbudgeted (extra), what is still to spend, and what is free in the accounts once that is set aside. A line's budget is its bills plus an estimate typed on top for everything else (D64): bills are known amounts, so they set the budget themselves, a line can never be budgeted below them, and a dearer bill raises it. A recurring cost is entered once, as a bill, which gives its category a line, and comes off the budget when marked paid. The page says how much of the budget is bills and how much estimates. A budget spreadsheet (XLSX or CSV with Item, Amount, Frequency) can be imported with a line-by-line preview. Lines can be grouped under headings (Bills, Living), each with its own totals, and lines and groups put in order with up and down arrows. A line paid every few months (rego, insurance) is saved up, and so is a monthly line holding a bill paid less often (a phone plan and its yearly device cover), so what it does not spend carries into the next month: what it holds can be typed in, and a line a bill in the year ahead would find short (two regos on different days are checked together) can be set to hold just enough for every bill by its due date. What it holds is still to spend, never free in the accounts. |
| Goals | Money set aside inside an account for something: a trip, general savings, an emergency fund. An account keeps its one real balance and shows the goals in it and what is unallocated. Goals are not in the budget: what a goal keeps aside only comes off what the budget calls free in accounts (per goal). A goal with a target and a date shows what to put in each month to get there (what is left to save over the months left); money is put in or taken out by hand. One goal can be marked as the tax money, and what it holds counts as set aside for tax. A goal can be saved from a budget line (utilities paid quarterly, say): a Put aside button on that line records the money as spent on it and puts it in the goal at once, so the budget shows it used and Free in accounts does not go up; paying the bill out of the goal is not spent again. It never leaves the account, so the forecast and an offset are unchanged. |
| Tax | Australian financial year. Income from payslips and expected pays (each job's gross and tax from its schedule, else its own payslips, else none withheld; D38), less a deductions total typed in (D31), resident rates and Medicare from a versioned YAML, projected to 30 June and year to date as a share of it, against the set-aside balance. Paydays this year without a payslip are listed. |
| Dashboard | Free to spend first, then overdue bills, balances, unbudgeted money and what is left in each budget, the next 14 days as a ledger with the balance after each day, tax position, offset savings and free to spend over 3 months, with any low-balance warning (D68). |
| PWA | Installable on iOS, Android and desktop. The service worker caches the app shell only; offline shows an offline screen. On a phone, and when installed, bottom tabs the person chooses (two to five, in their order) and a menu that slides in from the right with every screen; pages slide the way along the tabs they went (D67). Update prompt. |
| Reminders | Optional ntfy pushes, through ntfy.sh or a server of your own, for bills due soon, overdue bills, low balances and paydays without a payslip. |
| Ops | One container, a named volume, health and ready checks, nightly backups with retention, download from Settings, CLI restore, recovery links and removing a person. The version running is at the foot of Settings and the menu. |

## Out of scope for v1

Live bank feeds, investments, multiple currencies, a native app.
