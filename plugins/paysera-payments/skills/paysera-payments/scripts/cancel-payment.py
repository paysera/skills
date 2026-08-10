#!/usr/bin/env python3
"""Cancel / delete a (draft) Paysera transfer by transferHash.

In the public Transfer API there is one removal operation — `DELETE
/transfers/{hash}` (scope `transfers:cancel`). It both **deletes a draft** and
**cancels a pending transfer**, depending on the transfer's current state:

  - state `new`/`reserved` (a live, unsigned draft)  -> removed (status -> rejected/canceled)
  - terminal states (`failed`, `done`, `rejected`, already `canceled`) -> 409 invalid_state
    (nothing to cancel)

So `--cancel` and `--delete` are the same call here; both flag spellings are accepted.

Safety: dry-run by default. It reads each transfer and shows what it would remove;
nothing is deleted until you pass --confirm.

Token: ~/.config/paysera-payments/token (override with --token-file or PAYSERA_PAT).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

TRANSFER_API = "https://api.paysera.com/public/transfer/rest/v1/transfers"
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/paysera-payments/token")
# States in which a transfer can still be removed/cancelled.
CANCELABLE_STATES = {"new", "reserved", "registered", "waiting_funds", "signing"}


def read_token(path):
    tok = os.environ.get("PAYSERA_PAT")
    if tok:
        return tok.strip()
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        sys.exit(f"ERROR: cannot read PAT ({e}). Set PAYSERA_PAT or pass --token-file.")


def curl_json(method, url, token):
    out = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            method,
            url,
            "-H",
            f"Authorization: Bearer {token}",
            "-w",
            "\nHTTP:%{http_code}",
        ],
        capture_output=True,
        text=True,
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
        http, doc = curl_json("GET", f"{TRANSFER_API}/{h}", token)
        if http != "200" or not isinstance(doc, dict) or not doc.get("id"):
            print(f"{h}: cannot read (HTTP {http}) — {str(doc)[:160]}")
            rc = 1
            continue
        st = doc.get("status")
        amt = doc.get("amount", {})
        payer = (doc.get("payer") or {}).get("account_number")
        print(f"{h}: status={st} amount={amt.get('amount')} {amt.get('currency')} payer={payer}")

        if st not in CANCELABLE_STATES:
            print(f"  -> not cancelable in state '{st}' (terminal). Skipping.")
            continue
        if not args.confirm:
            print("  -> DRY-RUN — would DELETE (cancel/delete). Add --confirm to do it.")
            continue
        dhttp, dres = curl_json("DELETE", f"{TRANSFER_API}/{h}", token)
        if dhttp in ("200", "204"):
            new_st = dres.get("status") if isinstance(dres, dict) else None
            print(f"  -> CANCELED (HTTP {dhttp}){f', status={new_st}' if new_st else ''}")
        else:
            print(f"  -> FAILED (HTTP {dhttp}) — {str(dres)[:200]}")
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
