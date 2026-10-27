# 3. Data model

Money columns are integer cents. Interest rates are decimal strings ("6.24"
means 6.24 % a year). Datetimes are naive UTC; dates are the household's local
dates. Tables marked **owned** carry `user_id`, and changing a row bumps that
user's `data_version` (the forecast cache key).

## Identity

| Table | Notes |
| --- | --- |
| users | username, display name, admin and active flags, random `user_handle` for WebAuthn, `data_version`. No password columns: sign-in is passkeys only (D52), and migration 0026 dropped them |
| webauthn_credentials | credential id, COSE public key, sign count, transports, name, backed-up flag, created and last used |
| sessions | SHA-256 of the cookie token, idle and absolute expiry, sign-in method, IP, user agent |
| setup_links | SHA-256 of the token, purpose (`bootstrap`, `recovery`; `onboard` from before D52), user, expiry, used and revoked times |
| auth_challenges | WebAuthn challenge by opaque handle, purpose, five-minute expiry, deleted on first use |
| audit_log | append-only (triggers refuse UPDATE and DELETE) |
| app_state | small key/value for instance state |
| user_settings (owned) | low-balance threshold, forecast horizon, spending account, ntfy settings (`ntfy_payslips`: a payday passed without a payslip), `budget_start_day` (the budget month runs from this day), `chart_lines` (forecast chart lines turned off or on in its legend, JSON, D50), `tabs` (the screens on the bottom tabs in order, a JSON list of keys, empty for the default, D67) |

## Money

| Table | Notes |
| --- | --- |
| accounts (owned) | type, opening balance (used only while no balance has been typed in), set-aside flag, `loan_id` for an offset, archived, `balance_updated_on` |
| account_balances (owned) | each balance typed in: account, date (one a day, the last wins), cents, `typed_at` (when; none before D45). Any day's balance, today's too, is the last one on or before it plus the transactions dated after that day and those dated its day put in after `typed_at`; offset savings read it (D39, D42, D45) |
| transactions (owned) | date, signed amount, description, category, import, `dedup_hash` unique per account. Kept from Square sync and statement imports (both gone, D37 and D55), and written for a bill marked paid by hand (`bill_payment_id`), a payslip (`payslip_id`) or a budget spend (`budget_spend_id`) so the balance moves with it; deleted with that row (D42, D49). The account page lists those since the balance was typed in |
| imports (owned) | a Square sync batch or statement import from before D55 and D37; nothing writes one now, but their transactions point here |
| categories (owned) | name unique per user; income, business, deductible, transfer flags; the budget group its line sits in and its place there (0 until moved); the goal its line saves into (optional) |
| budget_groups (owned) | a heading on the budget page: name, position, collapsed (shows only its totals) |

## Bills

| Table | Notes |
| --- | --- |
| bills (owned) | amount, frequency, next due (the recurrence anchor), end date, the account it is paid from and its category (both optional), `varies` (marked paid at what it came to; the amount is then the average of the last three paid, D66). Not shared since D52: migration 0025 gave each person their share of a shared bill as a bill of their own, and dropped `bill_shares` |
| bill_payments (owned) | an occurrence marked paid by hand (or, before D55, matched to a synced transaction), with what was paid (`amount_cents`, D57); unique per bill, user and due date, and per transaction |

## Income, loans, planning

| Table | Notes |
| --- | --- |
| pay_schedules (owned) | frequency (weekly, fortnightly, monthly or quarterly), anchor payday, net, optional gross and withholding, end date, account; `varies` and `match_text` for casual pay (expects the 13-week average (a year, for quarterly pay) per payday of deposits containing the text, the net until any arrive; no longer on the form since D25, so the net is used; a varying schedule averages its own payslips first) |
| payslips (owned) | date, gross, withheld, net, source, account (paid into, else the schedule's); `schedule_id` and `payday`, unique together, when it records a schedule payday and replaces that payday's expected pay (zero figures: nothing was paid). Both null for a one-off, which is extra income in the forecast, calendar and tax projection when dated after today |
| loans (owned) | principal, start, term, repayment frequency, optional first repayment date (D71), current balance and its date, optional repayment override, repayment account |
| loan_rates (owned) | rate from an effective date; the history |
| recurring_transfers (owned) | from, to, amount, frequency, tax set-aside flag |
| budgets (owned) | per category, the monthly amount budgeted on top of this person's bills in it, which are in its budget by themselves (D64; every spending category holding a bill has one), `use_in_forecast` (no longer read: every budget is used in the forecast), `cycle_months` (1, 2, 3, 6 or 12; above 1 the line is saved up, as is a monthly spending line holding a bill paid less often, D63), `total_cents` (the amount on top as typed for one cycle, so a cycle saves exactly that), `saved_since` and `saved_opening_cents` (where the saving starts) |
| budget_spends (owned) | money taken off a category's budget by hand: category, date, amount (negative for a refund), note. The account it was paid from is its transaction's (D49); one put aside into a goal (D34) or paid in cash has none |
| goals (owned) | name, the account it sits in (optional), target amount and date (both optional; with both it shows a monthly amount to put in), `keep_aside` (its money is not free to spend), `is_tax` (holds the tax money; one per person), archived (done). The budget lines it is saved from are the categories pointing at it |
| goal_moves (owned) | money put into (positive) or taken out of (negative) a goal by hand: date, amount, note, and the budget spend recorded with it when put aside from a budget line (removed together). What a goal holds is the sum |
| tax_deductions (owned) | one total of deductions per person per financial year (`year` like "2026-27"), typed in on the tax page (D31) |
| scenarios, scenario_adjustments (owned) | named what-ifs; adjustments are a kind plus JSON parameters (`one_off`, `income_change`, `rate_change`, `new_bill`, `new_income`, `extra_repayment`) |
| forecast_cache | one row per user: key (data version and date) and a serialised result per horizon asked for |
| notification_log | one row per reminder sent, so each is sent once |
