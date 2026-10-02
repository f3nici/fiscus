# 5. Forecast, loans and recurrence

## Recurrence

`domain/recurrence.py` expands a schedule (anchor date and frequency) into
dates in a range. Weekly and fortnightly step by days. Monthly, quarterly and
yearly keep the anchor's day of month and clamp to a shorter month's last day,
returning to the 31st when the month allows. A schedule never runs before its
anchor. A bill's "next due" is its anchor.

## Loans

- **Repayment**: the annuity `P r / (1 - (1 + r)^-n)` with `r` the annual rate
  over repayments per year.
- **Interest**: every day, `max(0, balance - offset) x rate / 365` accrues,
  with fractions of a cent carried. The accrual is charged to the balance on
  the monthly anniversary of the start date (clamped).
- **Order within a day**: scheduled repayments first, then every other flow,
  then the evening's interest on the balance and the offset accounts' balances
  as they now stand.
- **Rate changes** reprice the repayment over the remaining term from the
  balance that day, unless the repayment is fixed by hand.
- **Offset savings** this month and this financial year sum
  `min(balance, offset at end of day) x rate / 365` over the actual daily
  offset balances from transactions, holding the loan balance at its current
  figure. Lifetime figures assume today's rate and offset hold.

## The engine

`domain/forecast.run(ForecastInput) -> ForecastResult` is pure. Day 0 is
today with real balances. Each later day applies:

1. flows: pay (net), bills at this person's share, planned transfers (both
   legs), scenario one-offs. Unpaid bills overdue within the last two months
   are assumed to go out tomorrow;
2. loan repayments, debited from the repayment account;
3. discretionary spending: a per-day amount with a carried fraction, debited
   from the spending account;
4. loan interest for the evening.

Discretionary spending is, per category, the average of the last 90 days
scaled to a month, excluding transfers, salary (covered by pay schedules) and
any transaction matched to a bill (bills come from their schedule). A budget
line marked "use in forecast" replaces that category's average. Business
income is projected the same way.

Outputs: per-account and combined daily balances, loan balances, markers for
paydays and bills over the large-bill threshold, month-end rows, payoff dates
and the first day a non-credit-card account falls below the threshold.

## Scenarios

`apply_scenario` deep-copies the input and applies adjustments in order:
`one_off`, `income_change` (new net or a delta, from a date, one schedule or
all), `rate_change`, `new_bill`, `extra_repayment` (one-off or recurring).
Real rows are never written. The forecast page runs base and scenario and
draws both.

## Caching

`forecasting.base` caches one result per user in `forecast_cache`, keyed by
`data_version`, today's date and the horizon. `services/versioning.py` bumps
`data_version` on any flush touching an owned row, and for every participant
of a touched bill. Scenario runs are not cached.
