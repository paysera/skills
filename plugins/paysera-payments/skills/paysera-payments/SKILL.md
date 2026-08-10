---
name: paysera-payments
description: Create (draft) or cancel/delete a Paysera payment/transfer via the public Transfer API using a scoped Personal Access Token — the "do a payment / pay this invoice" action. Use when asked to create, cancel, or delete a payment, transfer, pavedimas, or mokėjimas on a configured Paysera account. Create is drafts-only — it does NOT sign/execute; signing happens in the Paysera app (2FA). Cancel/delete removes a live (unsigned) draft. Keywords — payment, transfer, pavedimas, mokėjimas, SEPA, invoice. Do NOT use for — diagnosing why an existing payment failed or is stuck, or handling a support ticket about one; contact Paysera support instead.
---

# Paysera Payments (create draft transfers)

Register transfers on Paysera accounts via the public Transfer API, authenticated
with the account-scoped `paysera-payments` Personal Access Token.

**Important — drafts only.** The token has `transfers:create` and
`transfers:cancel` but **not** `transfers:sign`. Every transfer this skill creates
is a DRAFT and is **not executed**. The money moves only after the transfer is signed
in the Paysera app (2FA), where your account's own per-company daily limits apply. This
skill cannot send money on its own — by design. It can, however, cancel/delete its own
unsigned drafts.

**Create then register (visible for signing).** After creating a transfer (which lands
in the validation-only `new` state, invisible everywhere), the tool calls
`PUT /transfers/{hash}/register` so it shows up for **manual signing** in the Paysera
app/UI. Without that register step (scope `transfers:create`) the draft stays invisible.
Skip it with `--no-register`.

Scopes on the token: `accounts:read`, `transfers:read`, `transfers:create`,
`transfers:cancel` (each scoped to the accounts you configure below).

## Requirements

- **Python 3.8+** and **`curl`** on `PATH`. If curl is missing or a request times out
  (30 s), the tool says so on stderr rather than treating it as an empty API response.
- **`tzdata`** (Python 3.9+ ships `zoneinfo`; slim containers often omit the tz database).
  Scheduling is done in Europe/Vilnius, because that day boundary decides whether a
  transfer is signable in the mobile app. Without tzdata the tool falls back to a built-in
  EET/EEST rule and prints a note — correct under current EU DST rules, but install tzdata
  if you want the authoritative zone.

## Token

- Stored at `~/.config/paysera-payments/token` (mode `0600`, local to your machine).
- Never passed on a command line. The scripts hand it to curl through stdin, because
  command arguments are readable by any local user (`ps auxww`, `/proc/<pid>/cmdline`).
- `jti` for revocation is in `~/.config/paysera-payments/jti.txt`.
- Created against the public Personal Access Token API, authenticated with a
  `bank.paysera.com` session bearer token (`$AUTH`):

  ```bash
  curl -s -X POST \
    "https://auth-api.paysera.com/personal-access-token/rest/v1/personal-access-tokens" \
    -H "Authorization: Bearer $AUTH" -H "Content-Type: application/json" \
    -d '{
      "name": "paysera-payments",
      "resources": [
        {"type": "accounts",  "scopes": [{"key": "read"}],   "context": {"account_number": "EVP0000000000001"}},
        {"type": "transfers", "scopes": [{"key": "read"}, {"key": "create"}, {"key": "cancel"}],
         "context": {"account_number": "EVP0000000000001"}}
      ]
    }'
  ```

  Save the returned token to `~/.config/paysera-payments/token` and the `jti` to
  `~/.config/paysera-payments/jti.txt`. **The token value is not retrievable again** —
  if lost, revoke it (see "Revoking the token") and create a new one.
- Do **not** grant `transfers:sign`. Without it the skill physically cannot move money.

## Scoped accounts (payer must be one of these)

The PAT is scoped to a fixed set of accounts. Configure them in `create-payment.py` →
`ALLOWED_ACCOUNTS` (the `EVP…` account_number, NOT the IBAN). Example:

| account_number     | label                          |
|--------------------|--------------------------------|
| `EVP0000000000001` | Company A (example — replace)  |
| `EVP0000000000002` | Company B (example — replace)  |

The token rejects any payer account not in that map (a safety guard).

## Usage

Gather inputs (the invoice BUYER, beneficiary name, beneficiary IBAN, amount,
currency, purpose), then run the helper. It is **dry-run by default** — it prints the
payload and sends nothing until you add `--confirm`.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/scripts/create-payment.py \
  --buyer-code 123456789 --buyer-name "Your Company, UAB" \
  --beneficiary-name "UAB Example Supplier" \
  --iban "LT000000000000000000" \
  --amount "2830.00" --currency EUR \
  --purpose "Pagal PVM sąskaitą faktūrą EX 000123" \
  --invoice-id "EX000123" --invoice-date 2026-06-15
# review the printed payload, then add --confirm to register the draft.
# NOTE: the --purpose above is the FALLBACK form (that invoice gave no purpose
# instruction). If an invoice/email DOES say what to write in the purpose, quote it
# verbatim instead — that is BINDING. See "Payment purpose" below.
```

On success it prints the transfer `id` (transferHash) and `status`, registers it for
signing (`registered: yes`), and reminds you to sign it in the Paysera app.

- **The payment purpose is dictated by the invoice — see "Payment purpose" below.** If
  the invoice (or its covering email) states what to put in the purpose, quote that
  EXACTLY; only compose a descriptive purpose when no such instruction exists anywhere.
- **Write the purpose in proper Lithuanian** (ą, č, ę, ė, į, š, ų, ū, ž). The tool sends
  it verbatim with `details_options.preserve=true`, so diacritics are kept (it does NOT
  strip them — earlier ASCII-only examples were a habit, not a requirement). Use
  `--no-preserve` only if you want Paysera to transliterate to the bare SEPA charset.
- **Schedule is context-aware by default** (re-verified live 2026-06-30):
  - **WITHOUT `--invoice-id` (ad-hoc / personal payment) → defaults to TODAY** — `perform_at
    = 23:00 Vilnius today`, so you get a same-day window AND the transfer shows in **both the
    mobile app and the web bank**. This is the "pay today, sign on my phone now" case.
  - **WITH `--invoice-id` (invoice / bulk) → defaults to +30d** — a long window signed at
    your leisure in the **web bank** (a future date is NOT shown in mobile until that day).
  - Override either: `--today` (force same-day, mobile-signable), `--perform-at +Nh|+Nd|YYYY-MM-DD`,
    or `--advance` (ASAP, sign on the spot). See "Choosing the execution date" below.
  - **Want to sign it on your phone? Use `--today`** (or just omit `--invoice-id`). A future
    date only shows in the web bank — that is why a `+30d` draft "can't be signed" in mobile.
- **Multiple beneficiary IBANs?** Pass them ALL, in invoice order (`--iban` = first
  listed, `--also-iban` = the rest, repeatable). The tool then auto-selects which one to
  pay — the Paysera IBAN if the invoice lists one, otherwise the first — see "Beneficiary
  IBAN selection" below. (All listed IBANs also feed the duplicate check.)
- `--amount` is a **decimal string** (e.g. `12.34`), not minor units.
- Override the token path with `--token-file <path>` or `PAYSERA_PAT`.

### Payment purpose (`--purpose`) — follow the invoice's instruction LITERALLY

The payment purpose/reference is **not yours to compose freely**. Most invoices — and
their covering emails — state **exactly** what the purpose must contain, e.g.
*"Mokėjimo paskirtyje prašome nurodyti dokumento seriją ir numerį ESO26N005482"*,
*"please quote reference INV-2026-001"*, *"paskirtyje nurodykite kliento kodą 123456"*.
The payee reconciles incoming payments (very often **automated / RPA**) by matching on
**precisely that string**, so any extra word can leave the payment unmatched.

**Decision rule for `--purpose` — apply every time, before you build the command:**

1. **Scan BOTH the invoice body AND the covering email** for a purpose/reference
   instruction. Trigger phrases: *paskirtyje, paskirtyje nurodyti / nurodykite, mokėjimo
   paskirtis, prašome nurodyti, būtina nurodyti, reference, payment reference / details,
   please indicate / quote / state*.
2. **If an instruction exists → `--purpose` = EXACTLY the value it tells you to quote, and
   NOTHING else.** Quote only the token(s) the instruction names — normally the bare
   document/reference number (`ESO26N005482`). Do **NOT** wrap it in prose: no *"Pagal
   sąskaitą …"*, no *"PVM sąskaita faktūra …"*, no *"Mokėjimas už …"*, no *"Dokumento
   Nr. …"*, no label words at all. If the instruction lists several elements (e.g.
   "client code and invoice number"), include exactly those, in the order given.
3. **Only if NO purpose instruction appears anywhere** → fall back to a short descriptive
   purpose: `--purpose "Pagal sąskaitą <invoice-id>"` (or `"Pagal PVM sąskaitą faktūrą
   <invoice-id>"`).

**Mandatory self-check before you run (even in dry-run):** re-read the invoice's purpose
sentence, then look at your `--purpose`. Does it contain **only** what the invoice asked
for? If you added a single word the invoice did not request, remove it and re-check.

| Invoice / email says | Correct `--purpose` | Wrong |
|----------------------|---------------------|-------|
| "paskirtyje nurodyti dokumento numerį ESO26N005482" | `ESO26N005482` | `Pagal PVM sąskaitą faktūrą ESO26N005482` |
| "please quote reference INV-2026-001" | `INV-2026-001` | `Payment for invoice INV-2026-001` |
| (no purpose instruction), invoice id `VPV178051` | `Pagal PVM sąskaitą faktūrą VPV178051` | — |

### Beneficiary IBAN selection (multi-IBAN invoices)

An invoice often prints **several** beneficiary IBANs (e.g. a SEB *and* a Luminor
account). Pass **every** one — `--iban` for the first listed, `--also-iban` (repeatable)
for the rest, **in the order printed on the invoice**. The tool then decides where the
money actually goes:

1. **If any listed IBAN is a Paysera account** — Lithuanian IBAN with bank code `35000`,
   i.e. `LTkk35000…` — it **always pays to that Paysera IBAN**, even if you passed it as
   `--also-iban` (Paysera→Paysera transfers are instant and free).
2. **If none is a Paysera IBAN** — it pays to the **first** IBAN you passed (`--iban`),
   which must be the first one listed on the invoice. So always pass them in invoice order.

Either way, all listed IBANs still feed the duplicate check. The tool prints a
`Beneficiary IBAN: … (reason)` line whenever more than one IBAN was given, so you can see
which it chose and why. Selection is enforced in the script (`select_beneficiary_iban`),
so it holds regardless of the order you pass the IBANs.

### Choosing the payer account (don't pay from the wrong account)

The payer must be the scoped account that belongs to the **invoice's BUYER (pirkėjas)**,
never an arbitrary one. Pass `--buyer-code <registration code>` and the tool **resolves
the correct payer** from `BUYER_CODE_TO_ACCOUNT` in the script. Rules:

- `--buyer-code` known → payer auto-resolved. If you ALSO pass `--payer` and it differs,
  the tool **refuses** (hard error) — this is the guard against wrong-account mistakes.
- `--buyer-code` unknown → the tool refuses to guess; add the **verified** code to
  `BUYER_CODE_TO_ACCOUNT` (never fabricate a code) or pass `--payer` explicitly.
- No buyer info → falls back to explicit `--payer` (legacy behaviour).

If the registration code is missing (some documents print only the seller's code), the
tool consults `BUYER_NAME_TO_ACCOUNT`, an **exact-match** (never fuzzy) buyer-name fallback
that also starts empty — add only your own, unambiguous entities.

Both the `BUYER_CODE_TO_ACCOUNT` and `BUYER_NAME_TO_ACCOUNT` maps start empty — add entries
as real invoices confirm each buyer's registration code/name (never fabricate one), or
always pass `--payer` explicitly.

### `charge_type` — REQUIRED for the mobile app to show the transfer

The tool always sends **`charge_type` (default `sha`)**. This is *optional* per the official
API (paysera/lib-wallet-transfer-rest-client: nullable, values `SHA`/`OUR` only — no `ben`),
BUT omitting it leaves the transfer's charge type unset, and the **Paysera mobile app then
filters the transfer out of its sign list** (web shows it; mobile does not). Verified
2026-06-15 by diffing an API-made transfer against a hand-made web one — the only meaningful
diff was `charge_type` (unset vs `sha`). Override with `--charge-type OUR`
if the payer should bear all bank fees (rare for SEPA). Without `sha`, expect the
"shows in web, not in mobile" bug.

### Priority — SEPA Instant by default for EUR

`--priority auto` (default) sends **`urgency: urgent`** for **EUR**, normal otherwise. Force
with `--priority urgent|normal`. Per the docs, `urgent` routes via Paysera's IBAN and uses
**SEPA Instant** when the beneficiary bank supports it (reaches the beneficiary in ~1 min).
"Instant" describes the **rail speed at execution**, not *when* execution happens: an ASAP
(`--advance`) transfer executes when signed; a **future-dated** (`--perform-at`/`--due-date`)
transfer executes **on its scheduled day** (money leaves that day, not at signing).

### Choosing the execution date (today vs future vs ASAP)

Priority: `--perform-at` (manual) > `--advance` > `--today` > `--due-date` > **context-aware
default** (`--invoice-id` present → `+30d`; absent → today).

`perform_at` sets the signing deadline (`max_execution_time`). Three regimes — pick by **how
soon you'll sign** and **where** (mobile vs web):

- **`--today` (or default when no `--invoice-id`) — "pay today, sign on my phone".**
  `perform_at = 23:00 Vilnius today` → a same-day window (until tonight), and because
  `operation_date = today` the transfer shows in **BOTH the mobile app AND the web bank**.
  ✅ This is the fix for the old "can't sign it on my phone" problem. Same-day `--perform-at
  +Nh` / today's `YYYY-MM-DD` behave the same.
- **`--perform-at +Nd` / a future `YYYY-MM-DD` (default for invoices, with `--invoice-id`)** —
  a real multi-day window, but a **future** `operation_date` renders **only in the web bank,
  not mobile**, until that day. Sign it in the web bank. Use for "I'll sign sometime this week".
- **`--due-date YYYY-MM-DD`** — after-fact invoice → `perform_at = due date − 1 day`. A
  today/past `due − 1` falls back to ASAP.
- **`--advance`** — *sign-right-now* ASAP (*Išankstinis*) → **`perform_at` omitted**. ⚠️ The
  deadline is **immediate**: with `urgent` (EUR SEPA-Instant default) `max_execution_time ≈
  creation instant` (~0 s); with `normal` it's ~30 min. Mobile-visible, but you must sign on
  the spot. **For a same-day payment you'll sign within hours, prefer `--today`.**

**Deadline mechanic (measured live; re-verified 2026-06-30).** Read the API field
**`max_execution_time`** on the transfer to know the truth:
- **same-day** `perform_at` (`--today`, `+Nh`, today's date) → `max_execution_time` = **that
  exact timestamp** (e.g. tonight 23:00). ✅ verified live. `operation_date = today` → mobile.
- **future-day** `perform_at` → `max_execution_time` ≈ **Vilnius midnight at the start of
  perform_at** (an ~N-day window). ✅ verified live. Web bank only until then.
- **ASAP** (`--advance`) → `max_execution_time` ≈ **creation instant** with `urgent` (~0 s);
  ~30-min with `normal`.

**No more "can't have both".** A *same-day* `perform_at` gives a usable window AND mobile
visibility simultaneously — the earlier "long window OR mobile, not both" note was wrong; it
only compared ASAP vs a future DAY and never tested a same-day future timestamp. (The
constraint that remains: a *future-day* transfer is web-only until that day.) A failed/
timed-out transfer is terminal and cannot be deleted (`DELETE` → 409); harmless.

> **Where to sign.** `--today` / same-day & ASAP transfers (with `charge_type=sha`) show in
> **both the mobile app and the web bank** — sign on your phone (2FA). A **future-dated**
> transfer is signed only in the **web bank** (bank.paysera.com → account → awaiting signature)
> until its scheduled day.

## Idempotency — avoid double-paying an invoice

Pass `--invoice-id <id>` (and optionally `--invoice-date YYYY-MM-DD`) when creating.
Before posting, the tool reads a local ledger (`~/.config/paysera-payments/ledger.json`),
finds prior transfers it created for that invoice, checks each one's **live** status,
and **refuses** (`exit 3`, `SKIP`) if any is still alive or already succeeded — i.e.
anything outside `NONBLOCKING_STATES` in `create-payment.py` (currently `failed`,
`rejected`, `canceled`/`cancelled`, `expired`, `declined`). A previously failed/canceled
attempt does NOT block (you can retry). Override with `--force`.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/scripts/create-payment.py \
  --payer EVP0000000000001 --beneficiary-name "Acme UAB" \
  --iban LT...primary --also-iban LT...second-bank \
  --amount 123.45 --purpose "Pagal sąskaitą INV-2026-001" \
  --invoice-id INV-2026-001 --invoice-date 2026-06-15 --confirm
```

The dedup checks **two** sources: (1) the local ledger of tool-created transfers, and
(2) the payer account's **actual** transfers via the `GET /transfers` list filter
(shipped 2026-06-18). For (2) it scans **all of the beneficiary's IBANs** — `--iban`
plus every `--also-iban` — over the whole period **from (invoice issue date − 1 day) to
today**, because an invoice can name several accounts (e.g. Luminor *and* SEB) and a
prior payment to **any** of them means the invoice is already paid. It **prints every
payment it finds to those IBANs in the period** (amount + purpose) for you to eyeball,
and **blocks** (`exit 3`, `SKIP`) on any whose **amount matches OR whose purpose quotes
the invoice id** — so a duplicate made **manually in the Paysera app, to either bank**,
is caught. Blocking ignores terminal transfers (`NONBLOCKING_STATES`, above — a prior
failed attempt does NOT block; retry freely). Override with `--force`. The live-list
check is best-effort: if the list call fails the tool **prints a warning to stderr** and
falls back to the ledger, which remains the guaranteed guard against e.g. an hourly cron
firing twice. Take that warning seriously — with only the ledger, a duplicate made by
hand in the Paysera app is invisible.

> **Pass all the invoice's IBANs.** The check is only as complete as the IBANs you give
> it. If the invoice lists two banks, `--also-iban` the second one — otherwise a prior
> payment to that other account would be missed.

## Cancel / delete a transfer

One operation removes a transfer: `DELETE /transfers/{hash}` (scope
`transfers:cancel`). It **deletes a live draft** and **cancels a pending transfer** —
same call; the effect depends on the transfer's state. Only live, unsigned transfers are
removable — the authoritative list is `CANCELABLE_STATES` in `cancel-payment.py`
(currently `new`, `reserved`, `registered`, `waiting_funds`, `signing`). Terminal states
(`failed`, `done`, `rejected`, already `canceled`) return `409 invalid_state` and are
skipped.

Dry-run by default; add `--confirm` to actually remove. Accepts multiple hashes.

```bash
# preview
python3 ${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/scripts/cancel-payment.py <transferHash> [<transferHash> ...]
# actually cancel/delete
python3 ${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/scripts/cancel-payment.py <transferHash> --confirm
```

(`--cancel` and `--delete` are accepted spellings but optional — the script always
does the one DELETE operation.)

## Checking a transfer afterwards

The token also has `transfers:read` and `accounts:read`:

```bash
PAT=$(cat ~/.config/paysera-payments/token)
# read one transfer (status moves to e.g. "signed"/"done" after you sign in the app)
curl -s "https://api.paysera.com/public/transfer/rest/v1/transfers/{transferHash}" \
  -H "Authorization: Bearer $PAT"
# list transfers with filters — ACCOUNTING semantics (verified 2026-07-08):
#   credit_account_number = account is the PAYER (outgoing transfers)
#   debit_account_number  = account is the BENEFICIARY (incoming transfers)
# dates are Unix timestamps; also: created_date_to, status (status is ignored in practice)
curl -s "https://api.paysera.com/public/transfer/rest/v1/transfers?credit_account_number={accountNumber}&created_date_from=1780261200" \
  -H "Authorization: Bearer $PAT"
# account balance
curl -s "https://api.paysera.com/public/account/rest/v1/accounts/{accountNumber}/full-balance" \
  -H "Authorization: Bearer $PAT"
```

## Revoking the token

```bash
JTI=$(cat ~/.config/paysera-payments/jti.txt)
# needs a fresh bank.paysera.com session bearer token ($AUTH):
curl -s -X DELETE \
  "https://auth-api.paysera.com/personal-access-token/rest/v1/personal-access-tokens/$JTI" \
  -H "Authorization: Bearer $AUTH"
```

## Notes / gotchas

- The public API is behind a CDN, so **PAT IP-restrictions may not match your real
  client IP**. If you hit unexpected `403`s, create the token without an IP restriction —
  the risk is bounded by: create-only (no sign), account scoping, and revocability.
- `GET /transfers` list **is** available (since 2026-06-18) with filters
  `credit_account_number` (= account is PAYER → outgoing), `debit_account_number`
  (= account is BENEFICIARY → incoming; accounting semantics, verified empirically
  2026-07-08), `created_date_from`/`_to` (Unix timestamps), `status` (ignored in
  practice). Also read a single transfer by hash.
- A freshly created transfer must be **registered** (`PUT /transfers/{hash}/register`)
  to be visible for signing; create-payment.py does this automatically.
- `account_number` is the Paysera `EVP…` number, **not** the IBAN.
