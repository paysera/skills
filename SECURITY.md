# Security policy

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Report it to
**security@paysera.com**, or through the vulnerability disclosure process at
<https://www.paysera.com>. Include the affected skill and version, what you observed, and
how to reproduce it. Do not include tokens, account numbers, or personal data.

## Scope

This repository contains agent skills for public Paysera APIs: some call those APIs from a
user's own machine, others teach an agent to write integration code. In scope: the skill
content and helper scripts published here. Vulnerabilities in
the Paysera APIs or products themselves fall under Paysera's general disclosure process.

## Credentials by skill

### `paysera-payments`

It authenticates with a Personal Access Token that the user creates on their own account:

- The token is stored locally (`~/.config/paysera-payments/token`, mode `0600`) and is sent
  only to Paysera API hosts.
- Tokens are scoped to specific accounts and must be created **without** the
  `transfers:sign` scope, so a skill cannot execute a payment.
- A token can be revoked at any time via the Personal Access Token API using its `jti`.

If you believe a token has been exposed, revoke it immediately and create a new one.

### `paysera-checkout`

It holds no credentials and calls no Paysera API. The code it helps write reads the Checkout
project's `client_secret` from the merchant's server environment. That secret also signs
webhooks. If it is exposed, regenerate the credentials on the project's Integrations page in
the Paysera merchant portal and deploy the new secret at once: the old one stops working
immediately, webhook signing included.
