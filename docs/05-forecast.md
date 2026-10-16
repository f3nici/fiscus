# 5. Forecast, loans and recurrence

## Recurrence

`domain/recurrence.py` expands a schedule (anchor date and frequency) into
dates in a range. Weekly and fortnightly step by days. Monthly, bimonthly (every two months), quarterly and
yearly keep the anchor's day of month and clamp to a shorter month's last day,
returning to the 31st when the month allows. A schedule never runs before its
anchor. A bill's "next due" is its anchor.

## Loans

- **Repayment**: the annuity `P r / (1 - (1 + r)^-n)` with `r` the annual rate
  over repayments per year.
- **Interest**: every day, `max(0, balance - offset) x rate / 365` accrues,
  with fractions of a cent carried. The accrual is charged to the balance on
  the monthly anniversary of the start date (clamped). A typed-in balance
  leaves out what accrued since the last charge, so the first charge after
  the balance date includes it, worked on that balance and today's offset.
- **Order within a day**: scheduled repayments first, then every other flow,
  then the evening's interest on the balance and the offset accounts' balances
  as they now stand.
- **Rate changes** reprice the repayment over the remaining term from the
  balance that day, unless the repayment is fixed by hand. A change dated
  before the loan's balance date reprices from that balance, so a backdated
  change still moves the repayment. The calendar and the loan page step the
  same simulation (`loan.repayments`), with today's offset held steady.
  The payoff projection steps from the balance date too, so the repayments
  since an old balance count; its interest is what is still to come after
  today.
- **Offset savings** this month and this financial year sum
  `min(balance, offset at end of day) x rate / 365` over the offset balance
  known on each day: the last one typed in on or before it (D39), holding the
  loan balance at its current figure. Days before the offset was first typed
  in are left out, and the page says from when it counts. Lifetime figures
  assume today's rate and offset hold.

## The engine

`domain/forecast.run(ForecastInput) -> ForecastResult` is pure. Day 0 is
today with real balances. Each later day applies:

1. flows: pay (net, from `services/pay.items`: each schedule payday, or the
   payslip recording it on that payslip's date, and one-off payslips),
   bills, planned transfers (both
   legs), scenario one-offs. Unpaid bills overdue within the last 62 days
   (`bills.OVERDUE_CARRY_DAYS`) are assumed to go out tomorrow; the agenda
   lists them above today;
2. loan repayments, debited from the repayment account;
3. discretionary spending: a per-day amount with a carried fraction, debited
   from the spending account;
4. loan interest for the evening.

Discretionary spending is, per category, the average of the last 90 days
scaled to a month, excluding transfers, salary (covered by pay schedules) and
any transaction matched to a bill (bills come from their schedule). Any budget
line replaces that category's average with the part of its budget beyond the
category's recurring bills, since the bills are already forecast from their own dates
(D25: spending is budgeted, not imported). Business
income is projected the same way.

Outputs: per-account and combined daily balances, the combined balance's
trend (the least-squares straight line over every day shown, and its rise in
an average month), loan balances, month-end rows, payoff dates and the first
day a non-credit-card account falls below the threshold. The page also draws
Free to spend, the budget page's Free in accounts now day by day (D51): the
accounts the budget counts (not the tax set-aside), less what goals kept
aside hold today (the same amount off every day, as goals never move money
between accounts, D46) and what is still budgeted for the rest of each day's
budget month (`domain/forecast.still_to_spend`). Today's is the budget's
Still to spend; later days spend each line as the forecast does (its share
beyond its bills each day, its bills on their dates, loan repayments on
theirs), and a new budget month gives each line its budget again, on top of
what a saved-up line holds. Free to spend and a scenario compared each get their
own trend (D48). A line turned off or on in the chart's legend is kept for
the person (`user_settings.chart_lines`, D50); the rest show as drawn.

## Scenarios

`apply_scenario` deep-copies the input and applies adjustments in order:
`one_off`, `income_change` (new net or a delta, from a date, one schedule or
all), `rate_change`, `new_bill`, `new_income` (net, recurring, its own kind of
flow so an income change to every schedule leaves it alone), `extra_repayment`
(one-off or recurring).
Real rows are never written. The forecast page runs base and scenario and
draws both.

## Caching

`forecasting.base` caches results per user in `forecast_cache`, keyed by
`data_version` and today's date, one per horizon asked for (the dashboard's
three months beside the forecast page's twelve). `services/versioning.py` bumps
`data_version` on any flush touching an owned row. Scenario runs are not
cached.
