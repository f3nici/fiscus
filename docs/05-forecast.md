# 5. Forecast, loans and recurrence

## Recurrence

`domain/recurrence.py` expands a schedule (anchor date and frequency) into
dates in a range. Weekly and fortnightly step by days. Monthly, bimonthly (every two months), quarterly and
yearly keep the anchor's day of month and clamp to a shorter month's last day,
returning to the 31st when the month allows. A schedule never runs before its
anchor. A bill's "next due" is its anchor.

## Loans

- **Repayment**: the annuity `P r / (1 - (1 + r)^-n)` with `r` the annual rate
  over repayments per year and `n` the term in repayments. Monthly is 12 a
  year. Weekly and fortnightly count a year as 365.25 days, 52.18 weeks or
  26.09 fortnights, so a 30-year weekly loan is 1,565.36 repayments, as ANZ
  works it (D70).
- **Repayment days** recur from the first repayment, as the bank set it, or
  one period after the start date when none is given (D71). They go through
  `recurrence.occurrences`, so a monthly loan from the 31st keeps the 31st
  where a month has one. The loan page shows the next one.
- **Interest**: every day, `max(0, balance - offset) x rate / 365` accrues,
  with fractions of a cent carried. The accrual is charged to the balance on
  the monthly anniversary of the start date (clamped). A typed-in balance
  leaves out what accrued since the last charge, so the first charge after
  the balance date includes it, worked on that balance and today's offset.
- **Marked paid** (D76): a repayment fallen due comes off the repayment
  account only once it is marked paid from the budget, which writes a
  transaction dated its day, or once a balance typed in after its day has it.
  Until then the budget counts it still to spend and the forecast takes it
  off tomorrow. The loan's own balance steps on from its balance date either
  way. Loans not tracked in an account count each repayment made as its day
  passes.
- **Order within a day**: scheduled repayments first, then every other flow,
  then the evening's interest on the balance and the offset accounts' balances
  as they now stand.
- **Rate changes** reprice the repayment over the remaining term from the
  balance that day, unless the repayment is fixed by hand. The remaining term
  is the monthly repayments left, or the days left over 7 or 14. A rate the
  same as the one before it is no change. A change dated
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
   payslip recording it on that payslip's date, irregular pay's payments and
   one-off payslips), irregular pay's typical month a day at a time
   (`domain/pay.spread`, D74), bills, planned transfers (both
   legs), scenario one-offs. Pay expecting the same month last year (D77)
   gives a payday its share of that month's pay, and irregular pay that
   month's takings over its days (`domain/pay.spread_by_month`), each
   month the last whole one of its name; a month without that history
   takes the average. Unpaid bills overdue within the last 62 days
   (`bills.OVERDUE_CARRY_DAYS`) are assumed to go out tomorrow; the agenda
   lists them above today. So are loan repayments in that window that have
   fallen due and are not off their account yet (`loans.due`, D76): not
   marked paid, and dated on or after its last balance typed in. The loan
   already counts them repaid, so only the account moves;
2. loan repayments, debited from the repayment account;
3. discretionary spending: a per-day amount with a carried fraction, debited
   from the spending account;
4. loan interest for the evening.

Discretionary spending is, per category, the average of the last 90 days
scaled to a month, excluding transfers, salary (covered by pay schedules) and
any transaction matched to a bill (bills come from their schedule). Any budget
line replaces that category's average with what it budgets on top of its
bills, which are already forecast from their own dates (D25: spending is
budgeted, not imported; D64: only that estimate is stored). Business
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
theirs, one fallen due and not marked paid tomorrow), and a new budget month gives each line its budget again, on top of
what a saved-up line holds. Free to spend and a scenario compared each get their
own trend (D48). A line turned off or on in the chart's legend is kept for
the person (`user_settings.chart_lines`, D50); the rest show as drawn.

## Scenarios

`apply_scenario` deep-copies the input and applies adjustments:
`one_off`, `income_change` (new net, a percentage of it or of what was
expected (D79), and a delta, applied in that order from a date, one schedule or
all; a change is per payday, and for irregular pay, chosen by name, per month:
a new net is its month from that day, spread as a typical month is, a
percentage scales each day's share with fractions carried, and a delta adds
that much a month, over the days in `ForecastInput.irregular`, D78; all of them
is every payday and leaves irregular pay as it is), `rate_change`, `new_bill`, `new_income` (net, recurring, its own kind of
flow so an income change to every schedule leaves it alone), `extra_repayment`
(one-off or recurring). Rate and pay changes apply in date order, so a later
one takes over from its date; the rest add up in any order.
Real rows are never written. The forecast page runs base and scenario and
draws both. Several scenarios ticked together are one run with all their
changes, named for them all (D69).

## Caching

`forecasting.base` caches results per user in `forecast_cache`, keyed by
`data_version` and today's date, one per horizon asked for (the dashboard's
three months beside the forecast page's twelve). `services/versioning.py` bumps
`data_version` on any flush touching an owned row. Scenario runs are not
cached.
