# 6. Security

## Authentication

- **Passkeys** through py_webauthn. Registration asks for a discoverable
  credential (`residentKey: required`) with user verification preferred.
  Sign-in offers usernameless (conditional autofill and a button) and
  username-first. An unknown username gets a response the same shape as a
  known one.
- **Challenges** are stored server-side by an opaque handle, last five minutes
  and are deleted on first use, success or not.
- **Origin**: passkeys are refused (503) unless `FISCUS_ORIGIN` is HTTPS or
  localhost. The verifier checks origin and RP ID on every ceremony.
- **Setup links** (bootstrap, onboarding, recovery): 32 random bytes, only the
  SHA-256 stored, single use, expire (`FISCUS_SETUP_LINK_HOURS`, bootstrap
  `FISCUS_BOOTSTRAP_HOURS`), revoked by a newer link or by disabling the
  person. The bootstrap link exists only while there are no users and is
  replaced on each restart.
- **Password fallback**: off by default; an admin allows it per person;
  argon2id; 12 characters minimum; timing equalised for unknown users.
- **Last passkey**: cannot be revoked unless a password is active.
- **Break-glass**: `python -m fiscus.cli recovery-link <user>` inside the
  container, audited.

## Sessions and requests

- Server-side sessions; the cookie holds a random token, the table its
  SHA-256. `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax`. Idle
  expiry 30 days (sliding), absolute 90.
- Disabling a user deletes their sessions; every request re-checks the user is
  active.
- CSRF: a `__Host-` cookie, `SameSite=Strict`, compared in constant time with
  the form field or header, plus an `Origin`/`Referer` check. The share target
  is exempt and can only stage a file.
- Rate limits on `/auth/*` and `/setup/*` per IP and per username.
- Client IPs come from `X-Forwarded-For` only when the peer is in
  `FISCUS_TRUSTED_PROXIES`.
- Headers: strict CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin`, HSTS over HTTPS, `no-store` on everything
  but static assets.

## Data

- Ownership on every query (`services/access.py`); 404 for others' rows.
- Uploads are staged under random names in `/data/uploads`, size-capped,
  text-only, and deleted on commit, discard or after a day.
- Housing import file names embed the owner's id and are checked against it.
- CSV exports guard against formula injection.
- The service worker never stores data responses; offline shows a static
  screen.
- ntfy: off by default; refuses loopback, link-local and unspecified targets;
  messages carry bill names, dates and amounts only.
- Backups contain everyone's data: admin only, audited.
