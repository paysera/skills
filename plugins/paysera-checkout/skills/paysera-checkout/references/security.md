# Security

## The client secret

The project's `client_secret` does two jobs: it gets access tokens, and it is the key that
signs every webhook. Treat it like a password to the merchant's money.

- Keep it in the server environment (`PAYSERA_CLIENT_SECRET`) or a secret manager. Never put it in
  source code, a repository, a frontend bundle, a mobile app or a log line.
- Only the server talks to the Paysera API. The browser only ever sees the `payment_URL`.
- Do not log request headers, tokens or full webhook bodies. Log the order reference, the event
  name and your decision.
- If the secret leaks, regenerate the credentials in the merchant portal (project →
  Integrations → Regenerate credentials) and update the server at once. The old secret stops
  working immediately, webhook signing included.

### Changing the secret

Regenerating invalidates the old secret at once, so webhooks signed with the new secret fail the
check in a handler that still has the old one. The handler answers a failed check with `401`,
which makes Paysera stop retrying that delivery. Keep the switch short: deploy the new secret right after regenerating,
and for that window let the handler accept a signature made with either the old or the new
secret. Then remove the old one. Look up any order whose webhook was rejected during the switch
(`GET /merchant-order/integration/v1/orders/{id}`) instead of waiting for a retry.

## The webhook endpoint

- Serve it over HTTPS only.
- Verify the signature over the raw body before anything else ([callbacks.md](callbacks.md)).
  Compare in constant time: `hash_equals` in PHP, `crypto.timingSafeEqual` in Node.js after a
  length check, `hmac.compare_digest` in Python.
- Trust only the verified body. Query strings, the payer's return to `success_url` and form
  fields from the browser prove nothing.
- If the secret is missing from the configuration, answer `503`, not `401`, so Paysera retries
  once you fix it.
- Answer fast and process heavy work asynchronously.

## Money moves

- Mark orders paid only by the rule in [callbacks.md](callbacks.md).
- Every refund carries an `Idempotency-Key` stored before the call; retries reuse it, and a
  `failed` refund is never followed by a new one on its own ([refunds.md](refunds.md)).
- Restrict who in the merchant's own system can trigger a refund, and record who did.

## Your agent

An agent using this skill writes code. It does not create live orders, does not call the
refund endpoint, and never asks for or prints the `client_secret`.
