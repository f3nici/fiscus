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
| user_settings (owned) | low-balance threshold, forecast horizon, spending account, large-bill threshold, ntfy settings |

## Money

| Table | Notes |
| --- | --- |
| accounts (owned) | type, opening balance, set-aside flag, `loan_id` for an offset, archived |
| transactions (owned) | date, signed amount, description, category, import, `dedup_hash` unique per account |
| imports (owned) | file name, status (`pending`, `committed`, `undone`), counts, staged file path |
| import_mappings (owned) | one column mapping per account, for non-ANZ files |
| categories (owned) | name unique per user; income, business, deductible, transfer flags |
| rules (owned) | pattern, regex flag, category, priority |

## Bills

| Table | Notes |
| --- | --- |
| bills | owner, amount, frequency, next due (the recurrence anchor), end date, split mode, matching tolerance, window and hint, optional housing item |
| bill_shares (owned) | one per participant, the owner included: split value (basis points or cents), the participant's own account and category |
| bill_payments (owned) | one participant's occurrence marked paid, by transaction or by hand; unique per bill, user and due date, and per transaction |

## Income, loans, planning

| Table | Notes |
| --- | --- |
| pay_schedules (owned) | frequency, anchor payday, net, optional gross and withholding, end date, account |
| payslips (owned) | date, gross, withheld, net, source |
| loans (owned) | principal, start, term, repayment frequency, current balance and its date, optional repayment override, repayment account |
| loan_rates (owned) | rate from an effective date; the history |
| recurring_transfers (owned) | from, to, amount, frequency, tax set-aside flag |
| budgets (owned) | monthly amount per category, `use_in_forecast` |
| housing_items (owned) | name, amount, frequency, category, due date, shared-with user and their share; each drives a bill and its category's budget |
| scenarios, scenario_adjustments (owned) | named what-ifs; adjustments are a kind plus JSON parameters |
| forecast_cache | one row per user: key and serialised result |
| notification_log | one row per reminder sent, so each is sent once |
