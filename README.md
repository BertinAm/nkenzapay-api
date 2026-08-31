# nkenzapay-api

The API behind [nkenzapay.com](https://nkenzapay.com), a cross-border money
transfer service between Cameroon and India. Django and Django REST Framework.

The customer-facing app lives in a separate repository and is deployed
separately. This one serves `api.nkenzapay.com` and owns the database.

> **This repository is public.** Nothing here should name a real account, key,
> host or customer. Payment details, credentials and operational documents
> belong in configuration. Read [Configuration](#configuration) before adding
> anything.

## What it does

A customer asks what a transfer would cost, opens an order, and completes it
inside a private chat with the NkenzaPay desk, who verifies the payment by hand
and pays out the other side. Thirteen statuses, one audit trail, one receipt.

### The rule the code is organised around

Every amount shown to a customer is **after the charge**. The raw conversion is
a working line, never the promise.

```
100,000 XAF  →  rate  →  16,935.00 INR  →  6%  →  ₹15,918.90 lands
```

One place computes that: `nkenzapay/pricing/engine.py`, called by both the quote
endpoint and order creation. It has more tests than anything else here, and the
brief's worked examples are among them.

Nothing commercial is hard-coded. Fee percentage, minimum and maximum fee,
per-country overrides, transfer minimums, daily and monthly limits, payment
methods and the account details customers pay into are database rows the desk
edits from the admin, with no deploy.

## Layout

```
config/           settings, urls, WSGI and ASGI entry points
nkenzapay/
  accounts/       users, profiles, desk roles, login activity
  geo/            countries, currencies, corridors
  rates/          FX providers, rate snapshots, held quotes
  pricing/        fee rules, limits, the calculation engine
  payments/       methods, and the instructions the chat renders
  transactions/   orders, the status machine, chat, attachments, receipts
  disputes/       cases and resolutions
  content/        news, legal documents, newsletter
  notifications/  records, preferences, delivery rules
  analytics/      page views, exports
  audit/          append-only log of every desk action
  security/       abuse detection, blocking, idempotency
  adminapi/       the desk's endpoints, gated by role and 2FA
  common/         money, storage, uploads, throttling, errors
tests/
```

## Running it locally

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements/dev.txt   # Windows: .venv/Scripts/python.exe
cp .env.example .env                                      # then fill it in
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createcachetable
.venv/bin/python manage.py seed --demo
.venv/bin/python manage.py runserver
```

`seed` loads currencies, both corridors, the payment methods, the 6% fee rule
and both minimums. `--demo` adds sample articles and obvious placeholder payment
details. Add `--admin-email you@example.com --admin-password '…'` to create an
owner account.

`createcachetable` is not optional where there is no Redis. Rate limiting and
address blocking read the cache on every request.

```bash
.venv/bin/python -m pytest        # 175 tests
.venv/bin/python manage.py check --deploy
```

Run `check --deploy` as the last step of every deploy. It fails on the
configuration mistakes that are otherwise found by an outage or a leak: a disk
written in the clear, a media root the web server can reach, a cache that raises
on every request, a placeholder secret key, and an address header that either
cannot be trusted or is not being read.

## Configuration

Everything environment-specific is read from the environment. `.env.example`
lists the full set with empty values.

The ones worth understanding:

| Variable | Why it matters |
|---|---|
| `SECRET_KEY` | Signs sessions and upload links. Never reuse one, never commit one. |
| `TRUSTED_IP_HEADERS` | Which headers may be believed for the caller's address. Set it **only** to a header your own proxy writes. Trusting one nothing sets lets anyone forge their address and walk past a block. |
| `COOKIE_DOMAIN` | Lets the front end and the API share a session across two hosts on the same registrable domain. |
| `SECURITY_THRESHOLDS` | How many hostile events from one address before it is refused. The defaults are in the source and therefore public; set your own. |
| `FX_API_KEY` | Read on the server only. It has no path to a browser and must not acquire one. |
| `MEDIA_STORAGE` | `local` writes to disk, `s3` to private object storage. |
| `MEDIA_ENCRYPTION_KEY` | Seals every file before it is written. Required for `local`. Losing it loses the files; storing it in the same backup as the files defeats it. |
| `MEDIA_RETENTION_DAYS` | Days after a transfer closes before its attachments are deleted. |

### Payment details are not in this repository

The seed creates the payment methods with **empty** details and warns you. The
account numbers customers are told to pay into are entered once on the admin's
Payment methods screen and live only in the database.

That is deliberate. An account number in a public repository tells anyone who
reads it where the money arrives, which is the first thing you need to
impersonate the desk convincingly. Re-running the seed never overwrites details
that are already set.

## Security

All of it is enforced in code and covered by tests. The summary is here because
a maintainer needs one; the detail is in `tests/`.

Every endpoint is gated server-side. A customer reaches only their own
transfers, chat and attachments, and a transfer that does not exist answers
identically to one belonging to somebody else. Desk actions are gated by role,
and anything that moves money needs TOTP.

Order figures are frozen from the quote when the order is created, and never
recalculated. Amounts sent in a request body are ignored. A quote belongs to one
account and can be spent once.

Sign-up, order creation, chat messages and every transfer action accept an
`Idempotency-Key` header. The same key replays the first response instead of
doing the work twice, so a double tap cannot open two transfers.

Uploads are checked twice: the declared type and size before an upload URL is
issued, then the bytes themselves against the format's magic number. Executables
are refused however they are named. Files are stored under a generated name and
served only through short-lived links scoped to the transfer's participants.

Every file the local backend writes is sealed with AES-256-GCM before it reaches
the disk, keyed from the environment, with its storage path bound into the
ciphertext so a file cannot be moved into another customer's slot. Files are
created 0600 inside a 0700 tree. A copy of the disk without the key (a backup, a
snapshot, anything else on the same account) is a directory of noise.
`sweep_media` then removes what is no longer needed: uploads abandoned mid-flow,
and attachments past their retention date. Holding less is the one control an
attacker cannot work around.

Injection and traversal probes are refused and recorded. Scanning, failed
sign-ins, CSRF failures and rejected uploads are recorded with enough context to
act on. An address that crosses a threshold is refused automatically and the
block expires on its own, unless the address is in a private or loopback range:
blocking the proxy would take the site down for everybody.

Every desk action writes an append-only audit entry. The model refuses updates
and deletes, and the database revokes them too.

Rate limiting and blocking treat the cache as an optimisation. A cache outage
costs the limit; it does not take the site down or lock customers out of their
own money.

Found something? Email `security@nkenzapay.com` rather than opening an issue.

## Deployment

Runs under WSGI. `passenger_wsgi.py` is the entry point for cPanel's Python app
support, `config/wsgi.py` for anything else. Deployment notes for the live
environment are kept outside this repository.

Two capabilities need a long-lived process and degrade without one. Websockets
(Django Channels) fall back to polling in the chat, and Celery work (receipt and
export generation) runs inside the request. Both start working the moment an
ASGI process and a worker exist, with no code changes.

## Licence

Proprietary. All rights reserved.
