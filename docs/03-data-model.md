# 3. Data model

Money columns are integer cents. Interest rates are decimal strings ("6.24"
means 6.24 % a year). Datetimes are naive UTC; dates are the household's local
dates. Tables marked **owned** carry `user_id`, and changing a row bumps that
user's `data_version` (the forecast cache key).

## Identity

| Table | Notes |
| --- | --- |
| users | username, display name, admin and active flags, random `user_handle` for WebAuthn, `password_enabled` (admin-controlled) and argon2id hash, `data_version` |
| webauthn_credentials | credential id, COSE public key, sign count, transports, name, backed-up flag, created and last used |
| sessions | SHA-256 of the cookie token, idle and absolute expiry, sign-in method, IP, user agent |
| setup_links | SHA-256 of the token, purpose (`bootstrap`, `onboard`, `recovery`), user, expiry, used and revoked times |
| auth_challenges | WebAuthn challenge by opaque handle, purpose, five-minute expiry, deleted on first use |
| audit_log | append-only (triggers refuse UPDATE and DELETE) |
| app_state | small key/value for instance state |
| square_connections (owned) | one per user: enabled, access token, target account, categories for payments, fees and payouts, start date, synced-until, last result and error |
| square_invoices (owned) | unpaid invoice payment requests as of the last sync: number, customer, due date, amount owing |
| user_settings (owned) | low-balance threshold, forecast horizon, spending account, ntfy settings (`ntfy_payslips`: a payday passed without a payslip), `budget_start_day` (the budget month runs from this day) |

## Money

| Table | Notes |
| --- | --- |
| accounts (owned) | type, opening balance (used only while no balance has been typed in), set-aside flag, `loan_id` for an offset, archived, `balance_updated_on` |
| account_balances (owned) | each balance typed in: account, date (one a day, the last wins), cents, `typed_at` (when; none before D45). Any day's balance, today's too, is the last one on or before it plus the transactions dated after that day and those dated its day put in after `typed_at`; offset savings read it (D39, D42, D45) |
| transactions (owned) | date, signed amount, description, category, import, `dedup_hash` unique per account. Written by Square sync, kept from imports before D37, and written for a bill marked paid by hand (`bill_payment_id`) or a payslip (`payslip_id`) so the balance moves with it; deleted with that row (D42). The account page lists those since the balance was typed in |
| imports (owned) | a Square sync batch, or a statement import from before D37 |
| import_mappings (owned) | column mappings from statement imports before D37; no longer read |
| categories (owned) | name unique per user; income, business, deductible, transfer flags; the budget group its line sits in and its place there (0 until moved); the goal its line saves into (optional) |
| budget_groups (owned) | a heading on the budget page: name, position, collapsed (shows only its totals) |
| rules (owned) | pattern, regex flag, category, priority; Square sync applies them; no screen edits them since D37 |

## Bills

| Table | Notes |
| --- | --- |
| bills | owner, amount, frequency, next due (the recurrence anchor), end date, split mode, matching tolerance, window and hint |
| bill_shares (owned) | one per participant, the owner included: split value (basis points or cents), the participant's own account and category |
| bill_payments (owned) | one participant's occurrence marked paid, by hand or matched to a Square transaction; unique per bill, user and due date, and per transaction |

## Income, loans, planning

| Table | Notes |
| --- | --- |
| pay_schedules (owned) | frequency (weekly, fortnightly, monthly or quarterly), anchor payday, net, optional gross and withholding, end date, account; `varies` and `match_text` for casual pay (expects the 13-week average (a year, for quarterly pay) per payday of deposits containing the text, the net until any arrive; no longer on the form since D25, so the net is used; a varying schedule averages its own payslips first) |
| payslips (owned) | date, gross, withheld, net, source, account (paid into, else the schedule's); `schedule_id` and `payday`, unique together, when it records a schedule payday and replaces that payday's expected pay (zero figures: nothing was paid). Both null for a one-off, which is extra income in the forecast, calendar and tax projection when dated after today |
| loans (owned) | principal, start, term, repayment frequency, current balance and its date, optional repayment override, repayment account |
| loan_rates (owned) | rate from an effective date; the history |
| recurring_transfers (owned) | from, to, amount, frequency, tax set-aside flag |
| budgets (owned) | per category, the monthly amount allocated to it; this person's bills in it are paid out of it (D29), `use_in_forecast` (no longer read: every budget is used in the forecast), `cycle_months` (1, 3, 6 or 12; above 1 the line is saved up), `total_cents` (the amount as typed for one cycle, so a cycle saves exactly that), `saved_since` and `saved_opening_cents` (where the saving starts) |
| budget_spends (owned) | money taken off a category's budget by hand: category, date, amount (negative for a refund), note |
| goals (owned) | name, the account it sits in (optional), target amount and date (both optional; with both it shows a monthly amount to put in), `keep_aside` (its money is not free to spend), `is_tax` (holds the tax money; one per person), archived (done). The budget lines it is saved from are the categories pointing at it |
| goal_moves (owned) | money put into (positive) or taken out of (negative) a goal by hand: date, amount, note, and the budget spend recorded with it when put aside from a budget line (removed together). What a goal holds is the sum |
| tax_deductions (owned) | one total of deductions per person per financial year (`year` like "2026-27"), typed in on the tax page (D31) |
| scenarios, scenario_adjustments (owned) | named what-ifs; adjustments are a kind plus JSON parameters (`one_off`, `income_change`, `rate_change`, `new_bill`, `new_income`, `extra_repayment`) |
| forecast_cache | one row per user: key (data version and date) and a serialised result per horizon asked for |
| notification_log | one row per reminder sent, so each is sent once |
