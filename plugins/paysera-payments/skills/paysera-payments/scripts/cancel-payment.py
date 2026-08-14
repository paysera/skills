#!/usr/bin/env python3
"""Cancel / delete a (draft) Paysera transfer by transferHash.

In the public Transfer API there is one removal operation — `DELETE
/transfers/{hash}` (scope `transfers:cancel`). It both **deletes a draft** and
**cancels a pending transfer**, depending on the transfer's current state:

  - a live, unsigned transfer (see CANCELABLE_STATES below) -> removed
    (status -> rejected/canceled)
  - any other, terminal state (`failed`, `done`, `rejected`, already `canceled`, ...)
    -> 409 invalid_state (nothing to cancel)

So `--cancel` and `--delete` are the same call here; both flag spellings are accepted.

Safety: dry-run by default. It reads each transfer and shows what it would remove;
nothing is deleted until you pass --confirm.

Token: ~/.config/paysera-payments/token (override with --token-file or PAYSERA_PAT).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys

TRANSFER_API = "https://api.paysera.com/public/transfer/rest/v1/transfers"
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/paysera-payments/token")
HTTP_TIMEOUT = 30  # seconds per request — an unanswered API must not hang the run

# States in which a transfer is still live and unsigned, so DELETE can remove it.
# This is the authoritative list; SKILL.md points here rather than repeating it.
CANCELABLE_STATES = {"new", "reserved", "registered", "waiting_funds", "signing"}


# A transferHash is an opaque alphanumeric id. The character class is the point: it keeps
# slashes, dot-segments and query characters out of the URL path we build. The length
# bound is only a sanity cap — no minimum is imposed, since the API defines the format.
TRANSFER_HASH = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


class HttpError(RuntimeError):
    """A transport-level failure (curl missing, timed out, or non-zero exit)."""


def _check_token_file_mode(path):
    """Refuse a token file that group or other can read.

    Same rule as create-payment.py, and for the same reason: the docs promised 0600 but
    nothing enforced it, and a `curl ... > token` under the usual umask 022 leaves 0644.
    This token carries transfers:cancel, so a local reader can delete pending drafts.
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
    if not tok:
        _check_token_file_mode(path)
        try:
            with open(path) as f:
                tok = f.read()
        except OSError as e:
            sys.exit(f"ERROR: cannot read PAT ({e}). Set PAYSERA_PAT or pass --token-file.")
    tok = tok.strip()
    if not tok:
        sys.exit("ERROR: empty PAT.")
    if any(c in tok for c in '"\\\r\n'):
        sys.exit("ERROR: PAT contains a quote, backslash or newline — refusing to use it.")
    return tok


def curl_json(method, url, token, timeout=HTTP_TIMEOUT):
    """Run one curl request and return (http_code, parsed_body).

    The Authorization header goes to curl on STDIN as a config file (`-K -`), never as a
    command-line argument: argv is world-readable on Linux via `ps auxww` and
    /proc/<pid>/cmdline, so a token there is exposed to every local user for the
    lifetime of the call.
    """
    try:
        out = subprocess.run(
            ["curl", "-sS", "-X", method, url, "-K", "-", "-w", "\nHTTP:%{http_code}"],
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
    txt = out.stdout
    http = txt.split("\nHTTP:")[-1].strip()
    body = txt.split("\nHTTP:")[0]
    try:
        return http, json.loads(body)
    except json.JSONDecodeError:
        return http, body


def main():
    ap = argparse.ArgumentParser(
        description="Cancel/delete draft Paysera transfer(s) by hash (dry-run unless --confirm)."
    )
    ap.add_argument("hashes", nargs="+", help="One or more transferHash values to cancel/delete.")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument(
        "--confirm", action="store_true", help="Actually DELETE. Without it, dry-run only."
    )
    # accepted-but-ignored aliases so both 'cancel' and 'delete' wording works:
    ap.add_argument("--cancel", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--delete", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    token = read_token(args.token_file)
    rc = 0
    for h in args.hashes:
        if not TRANSFER_HASH.match(h):
            print(
                f"{h!r}: not a valid transferHash (expected only A-Z a-z 0-9 _ -). "
                f"Skipping.",
                file=sys.stderr,
            )
            rc = 1
            continue
        try:
            http, doc = curl_json("GET", f"{TRANSFER_API}/{h}", token)
        except HttpError as e:
            print(
                f"{h}: cannot read — {e}. Skipping (nothing was cancelled).",
                file=sys.stderr,
            )
            rc = 1
            continue
        if http != "200" or not isinstance(doc, dict) or not doc.get("id"):
            print(f"{h}: cannot read (HTTP {http}) — {str(doc)[:160]}", file=sys.stderr)
            rc = 1
            continue
        st = doc.get("status")
        # `or {}` not a .get default: the key can be present with a null value, and the
        # default only applies when the key is absent.
        amt = doc.get("amount") or {}
        payer = (doc.get("payer") or {}).get("account_number")
        print(f"{h}: status={st} amount={amt.get('amount')} {amt.get('currency')} payer={payer}")

        if st not in CANCELABLE_STATES:
            print(f"  -> not cancelable in state '{st}' (terminal). Skipping.")
            continue
        if not args.confirm:
            print("  -> DRY-RUN — would DELETE (cancel/delete). Add --confirm to do it.")
            continue
        try:
            dhttp, dres = curl_json("DELETE", f"{TRANSFER_API}/{h}", token)
        except HttpError as e:
            print(
                f"  -> FAILED — {e}. State unknown; re-check before retrying.",
                file=sys.stderr,
            )
            rc = 1
            continue
        if dhttp in ("200", "204"):
            new_st = dres.get("status") if isinstance(dres, dict) else None
            print(f"  -> CANCELED (HTTP {dhttp}){f', status={new_st}' if new_st else ''}")
        else:
            print(f"  -> FAILED (HTTP {dhttp}) — {str(dres)[:200]}", file=sys.stderr)
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
