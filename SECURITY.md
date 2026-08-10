# Security policy

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Report it to
**security@paysera.com**, or through the vulnerability disclosure process at
<https://www.paysera.com>. Include the affected skill and version, what you observed, and
how to reproduce it. Do not include tokens, account numbers, or personal data.

## Scope

This repository contains agent skills that call public Paysera APIs from a user's own
machine. In scope: the skill content and helper scripts published here. Vulnerabilities in
the Paysera APIs or products themselves fall under Paysera's general disclosure process.

## Token handling

The skills here authenticate with a Personal Access Token that the user creates on their own
account:

- The token is stored locally (`~/.config/paysera-payments/token`, mode `0600`) and is sent
  only to Paysera API hosts.
- Tokens are scoped to specific accounts and must be created **without** the
  `transfers:sign` scope, so a skill cannot execute a payment.
- A token can be revoked at any time via the Personal Access Token API using its `jti`.

If you believe a token has been exposed, revoke it immediately and create a new one.
