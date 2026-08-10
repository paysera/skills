---
description: "Create (draft) or cancel/delete a Paysera payment/transfer via the Transfer API — drafts only, never signs/executes."
argument-hint: "<the payment/transfer request: create draft, cancel/delete a draft, or check status>"
---

Carry out the request below as the **paysera-payments** skill would, following its SKILL.md exactly.

DETERMINISM (important): do NOT load `paysera-payments` through the Skill / routing namespace —
a same-named user-level skill may shadow this plugin and resolve to different content. Instead,
**`Read` this plugin's OWN file directly by path** — `${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/SKILL.md`
— and follow that file verbatim. The `scripts/` referenced below are this plugin's own
(`${CLAUDE_PLUGIN_ROOT}/skills/paysera-payments/scripts/`). This pins behaviour to the plugin
content regardless of any shadow.

Then carry out the request:

- **Create a draft transfer** → use `scripts/create-payment.py` (resolve the payer from
  `--buyer-code`, or `--payer`; pass beneficiary name, IBAN, amount, currency, purpose, and
  `--invoice-id` for dedup). It is **dry-run by default** — print the payload first; only add
  `--confirm` when the user has confirmed. Creation is **drafts-only**: it never signs/executes
  (signing happens in the Paysera app, 2FA).
- **Cancel / delete a draft** → use `scripts/cancel-payment.py <transferHash>` (dry-run by
  default; `--confirm` performs the `DELETE`). Only unsigned/live drafts are removable.
- **Check status / balance** → use the read-only `GET /transfers/{hash}`, `GET /transfers`,
  or `GET /accounts/{n}/full-balance` endpoints with the PAT.

Never move real money on your own and never `--confirm` without explicit user confirmation.

Request: $ARGUMENTS
