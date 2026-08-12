#!/usr/bin/env python3
"""Create a (draft) Paysera transfer via the public Transfer API.

Uses the account-scoped `paysera-payments` Personal Access Token. That token has
`transfers:create` but NOT `transfers:sign`, so every transfer it creates is a
DRAFT — it is created and registered for signing but NOT executed. Money moves only
after the transfer is signed in the Paysera app (2FA). This tool therefore cannot, by
design, send money on its own.

After POSTing the transfer (which lands in the validation-only `new` state, invisible
for signing) the tool calls PUT /transfers/{hash}/register so it becomes visible for
MANUAL SIGNING in the Paysera app/UI (skip with --no-register). The register route
fixed the old "invisible draft" problem.

Safety: dry-run by default. It prints the exact payload and does nothing until you
re-run with --confirm.

Idempotency (avoid double-paying an invoice): pass --invoice-id. Before creating, the
tool checks two sources and REFUSES if any live/succeeded match exists (anything except
failed/rejected/canceled/expired; override with --force):
  1. a local ledger (~/.config/paysera-payments/ledger.json) of transfers it created
     for that invoice (live-status-checked); and
  2. the payer account's ACTUAL transfers since the invoice date (GET /transfers list,
     shipped 2026-06-18). An invoice can name MORE THAN ONE beneficiary account (e.g. a
     Luminor AND a SEB IBAN); a prior payment to EITHER is still "this invoice paid". So
     pass every account from the invoice via --iban + --also-iban (repeatable). The check
     pulls EVERY transfer to ANY of those IBANs over [invoice_date .. today], prints them
     for review, and BLOCKS on any whose amount matches OR whose purpose mentions the
     invoice id — catching a duplicate made manually in the app, to either bank.

Scheduling (signing window, mobile vs web): Paysera sets a per-transfer signing deadline
`max_execution_time`. Three regimes, all re-measured live 2026-06-30:
  * perform_at = LATER TODAY (--today, or --perform-at +Nh / today's date) -> the deadline
    is that exact same-day timestamp, so you get an intraday window (e.g. until 23:00). The
    transfer keeps operation_date = today, so it shows in BOTH the mobile app AND the web
    bank. THIS is the right choice for "pay today and sign on my phone now". (Verified: a
    same-day perform_at IS accepted and IS mobile-visible — the older "you can't have both a
    window and mobile" claim was wrong; it only tested ASAP and future-DAY.)
  * perform_at OMITTED (--advance / ASAP) -> due now. Deadline ~immediate: with urgency
    `urgent` (EUR SEPA-Instant default) max_execution_time == creation instant (≈0 s window);
    with normal priority it's a ~30-min grace. Mobile-visible, but you must sign on the spot.
    Prefer --today for a same-day payment you'll sign within hours.
  * perform_at = a FUTURE DAY (--perform-at +Nd / YYYY-MM-DD; +30d is the INVOICE default) ->
    the deadline is ~Vilnius-midnight at the start of perform_at, i.e. an ~N-day window. BUT
    a future operation_date renders only in the WEB BANK, not mobile, until that day — sign
    it in the web bank.
Default is context-aware: a payment WITH --invoice-id (invoice/bulk) defaults to +30d (long
web-bank window); a payment WITHOUT --invoice-id (ad-hoc/personal) defaults to TODAY (mobile-
signable). Override either with --today / --advance / --perform-at.

Token: read from ~/.config/paysera-payments/token (override with --token-file or
the PAYSERA_PAT env var).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import time
from decimal import Decimal, InvalidOperation


class _VilniusFallback(datetime.tzinfo):
    """Europe/Vilnius for hosts without tzdata (Python < 3.9, or a slim container).

    Vilnius is EET (UTC+2) in winter and EEST (UTC+3) in summer, switching on the EU
    rule: DST starts the last Sunday of March at 01:00 UTC and ends the last Sunday of
    October at 01:00 UTC. Hard-coding that is correct for as long as the EU keeps the
    rule; falling back to plain UTC is wrong by 2-3 hours all year, which silently moves
    a transfer's operation_date to the next day (see _end_of_today_epoch).
    """

    _STD = datetime.timedelta(hours=2)
    _DST = datetime.timedelta(hours=3)

    @staticmethod
    def _last_sunday(year, month):
        d = datetime.date(year, month, 31)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)

    def _wall_bounds(self, year):
        """The transitions as LOCAL WALL clock readings.

        Going in, 01:00 UTC reads 03:00 (EET) and jumps to 04:00 (EEST) — 03:00-04:00
        never happens. Coming out, 01:00 UTC reads 04:00 (EEST) and falls back to 03:00
        (EET) — 03:00-04:00 happens TWICE.
        """
        return (
            datetime.datetime.combine(self._last_sunday(year, 3), datetime.time(3)),
            datetime.datetime.combine(self._last_sunday(year, 10), datetime.time(4)),
        )

    def _utc_bounds(self, year):
        """The same two transitions as UTC readings: 01:00 UTC on both dates."""
        return (
            datetime.datetime.combine(self._last_sunday(year, 3), datetime.time(1)),
            datetime.datetime.combine(self._last_sunday(year, 10), datetime.time(1)),
        )

    def _is_dst(self, dt):
        """Is this LOCAL WALL time in summer? Used by utcoffset/dst/tzname.

        Only ever asked about wall clock readings, because fromutc() below is explicit
        and never round-trips through here. Relying on the default fromutc() was the old
        defect: it adds the STANDARD offset to the UTC value and asks dst() about THAT,
        so one boundary constant had to serve two different conventions and could not.
        The result was an hour that converted one hour late, and non-monotonically
        (01:30 UTC gave 04:30 while 02:00 UTC gave 04:00).
        """
        if dt is None:
            return False
        start, end = self._wall_bounds(dt.year)
        naive = dt.replace(tzinfo=None)
        if not (start <= naive < end):
            return False
        # The last hour before `end` is the ambiguous one — it occurs once as EEST and
        # again as EET. fold=1 names the second, standard-time pass (PEP 495).
        if naive >= end - datetime.timedelta(hours=1) and getattr(dt, "fold", 0):
            return False
        return True

    def fromutc(self, dt):
        """Convert a UTC reading to local time directly, in UTC terms.

        Explicit rather than inherited: the base implementation infers the offset by
        calling dst() back on a partially-converted value, which cannot be reconciled
        with the wall-clock convention utcoffset() needs.
        """
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        naive = dt.replace(tzinfo=None)
        start, end = self._utc_bounds(naive.year)
        if start <= naive < end:
            return dt + self._DST
        result = dt + self._STD
        if end <= naive < end + datetime.timedelta(hours=1):
            # This reading lands in the repeated hour; mark it as the second pass so
            # utcoffset() reports EET rather than EEST.
            result = result.replace(fold=1)
        return result

    def utcoffset(self, dt):
        return self._DST if self._is_dst(dt) else self._STD

    def dst(self, dt):
        return datetime.timedelta(hours=1) if self._is_dst(dt) else datetime.timedelta(0)

    def tzname(self, dt):
        return "EEST" if self._is_dst(dt) else "EET"


try:
    from zoneinfo import ZoneInfo

    _VILNIUS = ZoneInfo("Europe/Vilnius")
    _VILNIUS_IS_FALLBACK = False
except Exception:  # no zoneinfo/tzdata — use the hard-coded EU rule, never bare UTC
    _VILNIUS = _VilniusFallback()
    _VILNIUS_IS_FALLBACK = True


def _warn_tz_once():
    """Tell the operator when the timezone is being approximated rather than read from
    tzdata — the schedule printout depends on it."""
    if _VILNIUS_IS_FALLBACK:
        print(
            "NOTE: no tzdata on this host — using a built-in Europe/Vilnius rule "
            "(EET/EEST). Install tzdata (or Python 3.9+) for authoritative times.",
            file=sys.stderr,
        )

# Accounts the PAT is scoped to. The token will reject any other payer account.
# ── CONFIGURE THIS ─ replace the placeholders with the EVP account_number(s) your
# own token is scoped to (see the Paysera app → account number, the "EVP..." one,
# NOT the IBAN). The script refuses any payer not listed here, as a safety guard.
ALLOWED_ACCOUNTS = {
    "EVP0000000000001": "Company A (example — replace me)",
    "EVP0000000000002": "Company B (example — replace me)",
}

# --- Invoice BUYER (pirkėjas) -> payer account resolution -------------------
# Prevents paying an invoice from the WRONG account. The correct payer is whichever
# scoped account belongs to the invoice's BUYER (pirkėjas). Resolution is by company
# registration code (reliable). Names are only a soft cross-check, never the sole key
# (fuzzy: two entities that share a surname must be resolved by code, not by name).
# ── CONFIGURE THIS ─ add an entry ONLY with a registration code you have actually
# verified on a real invoice — NEVER guess/fabricate a code. Leave empty to always
# pass --payer explicitly.
BUYER_CODE_TO_ACCOUNT = {
    # "123456789": "EVP0000000000001",  # Company A, UAB (verified: invoice <nr>, <date>)
    # "987654321": "EVP0000000000002",  # Company B, UAB (verified: invoice <nr>, <date>)
}

# Fail-closed BUYER NAME -> payer account fallback. Used ONLY when the registration
# code did not resolve a payer — some documents never print the buyer's code, only
# the SELLER's (e.g. an insurer's "Mokėjimo pranešimas" payment reminder), so the
# LLM returns an empty or wrong buyer_code. Keys are _norm_buyer_name() output
# (UPPERCASE, punctuation stripped, whitespace collapsed). EXACT match only — NEVER
# fuzzy — and ONLY your own, unambiguous entities. Do NOT add ambiguous pairs here
# (two entities that share a surname); resolve those by code.
# ── CONFIGURE THIS ─ starts empty; add an exact, unambiguous buyer name only when a
# real invoice confirms it.
BUYER_NAME_TO_ACCOUNT = {
    # "COMPANY A UAB": "EVP0000000000001",  # verified <ref> (example — replace me)
}


def _norm_buyer_name(s):
    """Normalize a buyer name for EXACT allow-list matching: NFC-compose (so a
    decomposed Lithuanian diacritic like S+caron matches the precomposed Š),
    uppercase, strip punctuation, collapse whitespace. Conservative on purpose — only
    an exact normalized hit resolves a payer (never a fuzzy/substring match)."""
    s = unicodedata.normalize("NFC", s or "").upper()
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


TRANSFER_API = "https://api.paysera.com/public/transfer/rest/v1/transfers"
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/paysera-payments/token")
LEDGER_FILE = os.path.expanduser("~/.config/paysera-payments/ledger.json")

# Transfer states that mean "no payment exists / safe to (re)create". Anything NOT in
# this set (new, reserved, signed, processing, done, ...) blocks a duplicate.
NONBLOCKING_STATES = {"failed", "rejected", "canceled", "cancelled", "expired", "declined"}

# SEPA purpose-of-payment field max length (the API rejects longer with
# 'details_too_long'). Used to trim long invoice purposes on a word boundary.
PURPOSE_MAX = 140

# SEPA zone (EU + EEA + the non-EU SEPA members). A EUR transfer to a SEPA-zone
# beneficiary can use the instant rail; one to a non-SEPA country (UA, AM, GE, …)
# is a regular international SWIFT transfer (needs a BIC, not instant).
SEPA_COUNTRIES = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "IS",
    "LI",
    "NO",
    "CH",
    "MC",
    "SM",
    "GB",
    "VA",
    "AD",
    "GI",
    # Joined the SEPA schemes 2023-2024 (list last checked 2026-08-12).
    "AL",
    "MD",
    "MK",
    "ME",
}


def beneficiary_country(iban: str, bic: str | None) -> str | None:
    """Recipient ISO-2 country. A BIC's chars 5-6 are the country (reliable even for
    non-IBAN accounts); otherwise the IBAN's 2-letter prefix."""
    if bic and len(bic) >= 6 and bic[4:6].isalpha():
        return bic[4:6].upper()
    iban = (iban or "").replace(" ", "").upper()
    return iban[:2] if iban[:2].isalpha() else None


def _clip_purpose(text: str, limit: int = PURPOSE_MAX) -> str:
    """Trim the purpose to the SEPA limit on a word boundary.

    Clipping is LOUD on purpose: the payee usually reconciles on an exact string, and the
    invoice reference is typically at the END of the purpose — precisely what a silent
    trim would drop. See SKILL.md "Payment purpose".
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    kept = (cut[:sp] if sp > limit * 0.6 else cut).rstrip()
    print(
        f"WARNING: purpose is {len(text)} chars, over the {limit}-char SEPA limit — "
        f"trimmed to {len(kept)}.\n"
        f"         DROPPED: {text[len(kept):].strip()!r}\n"
        f"         If the invoice reference was in the dropped tail, the payee may not "
        f"match this payment. Shorten --purpose yourself so the reference survives.",
        file=sys.stderr,
    )
    return kept


HTTP_TIMEOUT = 30  # seconds per request — an unanswered API must not hang a cron run


class HttpError(RuntimeError):
    """A transport-level failure (curl missing, timed out, or non-zero exit) — as
    opposed to an HTTP error status, which comes back as a code."""


def _validate_token(token):
    """The token is passed to curl through a config file, where it is delimited by
    double quotes. Refuse anything that could break out of that quoting."""
    if not token:
        sys.exit("ERROR: empty PAT.")
    if any(c in token for c in '"\\\r\n'):
        sys.exit("ERROR: PAT contains a quote, backslash or newline — refusing to use it.")
    return token


ADDRESS_MAX = 70  # the API rejects longer with 'mapper_beneficiary_address_too_long'


def _clip_address(text, limit=ADDRESS_MAX):
    """Trim the beneficiary address line, saying what was lost.

    Same rule as _clip_purpose: an address that quietly loses its city, postcode or
    country can get an international wire refused, so the operator has to be told.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    kept = text[:limit].rstrip()
    print(
        f"WARNING: beneficiary address is {len(text)} chars, over the {limit}-char limit "
        f"— trimmed to {len(kept)}.\n"
        f"         DROPPED: {text[len(kept):].strip()!r}\n"
        f"         If that held the city, postcode or country, the transfer may be "
        f"refused. Shorten --beneficiary-address yourself.",
        file=sys.stderr,
    )
    return kept


def _check_token_file_mode(path):
    """Refuse a token file that group or other can read.

    The docs say this file is 0600, but nothing used to make it so: a plain
    `curl ... > ~/.config/paysera-payments/token` under the usual umask 022 leaves it
    0644, and then every local user can read a PAT with transfers:create and
    transfers:cancel. Unlike the argv exposure fixed in 1.4.0 — which lasted for the
    length of one request — a world-readable token file is permanent.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return  # the open() below reports it properly
    if mode & 0o077:
        sys.exit(
            f"ERROR: {path} is mode {mode:04o} — readable by other users on this host.\n"
            f"  A PAT in a world- or group-readable file is exposed to every local user.\n"
            f"  Fix it and re-run:  chmod 600 {path}"
        )


def read_token(path):
    tok = os.environ.get("PAYSERA_PAT")
    if tok:
        return _validate_token(tok.strip())
    _check_token_file_mode(path)
    try:
        with open(path) as f:
            return _validate_token(f.read().strip())
    except OSError as e:
        sys.exit(f"ERROR: cannot read PAT ({e}). Set PAYSERA_PAT or pass --token-file.")


def _curl(method, url, token, payload=None, timeout=HTTP_TIMEOUT):
    """Run one curl request and return (http_code, parsed_body).

    The Authorization header is fed to curl on STDIN as a config file (`-K -`), never as
    a command-line argument: argv is world-readable on Linux via `ps auxww` and
    /proc/<pid>/cmdline, so a token in argv is exposed to every local user for the
    lifetime of the call. A JSON body goes through a 0600 temp file for the same reason
    stdin is already taken.

    Raises HttpError on a transport failure so the caller can report it rather than
    mistaking it for an empty API response.
    """
    cmd = ["curl", "-sS", "-X", method, url, "-K", "-", "-w", "\nHTTP:%{http_code}"]
    tmp = None
    try:
        if payload is not None:
            fd, tmp = tempfile.mkstemp(prefix="paysera-payload-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)
            cmd += ["-H", "Content-Type: application/json", "--data-binary", "@" + tmp]
        try:
            out = subprocess.run(
                cmd,
                input=f'header = "Authorization: Bearer {token}"\n',
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise HttpError("curl is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise HttpError(f"no response within {timeout}s")
        if out.returncode != 0:
            raise HttpError(
                f"curl exited {out.returncode}: {(out.stderr or '').strip()[:200] or 'no stderr'}"
            )
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    txt = out.stdout
    code = txt.split("\nHTTP:")[-1].strip()
    body = txt.split("\nHTTP:")[0]
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, body


def http_json(method, url, token, payload=None):
    """_curl, with transport failures reported and reduced to the code "ERR" so callers
    can treat them as "did not get an answer" rather than "the API said no"."""
    try:
        return _curl(method, url, token, payload)
    except HttpError as e:
        print(f"WARNING: {method} {url.split('?')[0]} failed — {e}", file=sys.stderr)
        return "ERR", {"error": str(e)}


def _lock_path():
    """Derived from LEDGER_FILE at call time, not import time: the lock has to follow the
    ledger when LEDGER_FILE is redirected (tests, --token-file-style overrides)."""
    return os.path.join(os.path.dirname(LEDGER_FILE), ".lock")


try:
    import fcntl

    _HAVE_FLOCK = True
except ImportError:  # non-POSIX host
    _HAVE_FLOCK = False

_lock_fd = None
_lock_depth = 0


@contextlib.contextmanager
def ledger_lock():
    """Serialise the whole duplicate-check-and-record section against other runs.

    Without this the ledger's read-append-write is a lost-update race: two runs (a cron
    job and a manual run, or two agent sessions) both read the same ledger, and the one
    that writes second erases the other's row. The erased row is usually the write-ahead
    `pending` one — and since GET /transfers cannot list unsigned drafts, that row is the
    ONLY record of the draft. Losing it lets a later run create a second signable draft
    for the same invoice, which is exactly the double payment 1.5.0 set out to stop.

    The lock spans the check AND the write-ahead append, not just the file write, because
    two runs that both pass the duplicate check before either records anything race the
    same way. It is taken NON-BLOCKING and fails closed: a concurrent run is told to wait
    rather than queueing behind a network call. flock is released by the kernel when the
    process dies, so a killed run cannot leave a stale lock behind.

    Re-entrant: nested `with` blocks in the same process share the one descriptor
    (flock is per open-file-description, so a second open would deadlock against itself).
    """
    global _lock_fd, _lock_depth
    if not _HAVE_FLOCK:
        # Better to run unserialised than not at all, but say so — the guarantee above
        # does not hold here.
        print(
            "WARNING: no fcntl on this platform — the ledger is not locked. Do not run "
            "two payments concurrently.",
            file=sys.stderr,
        )
        yield
        return
    if _lock_depth == 0:
        _ensure_config_dir()
        path = _lock_path()
        _lock_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(_lock_fd)
            _lock_fd = None
            sys.exit(
                f"ERROR: another paysera-payments run holds {path}.\n"
                f"  Two concurrent runs can each miss the other's transfer and pay the "
                f"same invoice twice, so this one is stopping.\n"
                f"  Wait for the other run to finish and re-run."
            )
    _lock_depth += 1
    try:
        yield
    finally:
        _lock_depth -= 1
        if _lock_depth == 0 and _lock_fd is not None:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
            _lock_fd = None


def _ensure_config_dir():
    """The ledger and the lock live beside the token, so the directory is 0700.

    os.makedirs(mode=...) applies the mode only to a directory it CREATES; with
    exist_ok=True it silently leaves an existing one alone. In the normal sequence the
    user creates this directory first, to hold the token, so it usually already exists
    with the umask's 0755 — hence the explicit chmod.
    """
    d = os.path.dirname(LEDGER_FILE)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if stat.S_IMODE(os.stat(d).st_mode) & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass


def load_ledger():
    try:
        with open(LEDGER_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _new_attempt_id():
    """A unique id for one create attempt, so a pending ledger row can be found and
    updated after the POST returns (or fails to)."""
    return f"{int(time.time())}-{os.getpid()}-{os.urandom(4).hex()}"


def update_ledger(attempt_id, **fields):
    """Merge `fields` into the ledger row with this attempt_id, rewriting the file."""
    with ledger_lock():
        data = load_ledger()
        for e in data:
            if e.get("attempt_id") == attempt_id:
                e.update(fields)
                break
        else:
            return False
        _write_ledger(data)
        return True


def _write_ledger(data):
    # The ledger holds IBANs, amounts and invoice numbers. The directory is 0700 and the
    # TEMP file is 0600 BEFORE it is renamed into place — setting the mode after
    # os.replace() leaves a window where the finished file is world-readable.
    _ensure_config_dir()
    tmp = LEDGER_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LEDGER_FILE)


def append_ledger(entry):
    with ledger_lock():
        data = load_ledger()
        data.append(entry)
        _write_ledger(data)


def _transfer_items(doc):
    """Extract the list of transfer rows from a GET /transfers response, robust to the
    container key (items / transfers / a bare list)."""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("items", "transfers", "data"):
            if isinstance(doc.get(key), list):
                return doc[key]
    return []


def list_transfers(token, payer_account, created_from, max_pages=50):
    """List the payer account's OUTGOING transfers since `created_from` (epoch) via
    the `GET /transfers` filter (added 2026-06-18), with OFFSET pagination so a
    busy account is NOT capped at the default page size. Returns [] on any
    error/unknown shape so the caller falls back to the ledger check rather than
    crashing.

    DIRECTION PARAM (fixed 2026-07-08): the API uses ACCOUNTING semantics —
    `credit_account_number=X` returns transfers where X is the PAYER (outgoing;
    paying out credits the asset account), `debit_account_number=X` returns INCOMING
    ones. Verified empirically on a live payer account: `credit_…` returned a full
    page of rows with payer=X while `debit_…` returned none. Until this fix the live
    dedup cross-check was reading the INCOMING list, so it could never see an executed
    outgoing duplicate.

    PAGINATION (fixed 2026-07-08): the cursor `after` param is BROKEN server-side —
    following `_metadata.cursors.after` returns the SAME date window again (mirrored
    order) and then reports has_next=false, silently truncating the list (verified on
    a 256-row window: cursor walk stopped at 06-16 while 58 July rows existed).
    `offset` works correctly (0/100/200 → full 256, ranges 06-01..06-16 / 06-16..07-01
    / 07-01..07-08). The earlier "verified 39 vs old 20" cursor check never exercised
    page 2 (39 < limit=100), so the breakage went unseen. Rows are deduped by id —
    page boundaries can shift while new transfers land.

    PAGE CAP: at most `max_pages` pages are read. Reaching that cap is REPORTED on
    stderr (see the `else` below) — a truncated list that reads as a complete one is the
    same defect class as a scan window that disagrees with its own report.

    LIMITATION (verified 2026-06-29): this endpoint returns only EXECUTED/terminal
    transfers (done/revoked/failed/rejected); it does NOT list unsigned drafts
    (new/registered/signed), and its `status` query param is ignored. So the live
    cross-check catches an already-EXECUTED duplicate, but NOT one left as an unsigned
    draft — the tool's own drafts are deduped via the LEDGER instead (find_blocking
    source 1); a duplicate created manually in the app and left unsigned stays invisible
    here. It also cannot see a payment made OUTSIDE the Paysera wallet (e.g. from the
    company's real bank account) — that is fundamentally out of this API's reach.
    """
    page_size = 100
    base = (
        f"{TRANSFER_API}?credit_account_number={payer_account}"
        f"&created_date_from={int(created_from)}&limit={page_size}"
    )
    items, seen_ids = [], set()
    for page in range(max_pages):
        code, doc = http_json("GET", f"{base}&offset={page * page_size}", token)
        if code != "200":
            # Say so: an incomplete live list silently weakens the duplicate check, and
            # the caller falls back to the ledger (which cannot see app-made payments).
            print(
                f"WARNING: live duplicate check INCOMPLETE (page {page + 1} returned "
                f"{code}) — falling back to the ledger only. A duplicate made in the "
                f"Paysera app may not be detected. Re-run, or verify manually.",
                file=sys.stderr,
            )
            break
        page_items = _transfer_items(doc)
        if not page_items:
            break
        for t in page_items:
            tid = t.get("id") if isinstance(t, dict) else None
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            items.append(t)
        meta = doc.get("_metadata") if isinstance(doc, dict) else None
        total = meta.get("total") if isinstance(meta, dict) else None
        if len(page_items) < page_size:
            break
        if isinstance(total, int) and (page + 1) * page_size >= total:
            break
    else:
        # Reached only when the loop ran out of pages: every honest end (empty page,
        # short page, total reached) and the HTTP-error path all `break`. Without this,
        # hitting the cap returned a partial list indistinguishable from a complete one —
        # the same shape as the defects fixed in 1.7.1 and 1.7.2, a partial check
        # reporting as complete. Raising the cap would not fix it; the silence is the bug.
        print(
            f"WARNING: live duplicate check INCOMPLETE — stopped at the {max_pages}-page "
            f"cap after reading {len(items)} transfers, and there are more in this "
            f"window. A duplicate older than those may not be detected.\n"
            f"         Narrow the window with --invoice-date, or verify manually.",
            file=sys.stderr,
        )
    return items


# An IBAN's canonical form has no separators. People paste them from invoices with
# spaces, hyphens or dots, and every comparison in this file has to see through all three.
IBAN_SHAPE = re.compile(r"\A[A-Z]{2}[0-9A-Z]{13,32}\Z")
ACCOUNT_SEPARATORS = re.compile(r"[\s.\-‐‑‒–—―]")


def _norm_iban(s):
    """Canonical form for COMPARING account numbers.

    Strips every separator, not just spaces. This helper drives three decisions — which
    listed IBAN gets paid (via is_paysera_iban), the duplicate check's candidate set, and
    the de-duplication of the listed IBANs — so a spelling it cannot see through silently
    breaks all three. 1.7.1 stripped separators at ONE call site instead of here, which
    left `--also-iban LT60-3500-…` invisible to the payee rule and to the duplicate check
    while the run still reported it as scanned.
    """
    return ACCOUNT_SEPARATORS.sub("", (s or "")).upper()


def clean_account(value):
    """Canonical form for the account number actually SENT, plus a note if it changed.

    Deliberately narrower than _norm_iban: separators are removed only when what remains
    is a well-formed IBAN. A national account number that is not an IBAN (the Armenian
    "2050…" format) keeps whatever punctuation it was given, because there is no rule
    saying its separators are decorative.
    """
    raw = (value or "").strip().upper()
    squeezed = raw.replace(" ", "")
    if IBAN_SHAPE.match(squeezed):
        return squeezed, None
    stripped = ACCOUNT_SEPARATORS.sub("", squeezed)
    if stripped != squeezed and IBAN_SHAPE.match(stripped):
        return stripped, f"{raw} -> {stripped}"
    return squeezed, None


def clean_accounts(primary, also):
    """Normalise every listed account BEFORE any of them is compared or chosen.

    Must run ahead of select_beneficiary_iban(): that is where a Paysera IBAN wins the
    payment, and an unnormalised one loses it while the printed reason says no Paysera
    IBAN was listed at all.
    """
    notes = []
    cleaned_primary, note = clean_account(primary)
    if note:
        notes.append(note)
    cleaned_also = []
    for account in also or []:
        cleaned, note = clean_account(account)
        cleaned_also.append(cleaned)
        if note:
            notes.append(note)
    if notes:
        print(
            "NOTE: an IBAN has no separators; using " + "; ".join(notes),
            file=sys.stderr,
        )
    return cleaned_primary, cleaned_also


def is_paysera_iban(s):
    """True if `s` is a Paysera IBAN — Lithuanian, bank (payment institution) code
    35000, i.e. LTkk35000…. Paysera→Paysera transfers are instant and free, so when an
    invoice lists a Paysera account alongside others we always pay to it."""
    return bool(re.match(r"^LT\d{2}35000", _norm_iban(s)))


def select_beneficiary_iban(primary, also):
    """Choose which beneficiary IBAN to actually pay, from all the IBANs an invoice lists.

    Rule:
      * If ANY listed IBAN is a Paysera account (LTkk35000…) → ALWAYS pay to that one,
        even if it was passed as --also-iban.
      * Otherwise → pay to the FIRST listed IBAN (the one passed as --iban).

    Returns (chosen_iban, other_ibans, reason). `other_ibans` keeps the rest (for the
    duplicate check). De-dupes by normalized IBAN while preserving the given order."""
    seen, ordered = set(), []
    for ib in [primary] + list(also or []):
        n = _norm_iban(ib)
        if n and n not in seen:
            seen.add(n)
            ordered.append(ib)
    if not ordered:
        sys.exit("ERROR: no beneficiary IBAN given — --iban must be a real IBAN, not empty.")
    paysera = [ib for ib in ordered if is_paysera_iban(ib)]
    if paysera:
        chosen, reason = (
            paysera[0],
            "Paysera IBAN (bank code 35000) — always pay Paysera (instant & free)",
        )
    else:
        chosen, reason = ordered[0], "no Paysera IBAN listed — paying the first IBAN on the invoice"
    others = [ib for ib in ordered if _norm_iban(ib) != _norm_iban(chosen)]
    return chosen, others, reason


# An invoice id shorter than this is too generic to match on inside free text — "12"
# appears in half of all payment purposes. Below it, only the amount check applies.
MIN_INVOICE_ID_MATCH_LEN = 4


def _purpose_quotes_invoice(purpose, invoice_id):
    """True if `purpose` quotes `invoice_id` as a distinct token.

    A bare substring test made short ids ("12", "A1") match unrelated purposes and refuse
    perfectly good payments, which pushes the operator towards --force — and --force
    disables the whole duplicate check, not just this one rule.
    """
    inv = str(invoice_id or "").strip()
    if len(inv) < MIN_INVOICE_ID_MATCH_LEN:
        return False
    # Boundaries are non-alphanumeric rather than \b: an id like "EX000123" sits next to
    # punctuation far more often than whitespace, and \b would also fire mid-token.
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(inv)}(?![0-9A-Za-z])", purpose or "", re.I) is not None


# How far back the live cross-check looks when --invoice-date is not given. Unbounded
# (from_epoch=0) meant the FULL account history, where the amount rule alone blocks: a
# supplier paid the same EUR 250.00 every month refused every month after the first, with
# a message naming the wrong invoice. The only escape was --force, which switches the
# whole duplicate check off — so an over-eager block trains the operator out of the guard
# that stops a real double payment. Pass --invoice-date for an exact window.
DEFAULT_LOOKBACK_DAYS = 90

INVOICE_DATE_FORMAT = "%Y-%m-%d"


def parse_invoice_day(spec):
    """The calendar date in `spec` (YYYY-MM-DD), or None if it is not that format."""
    try:
        return datetime.datetime.strptime(spec, INVOICE_DATE_FORMAT).date()
    except ValueError:
        return None


def parse_invoice_date(spec):
    """Epoch for the start of `spec` (YYYY-MM-DD) in UTC, or None if it is not that
    format. This is a scan boundary, so UTC is fine — a day of grace is subtracted from
    it anyway. Do NOT compare it against "now" to decide what day it is: see
    invoice_date_error()."""
    day = parse_invoice_day(spec)
    if day is None:
        return None
    return int(
        datetime.datetime(
            day.year, day.month, day.day, tzinfo=datetime.timezone.utc
        ).timestamp()
    )


def invoice_date_error(spec):
    """Return the error message for --invoice-date, or None when it is usable.

    A pure function of the value and the clock, so the boundary can be tested against a
    frozen clock rather than against whatever time CI happens to run at.
    """
    if not spec:
        return None
    day = parse_invoice_day(spec)
    if day is None:
        return (
            f"ERROR: --invoice-date {spec!r} is not YYYY-MM-DD.\n"
            f"  It sets the period the duplicate check scans, so it is not guessed — "
            f"a day-first date like 06/07/2026 is ambiguous.\n"
            f"  Write it as 2026-07-06, or omit it to scan the last "
            f"{DEFAULT_LOOKBACK_DAYS} days."
        )
    # Compare CALENDAR DAYS in Vilnius, never an epoch against "now". parse_invoice_date()
    # returns UTC midnight, which for the first 2-3 hours of a Vilnius day is still in the
    # future — so an epoch comparison rejected TODAY every night, in a message that then
    # printed today's Vilnius date as the reason it was not today. Same UTC-against-Vilnius
    # mismatch compute_schedule() documents for its own date decisions.
    today = _vilnius_today()
    if day > today:
        return (
            f"ERROR: --invoice-date {spec} is in the future (today is {today} in "
            f"Vilnius).\n"
            f"  It is the date the invoice was ISSUED, and it sets the start of the "
            f"duplicate scan — a future date scans an empty period and then reports "
            f"no prior payments, which looks like an all-clear.\n"
            f"  Check the year, or omit it to scan the last "
            f"{DEFAULT_LOOKBACK_DAYS} days."
        )
    return None


def scan_window(invoice_date):
    """Return (from_epoch, human description) for the live duplicate scan.

    ONE source of truth for the window, used both to run the scan and to report it. They
    were computed separately, and disagreed whenever --invoice-date failed to parse: the
    scan quietly fell back to the default window while stdout said "since <the malformed
    value>". A money-safety check whose report is less reliable than the check itself is
    worse than one that simply stops, which is why main() now refuses a bad date outright
    — but the two must be incapable of drifting regardless.
    """
    if invoice_date:
        parsed = parse_invoice_date(invoice_date)
        if parsed is not None:
            # 1-day grace before the issue date.
            return parsed - 86400, f"since {invoice_date}"
    return (
        int(time.time()) - DEFAULT_LOOKBACK_DAYS * 86400,
        f"over the last {DEFAULT_LOOKBACK_DAYS} days (no --invoice-date given)",
    )


def find_blocking(
    invoice_id, token, payer=None, ibans=None, amount=None, invoice_date=None, currency=None
):
    """Return (blocking, seen_to_ibans).

    `blocking` = [(hash, status, source, why)] that block re-creating a payment for this
    invoice. `seen_to_ibans` = [(hash, status, amount, iban, purpose)] — EVERY transfer
    to any candidate IBAN in the period, for human review (printed by the caller).

    Two block sources, deduped by hash:
      1. LEDGER — prior transfers this tool recorded for `invoice_id` (live-checked).
      2. LIVE LIST — the payer account's ACTUAL transfers since the invoice date to ANY
         of the beneficiary's accounts. An invoice can list several IBANs (Luminor AND
         SEB); a payment to EITHER means "this invoice already paid". Pass them all via
         `ibans` (a set). A transfer to a candidate IBAN BLOCKS when its amount matches
         OR its purpose mentions the invoice id. All transfers to those IBANs (whatever
         the amount) are also returned in `seen_to_ibans` so a human can eyeball "what
         was it for". Uses GET /transfers (shipped 2026-06-18); best-effort.

    A transfer blocks unless its status is in NONBLOCKING_STATES (failed/rejected/...).
    """
    blocking = {}
    seen_to_ibans = []

    # 1. Ledger-recorded transfers for this invoice.
    for e in load_ledger():
        if str(e.get("invoice_id")) != str(invoice_id):
            continue
        h = e.get("transfer_hash")
        state = e.get("state")
        if not h:
            # A create attempt with no hash: either it never got an answer (state
            # pending/unknown — a draft MAY exist and is invisible to GET /transfers), or
            # the API definitely refused it (state failed — safe to retry). Only the
            # latter is safe to ignore.
            if state in ("pending", "unknown"):
                key = e.get("attempt_id") or f"attempt@{e.get('created_at')}"
                blocking[key] = (
                    key,
                    f"UNCONFIRMED create attempt at {e.get('created_at_iso')} "
                    f"(HTTP {e.get('http_code', 'no answer')}) — check the Paysera app",
                    "ledger",
                    "this tool recorded an attempt for this invoice id",
                )
            continue
        code, doc = http_json("GET", f"{TRANSFER_API}/{h}", token)
        if code == "200" and isinstance(doc, dict) and doc.get("id"):
            st = (doc.get("status") or "").lower()
            if st not in NONBLOCKING_STATES:
                blocking[h] = (h, doc.get("status"), "ledger", "recorded for this invoice id")
        else:
            # Could not confirm it's dead -> be conservative, treat as blocking.
            blocking[h] = (
                h,
                f"unknown (HTTP {code})",
                "ledger",
                "recorded for this invoice id; its live status could not be read",
            )

    # 2. Live cross-check: same payer, any candidate beneficiary IBAN, over the whole
    #    period from (invoice issue date - 1d grace) to now — regardless of who created
    #    the transfer (app or tool).
    cand = {_norm_iban(i) for i in (ibans or []) if i}
    if payer and cand:
        # Never 0 (= the full account history): see DEFAULT_LOOKBACK_DAYS. Computed by
        # the same helper main() reports from, so the two cannot disagree.
        from_epoch, _ = scan_window(invoice_date)
        for t in list_transfers(token, payer, from_epoch):
            if not isinstance(t, dict):
                continue
            ben = t.get("beneficiary") or {}
            # IBAN location varies by beneficiary type: bank → beneficiary.bank_account.iban,
            # paysera → beneficiary.iban (verified against the live list). Check both.
            t_iban = _norm_iban((ben.get("bank_account") or {}).get("iban") or ben.get("iban"))
            if t_iban not in cand:
                continue
            t_amt = (
                ((t.get("amount") or {}).get("amount"))
                if isinstance(t.get("amount"), dict)
                else None
            )
            # `or ""` not a .get default: the key can be present with a null value, and
            # the default only applies when the key is absent.
            purpose = (
                ((t.get("purpose") or {}).get("details") or "")
                if isinstance(t.get("purpose"), dict)
                else ""
            )
            st = (t.get("status") or "").lower()
            h = t.get("id")
            seen_to_ibans.append((h, t.get("status"), t_amt, t_iban, purpose))
            # Block if amount matches OR the invoice id is quoted in the purpose.
            # The amount must match in the SAME currency — 100.00 USD is not a duplicate
            # of 100.00 EUR.
            t_ccy = (
                ((t.get("amount") or {}).get("currency") or "")
                if isinstance(t.get("amount"), dict)
                else ""
            )
            amount_match = False
            if amount is not None and t_amt is not None:
                try:
                    same_amount = Decimal(str(t_amt)) == Decimal(str(amount))
                except InvalidOperation:
                    same_amount = False
                # An absent currency on either side is treated as "cannot rule it out",
                # keeping the check fail-safe rather than fail-open.
                same_currency = not currency or not t_ccy or t_ccy.upper() == currency.upper()
                amount_match = same_amount and same_currency
            id_match = _purpose_quotes_invoice(purpose, invoice_id)
            if (
                h
                and (amount_match or id_match)
                and st not in NONBLOCKING_STATES
                and h not in blocking
            ):
                # Which rule fired matters to the operator: an invoice id quoted in the
                # purpose is near-certain, while an equal amount alone is circumstantial
                # (a recurring supplier charges the same sum every month).
                why = (
                    "its purpose quotes this invoice id"
                    if id_match
                    else "SAME AMOUNT only — the purpose does not mention this invoice"
                )
                blocking[h] = (h, t.get("status"), "live-list", why)

    return list(blocking.values()), seen_to_ibans


def _vilnius_today():
    """Today's date in Europe/Vilnius (the timezone Paysera uses for operation_date /
    the day boundary that decides mobile visibility)."""
    return datetime.datetime.now(_VILNIUS).date()


def _end_of_today_epoch():
    """Latest SAME-DAY signing deadline: today 23:00 Europe/Vilnius, or None if that has
    effectively passed.

    Keeping the deadline inside today is the whole point — operation_date must stay on
    today's Vilnius date for the transfer to appear in the mobile app. Late in the
    evening there is no same-day window left to give, so this returns None and the caller
    falls back to ASAP (which also has operation_date = today) rather than handing back a
    timestamp that has silently rolled into tomorrow.
    """
    now = int(time.time())
    end = datetime.datetime.now(_VILNIUS).replace(hour=23, minute=0, second=0, microsecond=0)
    epoch = int(end.timestamp())
    return epoch if epoch > now + 600 else None


def _vilnius_midnight_epoch():
    """Epoch of the next Vilnius midnight — the boundary that moves operation_date."""
    tomorrow = _vilnius_today() + datetime.timedelta(days=1)
    return int(
        datetime.datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_VILNIUS
        ).timestamp()
    )


def parse_perform_at(spec):
    """Resolve --perform-at to a FUTURE epoch — SAME-DAY allowed.

    perform_at is the signing deadline (`max_execution_time` == this timestamp for a same-day
    time; ~Vilnius-midnight-before for a future day). A LATER-TODAY time keeps operation_date =
    today, so the transfer shows in BOTH the mobile app and the web bank; a FUTURE DAY is
    web-bank-only until that day. Only a PAST/near-instant time is rejected (use --advance for
    sign-on-the-spot ASAP). Accepts: YYYY-MM-DD | +Nd | +Nh | epoch seconds | (default) +30d.
    """
    now = int(time.time())
    if not spec:
        return now + 30 * 86400
    if spec.isdigit():
        epoch = int(spec)
    elif spec.startswith("+") and spec[1:-1].isdigit() and spec[-1] in "dh":
        n = int(spec[1:-1])
        epoch = now + n * (86400 if spec[-1] == "d" else 3600)
        # +Nh is the one same-day spelling that could silently cross midnight (e.g. +6h at
        # 20:00), which moves operation_date to tomorrow and hides the transfer from the
        # mobile app. Every other same-day path is held inside today by
        # _end_of_today_epoch(); hold this one too, and say so.
        if spec[-1] == "h":
            end_today = _end_of_today_epoch()
            if end_today is not None and epoch > end_today:
                print(
                    f"NOTE: {spec} would land tomorrow (Vilnius), which makes the transfer "
                    f"web-bank-only. Holding the deadline at 23:00 tonight so it stays "
                    f"signable in the mobile app. Pass an explicit date if you really want "
                    f"a future day.",
                    file=sys.stderr,
                )
                epoch = end_today
            elif end_today is None and epoch > _vilnius_midnight_epoch():
                # No same-day window left to clamp to, so the deadline genuinely falls
                # tomorrow. Say so instead of silently changing where it can be signed.
                print(
                    f"NOTE: it is nearly midnight in Vilnius, so {spec} lands tomorrow — "
                    f"operation_date moves to the next day and the transfer will be "
                    f"signable in the WEB BANK ONLY, not the mobile app. Use --advance to "
                    f"sign right now instead.",
                    file=sys.stderr,
                )
    else:
        try:
            d = datetime.datetime.strptime(spec, "%Y-%m-%d").date()
        except ValueError:
            sys.exit("ERROR: --perform-at must be YYYY-MM-DD, +Nd, +Nh, or epoch seconds")
        today = _vilnius_today()
        if d < today:
            sys.exit(f"ERROR: --perform-at {d} is in the past. Pick today or a future date.")
        # Today's date -> latest same-day deadline; a future day -> noon UTC inside it.
        if d == today:
            epoch = _end_of_today_epoch()
            if epoch is None:
                sys.exit(
                    f"ERROR: --perform-at {d} is today, but less than 10 minutes of the "
                    f"same-day window remains (it closes at 23:00 Vilnius) — too little to "
                    f"be useful. Use --advance to sign right now, or pick tomorrow."
                )
        else:
            epoch = _noon_epoch(d)
    if epoch <= now + 60:
        sys.exit(
            "ERROR: --perform-at resolves to a past/near-instant time. perform_at is the signing "
            "deadline, so pick a comfortably-future time (e.g. +3h, +1d) — or use --advance for "
            "sign-on-the-spot ASAP."
        )
    return epoch


def resolve_payer(args):
    """Decide the payer account from the invoice BUYER, refusing to pay from an
    arbitrary account. Priority: verified --buyer-code map > verified --buyer-name map
    (fail-closed fallback for documents that print only the seller's code, e.g. an
    ERGO "Mokėjimo pranešimas") > explicit --payer. A code/--payer or code/name
    mismatch is a hard error (the whole point: never pay an invoice from the wrong
    company's account)."""
    code = (args.buyer_code or "").strip()
    code_resolved = BUYER_CODE_TO_ACCOUNT.get(code) if code else None
    # NAME fallback — used only when the code did not resolve. Some documents never
    # print the buyer's registration code (only the seller's), so the LLM returns an
    # empty or wrong buyer_code; an EXACT, curated buyer-name match then recovers the
    # payer. Never fuzzy, only our own unambiguous entities.
    name_resolved = (
        BUYER_NAME_TO_ACCOUNT.get(_norm_buyer_name(args.buyer_name)) if args.buyer_name else None
    )
    # If a mapped code and a mapped name DISAGREE, something is mis-extracted — refuse
    # rather than silently trust the code (defense in depth).
    if code_resolved and name_resolved and code_resolved != name_resolved:
        sys.exit(
            f"ERROR: buyer mismatch — code {code} maps to {code_resolved} "
            f"({ALLOWED_ACCOUNTS.get(code_resolved)}), but name {args.buyer_name!r} maps to "
            f"{name_resolved} ({ALLOWED_ACCOUNTS.get(name_resolved)}). Refusing (possible "
            f"mis-extraction) — verify and pass --payer explicitly."
        )
    resolved = code_resolved or name_resolved
    # Audit trail: a payer chosen by NAME (no usable code) is the safety-sensitive
    # path — make it visible in logs.
    if name_resolved and not code_resolved:
        print(
            f"NOTE: payer resolved via buyer NAME {args.buyer_name!r} → {resolved} "
            f"({ALLOWED_ACCOUNTS.get(resolved)}); buyer code "
            f"{('unmapped: ' + code) if code else 'absent'}.",
            file=sys.stderr,
        )
    # Audit trail: an unmapped code means the wrong-account guard did NOT run, so the
    # payer is whatever --payer said. Say so rather than proceeding silently.
    if code and not code_resolved and args.payer:
        print(
            f"NOTE: buyer code {code} is not in BUYER_CODE_TO_ACCOUNT — the wrong-account "
            f"guard could not verify it. Using --payer {args.payer} "
            f"({ALLOWED_ACCOUNTS.get(args.payer, '?')}) unchecked. Add the verified code "
            f"to the map to enable the guard.",
            file=sys.stderr,
        )
    if not resolved and not args.payer:
        known = "\n".join(f"    {a}  {l}" for a, l in ALLOWED_ACCOUNTS.items())
        ident = (
            " / ".join(
                x
                for x in [
                    f"code {code}" if code else "",
                    f"name {args.buyer_name!r}" if args.buyer_name else "",
                ]
                if x
            )
            or "(no buyer code or name given)"
        )
        sys.exit(
            f"ERROR: could not resolve a payer account from the invoice buyer [{ident}].\n"
            f"  Refusing to guess the account. Add a verified code to BUYER_CODE_TO_ACCOUNT\n"
            f"  or an exact name to BUYER_NAME_TO_ACCOUNT, or pass --payer explicitly.\n"
            f"  Scoped accounts:\n{known}"
        )
    if resolved and args.payer and args.payer != resolved:
        sys.exit(
            f"ERROR: account mismatch — invoice buyer maps to "
            f"{resolved} ({ALLOWED_ACCOUNTS.get(resolved)}), but --payer is {args.payer} "
            f"({ALLOWED_ACCOUNTS.get(args.payer, '?')}). Refusing to pay from the wrong account."
        )
    payer = resolved or args.payer
    if not payer:
        sys.exit(
            "ERROR: no payer. Pass --buyer-code (mapped), --buyer-name (mapped), or "
            "--payer (EVP...)."
        )
    if payer not in ALLOWED_ACCOUNTS:
        print(f"ERROR: payer {payer} is not one of the token's scoped accounts:", file=sys.stderr)
        for a, label in ALLOWED_ACCOUNTS.items():
            print(f"  {a}  {label}", file=sys.stderr)
        sys.exit(2)
    return payer


def _noon_epoch(d):
    """Epoch for 12:00 UTC on date d (safe inside the perform_at day, away from
    midnight boundaries)."""
    return int(
        datetime.datetime(d.year, d.month, d.day, 12, tzinfo=datetime.timezone.utc).timestamp()
    )


_NO_WINDOW_LEFT = (
    "NOTE: less than 10 minutes of today's signing window remains (it closes at 23:00 "
    "Vilnius) — too little to be useful, so this is being sent "
    "ASAP instead (operation_date is still today, so it stays visible in the mobile app). "
    "The deadline is immediate: sign it now, or re-run tomorrow."
)


def compute_schedule(args):
    """Resolve perform_at + a display `mode` from the chosen options.

    Regimes (re-measured live 2026-06-30):
      * --today    -> perform_at = LATER TODAY (23:00 Vilnius): same-day window, and the
        transfer shows in BOTH the mobile app AND the web bank. The right pick for "pay
        today, sign on my phone within hours".
      * --advance  -> perform_at OMITTED (ASAP): due now. Deadline ~immediate (urgent EUR =
        ~0 s, normal = ~30 min). Mobile-visible but must be signed on the spot.
      * --due-date -> perform_at = ONE DAY BEFORE the due date (after-fact invoices). If
        due-1 is today/past, falls back to ASAP.
      * --perform-at / default -> see parse_perform_at. The DEFAULT is context-aware:
        a payment WITH --invoice-id (invoice/bulk) gets +30d (long WEB-BANK window, signed
        at leisure); a payment WITHOUT --invoice-id (ad-hoc/personal) defaults to TODAY so
        it is mobile-signable. Both overridable via --today / --advance / --perform-at.

    Priority: --perform-at > --advance > --today > --due-date > context-aware default.
    Returns (perform_at_epoch_or_None, mode). None => omit perform_at (ASAP).
    """
    # Vilnius, not UTC: parse_perform_at judges "is this date past?" the same way, and
    # between 21:00 and 24:00 UTC the two calendars disagree.
    today = _vilnius_today()
    if args.perform_at:
        return parse_perform_at(args.perform_at), "scheduled"
    if args.advance:
        return None, "asap"
    if args.today:
        epoch = _end_of_today_epoch()
        if epoch is None:
            print(_NO_WINDOW_LEFT, file=sys.stderr)
            return None, "asap"
        return epoch, "today"
    if args.due_date:
        try:
            due = datetime.datetime.strptime(args.due_date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit("ERROR: --due-date must be YYYY-MM-DD")
        pay_day = due - datetime.timedelta(days=1)
        if pay_day <= today:
            print(f"NOTE: due-date {due} minus 1 day is today/past — paying ASAP instead.")
            return None, "asap"
        return _noon_epoch(pay_day), "scheduled"
    # Context-aware default: invoice/bulk -> +30d web-bank window; ad-hoc/personal -> today.
    if args.invoice_id:
        return parse_perform_at(None), "scheduled"  # +30d
    epoch = _end_of_today_epoch()
    if epoch is None:
        print(_NO_WINDOW_LEFT, file=sys.stderr)
        return None, "asap"
    return epoch, "today"


def main():
    """Own the lock's lifetime explicitly.

    The ExitStack used to be a bare local in the body below, with nothing closing it: the
    release then depended on CPython's reference counting collecting the stack and, through
    it, the ledger_lock generator. Nothing failed in practice — the process exits straight
    after, and the kernel drops a flock with the process — but the scope the comments claim
    has to be enforced by the code, not by an interpreter detail. Held here, the lock is
    released on every exit path, SystemExit included.
    """
    with contextlib.ExitStack() as guard:
        return _main(guard)


def _main(guard):
    ap = argparse.ArgumentParser(
        description="Create a draft Paysera transfer (dry-run unless --confirm)."
    )
    ap.add_argument(
        "--payer",
        default=None,
        help="Payer Paysera account_number (EVP...). Optional if --buyer-code resolves it. "
        "Must be a scoped account; a mismatch with --buyer-code is refused.",
    )
    ap.add_argument(
        "--buyer-code",
        default=None,
        help="Invoice BUYER (pirkėjas) registration code. Resolves the correct payer "
        "account so an invoice is never paid from the wrong company's account.",
    )
    ap.add_argument(
        "--buyer-name", default=None, help="Invoice buyer name (display / soft cross-check)."
    )
    ap.add_argument(
        "--today",
        action="store_true",
        help="Pay TODAY, sign on your phone: perform_at = 23:00 Vilnius today → a same-day "
        "signing window (until tonight) AND the transfer shows in BOTH the mobile app and the "
        "web bank. This is the right pick for an ad-hoc/personal payment you'll sign shortly. "
        "(It is already the default when no --invoice-id is given.)",
    )
    ap.add_argument(
        "--advance",
        action="store_true",
        help="Sign-RIGHT-NOW mode (advance/prepayment, Išankstinis) → perform_at omitted = "
        "due now. Deadline is IMMEDIATE (max_execution_time ≈ creation instant), so it must "
        "be signed on the spot. For a same-day payment you'll sign within hours prefer --today; "
        "for an invoice you'll sign later use the default (+30d) or --perform-at.",
    )
    ap.add_argument(
        "--due-date",
        default=None,
        help="Invoice due date YYYY-MM-DD (after-fact invoices). Money leaves (instantly, "
        "on signing) at the latest one day before the due date.",
    )
    ap.add_argument(
        "--priority",
        choices=["auto", "urgent", "normal"],
        default="auto",
        help="Transfer priority. 'auto' (default) = SEPA Instant (urgency=urgent) for EUR, "
        "normal otherwise. 'urgent' forces instant; 'normal' forces a regular transfer.",
    )
    ap.add_argument(
        "--charge-type",
        default="sha",
        type=str.lower,
        choices=["sha", "our"],
        help="Charge bearer: 'sha' (shared, the SEPA standard) or 'our' (payer bears all "
        "fees). The API accepts only these two — there is no 'ben'. REQUIRED for the "
        "mobile app to show the transfer; unset, mobile hides it.",
    )
    ap.add_argument("--beneficiary-name", required=True, help="Beneficiary full name")
    ap.add_argument(
        "--beneficiary-address",
        default=None,
        help="Beneficiary postal address (street, city, country). REQUIRED by the "
        "Paysera API for cross-border / international transfers (non-SEPA-Instant); "
        "the API rejects them with 'mapper_empty_beneficiary_address' otherwise.",
    )
    ap.add_argument("--iban", required=True, help="Beneficiary IBAN/account (the one to pay TO)")
    ap.add_argument(
        "--beneficiary-bic",
        default=None,
        help="Beneficiary bank BIC/SWIFT (e.g. PBANUA2X). REQUIRED for non-SEPA "
        "international transfers (UA/AM/GE/…); the BIC's chars 5-6 also give the "
        "recipient country. SEPA transfers don't need it.",
    )
    ap.add_argument(
        "--beneficiary-bank-name",
        default=None,
        help="Beneficiary bank name (e.g. JSC CB PRIVATBANK), for international wires.",
    )
    ap.add_argument(
        "--beneficiary-city",
        default=None,
        help="Beneficiary city — required by the API for international transfers "
        "(goes in additional_information; missing → 'mapper_beneficiary_city_not_set').",
    )
    ap.add_argument(
        "--also-iban",
        action="append",
        default=[],
        help="Other beneficiary IBAN(s) the SAME invoice lists (e.g. a second bank). "
        "Repeatable. These feed the duplicate check — a prior payment to ANY of them means "
        "the invoice is already paid. NOTE: a Paysera IBAN (LTkk35000...) in this list "
        "BECOMES THE PAYEE, even if it was passed here rather than as --iban (Paysera "
        "transfers are instant and free) — the chosen IBAN is always printed. Pass every "
        "account printed on the invoice, in invoice order.",
    )
    ap.add_argument(
        "--beneficiary-type",
        choices=["natural", "legal"],
        default=None,
        help="Is the beneficiary a private person ('natural') or a company ('legal')? "
        "Goes on the regulated payment message for cross-border transfers, so it is NOT "
        "guessed — required whenever the beneficiary is outside Lithuania.",
    )
    ap.add_argument("--amount", required=True, help="Decimal string, e.g. 12.34")
    ap.add_argument("--currency", default="EUR", help="ISO currency (default EUR)")
    ap.add_argument("--purpose", required=True, help="Payment purpose / details")
    ap.add_argument(
        "--no-preserve",
        dest="preserve",
        action="store_false",
        help="Let Paysera transliterate the purpose to the SEPA charset (ą→a, etc.). By "
        "default the tool sends purpose.details_options.preserve=true so Lithuanian "
        "letters are kept verbatim (matches typing them in the web bank).",
    )
    ap.set_defaults(preserve=True)
    ap.add_argument(
        "--payer-name", default=None, help="Payer display name (default: account label)"
    )
    ap.add_argument(
        "--invoice-id",
        default=None,
        help="Invoice number/identifier — dedup key. If a live/succeeded transfer for "
        "this invoice already exists, creation is refused (unless --force).",
    )
    ap.add_argument(
        "--invoice-date", default=None, help="Invoice issue date YYYY-MM-DD (recorded)."
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Bypass the invoice duplicate check and create anyway.",
    )
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument(
        "--perform-at",
        default=None,
        help="Signing deadline: YYYY-MM-DD | +Nd | +Nh | epoch seconds. SAME-DAY is allowed "
        "and is the recommended choice when you will sign on your phone (it keeps "
        "operation_date = today, so the transfer shows in the mobile app as well as the web "
        "bank); a FUTURE day is web-bank-only until that day. Only a past or near-instant "
        "time is rejected — use --advance for sign-on-the-spot ASAP. Default when omitted: "
        "+30d WITH --invoice-id, today WITHOUT it.",
    )
    ap.add_argument(
        "--no-register",
        action="store_true",
        help="Skip the PUT /register step after create. By default the tool registers the "
        "created transfer so it becomes visible for manual signing in the Paysera app/UI. "
        "Without registering, the transfer stays in the validation-only 'new' state (invisible).",
    )
    ap.add_argument(
        "--confirm", action="store_true", help="Actually POST. Without it, dry-run only."
    )
    args = ap.parse_args()

    _warn_tz_once()
    payer = resolve_payer(args)

    try:
        amount_dec = Decimal(args.amount)
        # Decimal accepts "Infinity", "NaN" and "1e999" — all of which are > 0 and would
        # sail into the payload to be rejected late and obscurely by the API.
        if not amount_dec.is_finite() or amount_dec <= 0:
            raise InvalidOperation
    except InvalidOperation:
        sys.exit("ERROR: --amount must be a positive, finite decimal string like 12.34")
    if -amount_dec.as_tuple().exponent > 2:
        sys.exit(
            f"ERROR: --amount {args.amount} has more than 2 decimal places. "
            f"Round it to cents first."
        )
    # Decimal is arbitrary-precision, so "1e999" is finite and positive. Bound it: no
    # legitimate transfer is this large, and the API's rejection is late and cryptic.
    if amount_dec >= Decimal("1e12"):
        sys.exit(f"ERROR: --amount {args.amount} is implausibly large. Check the value.")
    # Everything downstream uses the VALIDATED decimal, never the raw text. `1e2`,
    # ` 12.34` and `+12.34` all pass the checks above, and the raw spelling used to go
    # straight into the payload and the ledger — a format the API may reject or read
    # differently. format(..., "f") is plain positional notation and preserves the
    # written scale ("100.00" stays "100.00").
    args.amount = format(amount_dec, "f")

    # Normalise ONCE: the routing decision below upper-cases it, so sending the raw
    # spelling would let `--currency eur` pick the instant rail while the payload and the
    # ledger say "eur".
    currency = args.currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        sys.exit(f"ERROR: --currency {args.currency!r} must be a 3-letter ISO code, e.g. EUR.")
    args.currency = currency

    token = read_token(args.token_file)

    # --- Beneficiary IBAN selection (multi-IBAN invoices) -----------------------
    # Normalise EVERY listed account first. This has to precede the selection below: that
    # is where a Paysera IBAN wins the payment, and one written with separators would lose
    # it while the printed reason claimed no Paysera IBAN was listed.
    args.iban, args.also_iban = clean_accounts(args.iban, args.also_iban)

    # An invoice may list several beneficiary IBANs (e.g. SEB AND Luminor). Pay to the
    # Paysera one (LTkk35000…) if any is listed, else the first listed. This is enforced
    # here so it holds no matter the order --iban/--also-iban were given in. Every listed
    # IBAN still feeds the duplicate check below.
    chosen_iban, other_ibans, sel_reason = select_beneficiary_iban(args.iban, args.also_iban)
    if other_ibans:
        print(f"Beneficiary IBAN: {chosen_iban}  ({sel_reason})")
        print(f"  other listed IBAN(s), dup-check only: {', '.join(other_ibans)}")
    args.iban = chosen_iban
    args.also_iban = other_ibans

    # --- Configuration checks -------------------------------------------------------
    # These need no network, so they run BEFORE the duplicate check. Otherwise a missing
    # --beneficiary-type would first pay for a full round of dup-check requests (and any
    # warnings they print) before failing on something known up front.
    payer_name = args.payer_name or ALLOWED_ACCOUNTS[payer]
    # The shipped ALLOWED_ACCOUNTS labels are placeholders, and the label becomes the payer
    # NAME the beneficiary sees — an unedited config would send a transfer from
    # "Company A (example — replace me)". Only the LABEL is checked: an explicit
    # --payer-name is a deliberate choice, and a company really can be called "Example Ltd".
    if not args.payer_name and re.search(r"example|replace me", ALLOWED_ACCOUNTS[payer], re.I):
        sys.exit(
            f"ERROR: the ALLOWED_ACCOUNTS label for {payer} is still the placeholder "
            f"{ALLOWED_ACCOUNTS[payer]!r}. The beneficiary would see it as the payer name.\n"
            f"  Set a real label in ALLOWED_ACCOUNTS, or pass --payer-name."
        )

    # A date the operator typed is a date they expect to be used. Silently falling back to
    # the default window on a parse failure made the duplicate scan narrower than the
    # printed report claimed — and for an invoice older than the default window, a real
    # duplicate could sit outside the scan while stdout said it had been covered.
    # Format, and "not in the future" — a window starting after today holds no transfer,
    # so the live half of the duplicate check is disabled and then reports "no prior
    # payments", which reads as an all-clear. `--perform-at` refuses a past date for the
    # same class of reason.
    date_error = invoice_date_error(args.invoice_date)
    if date_error:
        sys.exit(date_error)

    # A national account number that is not an IBAN (e.g. the Armenian "2050…" format,
    # supported further down) starts with digits, so neither the IBAN prefix nor an
    # absent BIC yields a country. beneficiary_country() then returns None, and an
    # unknown country used to read as "domestic and in the SEPA zone": the --beneficiary-
    # type, --beneficiary-bic, --beneficiary-address and --beneficiary-city checks were
    # ALL skipped, and the instant rail was selected for an account outside SEPA. The API
    # then refuses the transfer with 'mapper_beneficiary_country_not_set' — exactly the
    # late, cryptic failure these pre-flight checks exist to replace. An unknown country
    # is not domestic; ask for the BIC, which carries the country in chars 5-6.
    # Separators were already removed by clean_accounts() above — for EVERY listed
    # account, not just this one, and before the payee was chosen from among them.
    acct_raw = args.iban or ""
    is_iban = bool(IBAN_SHAPE.match(acct_raw))
    if not is_iban and not args.beneficiary_bic:
        sys.exit(
            f"ERROR: {acct_raw!r} is not an IBAN, so the beneficiary's country cannot be "
            f"determined from it.\n"
            f"  Pass --beneficiary-bic — its characters 5-6 are the country, which the "
            f"cross-border and SEPA checks need.\n"
            f"  Without it this transfer would be treated as domestic and rejected by the "
            f"API with a 'mapper_beneficiary_country_not_set' error after being sent."
        )

    # Recipient country (ISO-2) from the BIC (chars 5-6, reliable for non-IBAN accounts
    # too) or the IBAN prefix. Drives the address.country field and SEPA-vs-international
    # routing further down.
    benef_country = beneficiary_country(args.iban, args.beneficiary_bic)
    if benef_country is None:
        # Backstop for a malformed BIC (chars 5-6 not letters) on a non-IBAN account:
        # an unknown country must never fall through to the domestic path.
        sys.exit(
            f"ERROR: cannot determine the beneficiary's country from account "
            f"{acct_raw!r} or BIC {args.beneficiary_bic!r}.\n"
            f"  Check --beneficiary-bic: characters 5-6 must be the ISO-2 country."
        )
    is_international = benef_country != "LT"
    is_sepa_zone = benef_country in SEPA_COUNTRIES

    if is_international and not args.beneficiary_type:
        sys.exit(
            f"ERROR: beneficiary is in {benef_country} (cross-border), so "
            f"--beneficiary-type is required.\n"
            f"  Pass 'legal' for a company or 'natural' for a private person — it goes on "
            f"the regulated payment message and must not be guessed."
        )
    if not is_sepa_zone:
        missing = [
            flag
            for flag, value in [
                ("--beneficiary-bic", args.beneficiary_bic),
                ("--beneficiary-address", args.beneficiary_address),
                ("--beneficiary-city", args.beneficiary_city),
            ]
            if not value
        ]
        if missing:
            sys.exit(
                f"ERROR: {benef_country} is outside the SEPA zone, so this is an "
                f"international wire and the API requires: {', '.join(missing)}.\n"
                f"  Without them the transfer is rejected with a 'mapper_*_not_set' error "
                f"after it is sent."
            )

    # --- Idempotency: refuse to double-pay the same invoice ---
    # Held from BEFORE the duplicate check until main() returns, so that a concurrent run
    # cannot pass its own check in the window between this one checking and recording.
    # Only for a run that will actually send: a dry run mutates nothing, and taking the
    # lock would make a harmless preview block a real payment.
    if args.invoice_id and args.confirm:
        guard.enter_context(ledger_lock())

    candidate_ibans = [args.iban] + list(args.also_iban or [])
    if args.invoice_id and not args.force:
        blocking, seen = find_blocking(
            args.invoice_id,
            token,
            payer=payer,
            ibans=candidate_ibans,
            amount=args.amount,
            invoice_date=args.invoice_date,
            currency=currency,
        )
        # Show EVERY payment to any candidate IBAN in the period, for human review. The
        # period comes from the same helper the scan used — never re-derived here.
        _, period = scan_window(args.invoice_date)
        print(
            f"Dup-check: scanned payments from {payer} to "
            f"{len(set(_norm_iban(i) for i in candidate_ibans))} beneficiary IBAN(s) {period}."
        )
        if seen:
            print(f"  Found {len(seen)} prior payment(s) to those IBANs — review:")
            for h, st, amt, ib, purp in seen:
                print(f"    {amt} -> {ib}  status={st}  {h}\n      purpose: {purp[:120]}")
        else:
            print("  No prior payments to those IBAN(s) in the period.")
        if blocking:
            print(f"SKIP — this invoice looks already paid ('{args.invoice_id}'):")
            for h, st, src, why in blocking:
                print(f"  transfer {h}  status={st}  [{src}]\n    matched because: {why}")
            amount_only = all(src == "live-list" and "SAME AMOUNT" in why for _, _, src, why in blocking)
            print("Not creating another.")
            if amount_only:
                # Do not point at --force first. --force disables the ledger source too,
                # including the write-ahead row that catches an unanswered POST.
                print(
                    "  Every match above is on the amount alone, which a recurring "
                    "supplier trips every period.\n"
                    "  Narrow the window with --invoice-date YYYY-MM-DD (the invoice's "
                    "issue date) before reaching for --force — --force switches the "
                    "duplicate check off entirely, ledger included."
                )
            else:
                print("  Use --force to override.")
            sys.exit(3)
    elif not args.invoice_id:
        print("NOTE: no --invoice-id given — duplicate check is OFF for this run.")
    else:
        # --force with an invoice id. Every other bypass in this script announces itself;
        # disabling the primary double-payment guard must not be the quiet one.
        print(
            f"NOTE: --force given — the duplicate check for invoice '{args.invoice_id}' "
            f"was SKIPPED ENTIRELY. Neither the ledger nor the live transfer list was "
            f"consulted. If this invoice was already paid, this will pay it again.",
            file=sys.stderr,
        )

    perform_at, mode = compute_schedule(args)

    # A real IBAN goes in bank_account.iban; a non-IBAN national account number (e.g.
    # Armenia "2050…") goes in bank_account_number (the API rejects it as iban).
    acct = (args.iban or "").replace(" ", "").upper()
    bank_account = {"iban": acct} if is_iban else {"bank_account_number": acct}
    if args.beneficiary_bic:
        # BIC/SWIFT for international (non-SEPA) wires, e.g. UA PrivatBank PBANUA2X.
        bank_account["bic"] = args.beneficiary_bic.strip().upper()
    beneficiary = {
        "type": "bank",
        "name": args.beneficiary_name,
        "bank_account": bank_account,
    }
    # The international mapper reads the recipient country from
    # beneficiary.additional_information.country (ISO 3166-1 alpha-2), per the Paysera
    # Transfers API spec — NOT from beneficiary.country or address.country. Without it
    # a non-SEPA transfer fails 'mapper_beneficiary_country_not_set'.
    if benef_country and benef_country != "LT":
        # `type` declares the beneficiary as a private person or a company on a regulated
        # payment message, so it is NOT guessable — the tool refuses rather than assume.
        addl = {"type": args.beneficiary_type, "country": benef_country}
        if args.beneficiary_city:
            addl["city"] = args.beneficiary_city.strip()
        beneficiary["additional_information"] = addl
    # Address is required by the API for cross-border transfers (e.g. PL/UA/PT
    # contractor IBANs); the schema is structured ({address_line, country}), a flat
    # string is rejected as 'beneficiary.address.address_line is blank'. Omitted for
    # domestic SEPA where it isn't needed.
    if args.beneficiary_address:
        # Clip the address line — the API rejects long ones with
        # 'mapper_beneficiary_address_too_long' (SWIFT address lines are short). 70 is
        # the safe single-line cap. Loud, like the purpose: a silently dropped city or
        # postcode can get an international wire refused.
        addr = {"address_line": _clip_address(args.beneficiary_address)}
        if benef_country:
            addr["country"] = benef_country
        beneficiary["address"] = addr
    # Beneficiary bank name, for international wires (helps the receiving side route).
    if args.beneficiary_bank_name:
        beneficiary["bank"] = {"title": args.beneficiary_bank_name.strip()}
    payload = {
        "amount": {"amount": args.amount, "currency": args.currency},
        "beneficiary": beneficiary,
        "payer": {"name": payer_name, "account_number": payer},
        # preserve=true keeps Lithuanian diacritics verbatim instead of letting Paysera
        # transliterate to the SEPA charset (the stored transfer exposes this as
        # purpose.details_options.preserve). Override with --no-preserve.
        # SEPA caps the purpose at 140 chars; the API rejects longer with
        # 'details_too_long'. Trim on a word boundary so the invoice ref survives.
        "purpose": {
            "details": _clip_purpose(args.purpose),
            "details_options": {"preserve": args.preserve},
        },
        # REQUIRED for the mobile app to render the transfer. Web sets charge_type='sha'
        # (shared SEPA charges); without it the bank_transfer row has charge_type=NULL and
        # the Paysera mobile app filters it out of the sign list (verified 2026-06-15 by
        # diffing an API-created transfer vs a web-created one in gateway.bank_transfer).
        "charge_type": args.charge_type,
    }
    # perform_at is OPTIONAL: omitted => execute ASAP (operation_date=today), which is how
    # a hand-made web instant payment looks. A future timestamp schedules it for that day.
    if perform_at is not None:
        payload["perform_at"] = perform_at
    # SEPA Instant for EUR (urgency=urgent) unless overridden. Instant executes the
    # moment the transfer is signed (EUR SEPA Instant rail: bank=lt_lb_sepa_inst).
    # The instant rail only reaches SEPA-zone banks — a EUR transfer to a non-SEPA
    # country (UA/AM/GE/…) is a regular international SWIFT transfer, NOT instant.
    is_sepa = is_sepa_zone
    if args.priority == "urgent":
        instant = True
    elif args.priority == "normal":
        instant = False
    else:  # auto
        instant = args.currency.upper() == "EUR" and is_sepa
    if instant:
        payload["urgency"] = "urgent"

    print("Payer  :", payer, f"({ALLOWED_ACCOUNTS[payer]})")
    if args.buyer_code or args.buyer_name:
        bits = " ".join(
            x
            for x in [args.buyer_name or "", f"(code {args.buyer_code})" if args.buyer_code else ""]
            if x
        )
        print("Buyer  :", bits)
    if args.invoice_id:
        print(
            "Invoice:",
            args.invoice_id,
            f"(issued {args.invoice_date})" if args.invoice_date else "",
        )
    if is_international:
        kind = "company (legal)" if args.beneficiary_type == "legal" else "private person (natural)"
        print(f"Beneficiary: {benef_country}, declared as a {kind}")
    print("Priority:", "SEPA Instant (urgent)" if instant else "normal")
    print("Charge  :", payload["charge_type"], "(mobile app needs this set)")
    # Same-day if the perform_at timestamp lands on today's Vilnius date — drives whether the
    # transfer is mobile-visible (today) or web-bank-only (a future day).
    tz = _VILNIUS
    same_day = (
        perform_at is not None
        and datetime.datetime.fromtimestamp(perform_at, tz).date() == _vilnius_today()
    )
    if mode == "asap":
        print(
            "Schedule: ASAP (perform_at omitted) — operation_date = today. Deadline is "
            "IMMEDIATE (max_execution_time ≈ now) — sign on the spot, or it times out. "
            "For a same-day window you can sign within hours, use --today instead."
        )
        print("         WHERE TO SIGN: mobile app or web bank (2FA) — but immediately.")
        if instant:
            print("         Executes INSTANTLY the moment it is signed (EUR SEPA Instant).")
    elif mode == "today" or same_day:
        deadline = datetime.datetime.fromtimestamp(perform_at, tz)
        print(
            f"Schedule: TODAY — sign until {deadline:%H:%M} (Vilnius) today; "
            f"then auto-cancels if unsigned."
        )
        print(
            "         WHERE TO SIGN: BOTH the mobile app AND the web bank (operation_date = "
            "today). Sign on your phone (2FA)."
        )
        if instant:
            print("         Executes INSTANTLY the moment it is signed (EUR SEPA Instant).")
    else:
        # Vilnius, like every other date decision here — a UTC calendar disagrees with it
        # between midnight and 02:00/03:00 Vilnius and would print a day too early.
        perform_day = datetime.datetime.fromtimestamp(perform_at, _VILNIUS).date()
        deadline_day = perform_day - datetime.timedelta(days=1)
        print(
            f"Schedule: execute {perform_day} — sign/cancel until end of {deadline_day} "
            f"(Vilnius midnight); then auto-cancels if unsigned."
        )
        print(
            "         WHERE TO SIGN: the WEB BANK only (bank.paysera.com) — a future "
            "operation_date is NOT shown in the mobile app until that day."
        )
        if instant:
            print(
                f"         Scheduled for {perform_day}; once executed it uses the instant "
                f"rail (EUR SEPA Instant). Money leaves on the scheduled day, not at signing."
            )
    print("Payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if not args.confirm:
        print("\nDRY-RUN — nothing sent. Re-run with --confirm to register the draft transfer.")
        return

    # WRITE-AHEAD: record the attempt BEFORE sending it. If the POST is accepted by the
    # API but the answer never reaches us (timeout, killed process, dropped connection),
    # a draft exists on the server that nothing here knows about — and GET /transfers
    # does not list unsigned drafts, so the live cross-check cannot find it either. The
    # pending row is what stops the next run from creating a second draft for the same
    # invoice. Recorded whenever we have an invoice id to key it on.
    attempt_id = _new_attempt_id()
    if args.invoice_id:
        append_ledger(
            {
                "attempt_id": attempt_id,
                "state": "pending",
                "invoice_id": args.invoice_id,
                "invoice_date": args.invoice_date,
                "transfer_hash": None,
                "payer": payer,
                "amount": args.amount,
                "currency": currency,
                "beneficiary_iban": args.iban,
                "beneficiary_name": args.beneficiary_name,
                "created_at": int(time.time()),
                "created_at_iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )

    code, j = http_json_post(TRANSFER_API, payload, token)

    if code in ("200", "201") and isinstance(j, dict) and j.get("id") and not j.get("error"):
        transfer_hash = j.get("id")
        print(f"\nOK draft transfer created (HTTP {code})")
        print("  id (transferHash):", transfer_hash)
        print("  status           :", j.get("status"))

        # Register the transfer so it becomes visible for MANUAL SIGNING in the Paysera
        # app/UI. A bare POST leaves the transfer in the validation-only `new` state, which
        # is NOT shown anywhere for signing (the cause of the old "invisible draft" problem).
        # PUT /transfers/{hash}/register (scope transfers:create) moves it to a registered,
        # signable state. Skip with --no-register.
        if not args.no_register:
            rcode, rj = http_json("PUT", f"{TRANSFER_API}/{transfer_hash}/register", token)
            if rcode in ("200", "201", "204"):
                reg_status = rj.get("status") if isinstance(rj, dict) else None
                print("  registered       : yes (visible for manual signing)", end="")
                print(f" — status={reg_status}" if reg_status else "")
            else:
                print(
                    f"  registered       : FAILED (HTTP {rcode}) — transfer stays in 'new' "
                    f"(invisible). Retry: PUT {TRANSFER_API}/{transfer_hash}/register"
                )
                print("   ", str(rj)[:400])
        else:
            print("  registered       : skipped (--no-register) — stays 'new' (invisible).")

        if args.invoice_id:
            update_ledger(
                attempt_id,
                state="created",
                transfer_hash=transfer_hash,
                status_at_create=j.get("status"),
            )
            print("  ledger           : recorded for invoice", args.invoice_id)
        print("\nNOT executed — this token has no sign scope. Open the Paysera app and SIGN the")
        print("transfer (2FA) to actually send the money.")
    else:
        print(f"\nFAILED (HTTP {code})")
        print(str(j)[:1500])
        # Did the transfer get created or not? A 4xx is a definite refusal — the pending
        # row can be cleared so it never blocks a legitimate retry. Anything else
        # (transport failure, 5xx, unparseable answer) leaves it GENUINELY UNKNOWN: the
        # API may have accepted the POST and only the answer went missing. Keep the row
        # blocking and tell the operator to reconcile by hand.
        definitely_not_created = code.isdigit() and 400 <= int(code) < 500
        if args.invoice_id:
            if definitely_not_created:
                update_ledger(attempt_id, state="failed", http_code=code)
            else:
                update_ledger(attempt_id, state="unknown", http_code=code)
                print(
                    f"\nWARNING: it is NOT known whether the transfer was created — the "
                    f"request failed with '{code}' after being sent.\n"
                    f"  A draft may exist on the server. GET /transfers does NOT list "
                    f"unsigned drafts, so this tool cannot check for you.\n"
                    f"  CHECK THE PAYSERA APP for a draft to '{args.beneficiary_name}' "
                    f"for {args.amount} {currency} before retrying.\n"
                    f"  Until then this invoice is treated as already attempted and "
                    f"further runs will refuse (override with --force once you have "
                    f"confirmed no draft exists).",
                    file=sys.stderr,
                )
        sys.exit(1)


def http_json_post(url, payload, token):
    return http_json("POST", url, token, payload=payload)


if __name__ == "__main__":
    main()
