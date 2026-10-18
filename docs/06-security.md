# 6. Security

## Authentication

- **Passkeys** through py_webauthn. Registration asks for a discoverable
  credential (`residentKey: required`), and registration and sign-in require
  user verification (D60): a face, fingerprint, PIN or screen lock.
  Sign-in offers usernameless (conditional autofill and a button) and
  username-first. An unknown username gets a response the same shape as a
  known one.
- **Challenges** are stored server-side by an opaque handle, last five minutes
  and are deleted on first use, success or not.
- **Origin**: passkeys are refused (503) unless `FISCUS_ORIGIN` is HTTPS or
  localhost. The verifier checks origin and RP ID on every ceremony.
- **Setup links** (bootstrap, recovery): 32 random bytes, only the SHA-256
  stored, single use, expire (`FISCUS_SETUP_LINK_HOURS`, bootstrap
  `FISCUS_BOOTSTRAP_HOURS`), revoked by a newer link for the same person. The
  bootstrap link exists only while there are no users and is replaced on each
  restart. Since D52 nothing in the app issues one: bootstrap prints itself,
  and recovery is the CLI.
- **No password sign-in** (D52): passkeys are the only way in, and `users`
  has no password columns (migration 0026). That migration rebuilds `users`
  and refuses to run with foreign keys on, which would cascade the rebuild's
  drop into every owned table.
- **Last passkey**: cannot be revoked.
- **Break-glass**: `python -m fiscus.cli recovery-link <user>` inside the
  container, audited. `remove-user` deletes a person and everything they own
  (database cascades), refuses the last person and the last admin, and is
  audited.

## Sessions and requests

- Server-side sessions; the cookie holds a random token, the table its
  SHA-256. `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax`. Idle
  expiry 30 days (sliding), absolute 90. Revoking a passkey ends every other
  session, a recovery link ends them all, and Settings or `cli sign-out` end
  them by hand (D56).
- Every request re-checks the user is active (no screen disables anyone since
  D52; a recovery link from the CLI re-enables).
- CSRF: a `__Host-` cookie, `SameSite=Strict`, compared in constant time with
  the form field or header, plus an `Origin`/`Referer` check, on every unsafe
  request.
- Redirects after a form go to a path on this site only (`web.local_path`):
  never `//host`, a backslash or a control character (browsers drop tabs and
  newlines, so `/<tab>/host` is `//host`) that a browser could read as
  another origin.
- An unexpected error answers with an error page carrying the usual headers
  and `no-store` (JSON instead for a script's request); nothing is saved (the
  request's session rolls back).
- Rate limits on `/auth/*` and `/setup/*` per IP and per username.
- A request body over 64 MB is refused with 413 before it is read into
  memory (D62).
- Client IPs come from `X-Forwarded-For` only when the peer is in
  `FISCUS_TRUSTED_PROXIES`. Compose publishes the port on `127.0.0.1` by
  default, so only the proxy on the same host reaches it (D61).
- Headers: strict CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin`, HSTS over HTTPS, `no-store` on everything
  but static assets.

## Data

- Ownership on every query (`services/access.py`); 404 for others' rows, and
  for an id too big to be a row.
- Bill and budget import files are staged under random names in
  `/data/uploads`, size-capped, with the owner's id in the name, checked
  against it; they are deleted on apply or after a day.
- XLSX files are size-checked before unzipping and parsed through defusedxml
  (no entity expansion); only cached cell values are read, nothing is executed.
- Payslip PDFs are size-checked (10 MB, 100 pages), parsed off the event loop
  and read in memory; the file is never kept. Nothing in a PDF is executed;
  encrypted ones are refused.
- The service worker never stores data responses; offline shows a static
  screen.
- ntfy: off by default; public servers (ntfy.sh) and private LAN addresses
  allowed; refuses loopback, link-local and unspecified targets, and connects
  to the address it checked (a name is resolved once, then the request goes
  to that address with the name as Host and for TLS), so a name cannot be
  rebound to the container between the check and the send.
  Messages carry bill and employer names, dates and amounts only.
- Backups contain every row in the database: the admin's only (Settings,
  Backups; 404 for anyone else), audited.
