# Changelog

## 1.5.1 (2026-08-11)
- Added a test suite for both scripts (89 tests): the Vilnius day boundary and every
  scheduling path, beneficiary IBAN selection, invoice-id matching, purpose clipping,
  the ledger state machine including the unconfirmed-attempt block, token handling
  (asserting the token never reaches argv), transport failures, and the cancel gate
  (dry-run never issues a DELETE; a terminal or unreadable transfer is never deleted).
- CI no longer tolerates "no tests collected", and runs the suite a second time without
  pytest, so a green pipeline cannot be reached by losing the tests.

## 1.5.0 (2026-08-11)

Second review round.

**Duplicate payments**
- The ledger row is now written **before** the POST, not after. Previously a request that
  was sent but never answered (timeout, killed process) left a draft on the server that
  nothing recorded — and `GET /transfers` does not list unsigned drafts, so neither dedup
  source could see it. A retry, e.g. an hourly cron, created a second signable draft for
  the same invoice. An unanswered attempt is now kept as `unknown`, blocks further runs,
  and prints instructions to check the Paysera app. A definite HTTP 4xx is recorded as
  `failed` and does not block a legitimate retry.
- `--force` with an invoice id now says on stderr that the duplicate check was skipped
  entirely. It was the only bypass in the script that stayed silent.
- The invoice-id match against a transfer purpose requires token boundaries and a minimum
  id length. `12` or `A1` previously matched unrelated purposes and refused good payments.

**Scheduling**
- The future-dated schedule line printed the day in UTC while every other date decision
  used Vilnius, so a run between midnight and 03:00 printed both the execution day and the
  deadline day one day early — the deadline appearing already past.
- `--perform-at +Nh` is now held inside today like every other same-day path. `+6h` at
  20:00 previously rolled `operation_date` to tomorrow, hiding the transfer from the
  mobile app, which is the opposite of what the help text promised.
- The "no window left" messages said "past 23:00" when the cutoff is 10 minutes earlier.

**Validation**
- `--currency` is normalised and checked once, so `--currency eur` no longer selects the
  instant rail while sending `"eur"` in the payload and the ledger.
- `--charge-type` accepts only `sha` and `our`, the two values the API takes.

**Publication checks**
- `scripts/validate.py` now scans the whole repository, not just `plugins/`. The
  marketplace catalogue duplicates each skill description verbatim, and the README is just
  as public; neither was being checked.
- Added detection of internal hostnames on non-Paysera domains (`intranet`/`internal`
  labels, `.local`/`.lan`/`.corp`/... suffixes) and of issue-tracker/forge URLs. The host
  allowlist alone could only see `paysera.*`.
- CONTRIBUTING.md now states exactly what the automated check covers and what only a human
  can catch, and its test command matches CI.

## 1.4.0 (2026-08-10)

Security and correctness fixes from the pre-publication review.

**Security**
- The PAT is no longer passed to `curl` as a command-line argument, where any local user
  could read it from `ps` or `/proc/<pid>/cmdline` for the duration of every request. It
  now goes to curl on stdin as a config file. A POST body goes through a `0600` temp file.
- The token is rejected if it contains a quote, backslash or newline (config-file quoting).
- The ledger directory is created `0700`, and the ledger temp file is `0600` **before**
  being renamed into place — previously the finished file was briefly world-readable.

**Scheduling**
- Without tzdata the tool used plain UTC in place of Europe/Vilnius, which is 2-3 hours
  off. A run after ~21:00 Vilnius produced a `perform_at` on the **next day**, making the
  transfer web-bank-only — the opposite of what `--today` promises. There is now a
  built-in EET/EEST fallback following the EU DST rule, and a note when it is in use.
- Past 23:00 Vilnius there is no same-day window left; `--today` now falls back to ASAP
  (which keeps `operation_date` on today) instead of returning a next-day timestamp.
- `compute_schedule()` now judges "is this date past?" in Vilnius, as `parse_perform_at()`
  already did. The two disagreed between 21:00 and midnight UTC.

**Reliability**
- Every request now has a 30-second timeout and checks curl's exit status. A missing curl,
  a timeout, or a non-zero exit is reported on stderr instead of being indistinguishable
  from an empty API response.
- An incomplete live duplicate check now warns loudly before falling back to the ledger.
- A transfer whose `purpose.details` is null no longer crashes the duplicate check, and a
  null `amount` no longer crashes `cancel-payment.py`.

**Validation and messaging**
- A purpose over the 140-char SEPA limit now warns and shows exactly what was dropped —
  silent truncation could remove the invoice reference the payee reconciles on.
- `--amount` rejects `Infinity`, `NaN`, more than 2 decimal places, and implausibly large
  values instead of passing them to the API.
- An empty `--iban` now gives a clear error instead of an `IndexError`.
- An unmapped `--buyer-code` used with an explicit `--payer` now prints a NOTE saying the
  wrong-account guard did not run.
- Corrected the `--also-iban` help, which claimed those IBANs are never paid (a Paysera
  IBAN there does become the payee), and the `--perform-at` help, which misstated both the
  default and whether same-day is allowed.

**Removed**
- `phone_utils.py` and its tests: nothing in the plugin referenced them.

## 1.3.2 (2026-08-10)
- First public release on https://github.com/paysera/skills.
- Documented Personal Access Token creation against the public PAT API, replacing the
  previous pointer to an internal-only tool.

## 1.3.1 (2026-07-08)
- Fixed `list_transfers` in create-payment.py: the live dedup cross-check was querying the wrong direction — `debit_account_number` returns INCOMING transfers, so it never saw an executed OUTGOING duplicate. Now uses `credit_account_number` (accounting semantics: the payer is the credit side).
- Replaced the broken server-side cursor pagination (`_metadata.cursors.after` re-returns the same window then reports has_next=false, silently truncating) with `offset` pagination plus id-based dedupe, so a busy payer account is no longer capped at one page.
- SKILL.md: corrected the `GET /transfers` filter documentation to the verified direction semantics (credit = payer/outgoing, debit = beneficiary/incoming).

## 1.3.0 (2026-07-03)
- SEPA-zone detection, purpose-length trimming, exact-match buyer-name fallback, and extra
  non-blocking transfer states in create-payment.py.
