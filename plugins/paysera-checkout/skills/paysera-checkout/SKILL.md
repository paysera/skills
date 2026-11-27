---
name: paysera-checkout
description: Build or review a merchant's Paysera Checkout integration — accept card, bank-link and wallet payments on a website, app or invoice, handle Paysera webhooks (callbacks), refunds and payment links, with the PHP SDK or the REST API. Use when asked to add Paysera payments to a shop or checkout, verify a Paysera webhook signature, mark orders paid, refund a Paysera payment, create a Paysera payment link, or migrate from Paysera Checkout Classic (lib-webtopay, ss1/ss2) to Checkout Modern. Keywords — Paysera Checkout, Checkout Modern, payment gateway, webhook, callback, refund, payment link, apmokėjimas, grąžinimas. Do NOT use for — sending money from a Paysera account (use paysera-payments), or support for one failed payment (contact Paysera support).
---

# Paysera Checkout integration

This skill helps you write the merchant's own integration code for **Paysera Checkout
Modern**: create an order, send the payer to the payment page, learn the result from a
signed webhook, and refund. It holds no credentials and calls no API by itself.

It targets the PHP SDK `paysera/lib-checkout-integration-sdk` 3.13.1 (`composer require
paysera/lib-checkout-integration-sdk:^3.13`), the newest version on Packagist when this
skill was published. Everything else uses the public REST API at `https://api.paysera.com`.

## Rules for you, the agent

- Write code; do not run payments yourself. Never create a live order or call the refund
  endpoint on the user's behalf. Run a test call only when the user asks, and only against
  a project in Test Mode.
- Never ask for, print, log or store the user's `client_secret`. Code reads it from the
  server environment (`PAYSERA_CLIENT_SECRET`) or the user's secret manager.
- Use only the names in these references. If you need an endpoint, field or event that is
  not here, read the public guide at https://developers.paysera.com/guides/checkout-modern
  instead of guessing.

## Modern or Classic

- **A new custom integration: always Checkout Modern.** Everything below is Modern.
- **Checkout Classic** (`lib-webtopay`, signed `ss1`/`ss2` request parameters) is only for a
  site that already runs it. To move such a site, follow the public migration guide:
  https://developers.paysera.com/guides/checkout-modern/migrating-from-classic. This skill
  has no Classic code.
- **A shop on a CMS or e-commerce platform:** check the official plugins page first —
  https://developers.paysera.com/guides/checkout-modern/getting-started/plugins. It names
  the right plugin per platform (for some platforms that is still the Classic extension),
  and a plugin needs no custom code.

## Step order

1. **Credentials.** The merchant copies the project's `client_id` and `client_secret` from
   the Paysera merchant portal into the server environment. The `client_secret` also signs
   every webhook.
2. **Test Mode.** Switch the project to Test Mode and add the site's URL to the project's
   websites. Test orders from a URL the project does not list are rejected. There is no
   separate sandbox: tests run against `https://api.paysera.com`.
   → [test-mode.md](references/test-mode.md)
3. **Token.** Get an access token with the OAuth2 client-credentials grant.
   → [payments.md](references/payments.md)
4. **Order.** Create the order with your reference, amount, currency and
   `redirect_urls` — `callback_url` is where Paysera sends webhooks.
5. **Payment link and redirect.** Create a payment link for that `order_id` and redirect the
   payer to its `payment_URL`. Every payment needs this step. In PHP, `initiatePayment()`
   creates both and `getPaymentUrl()` returns the URL.
6. **Webhook endpoint.** Verify the signature, apply the change once, answer 2xx.
   → [callbacks.md](references/callbacks.md)
7. **Mark the order paid** only as rule 2 below says.
8. **Refunds.** → [refunds.md](references/refunds.md)
9. **Go live.** Switch the project from Test Mode to Live. The code does not change.

Invoices or one-off payment requests with no shop use the same order and link calls →
[payment-links.md](references/payment-links.md).

## Rules every integration follows

1. **Verify every webhook before you read it.** Compute HMAC-SHA256 over the **raw** request
   body with the `client_secret`, hex-encode it and compare it with `X-Paysera-Signature`
   in constant time. Reject a mismatch before parsing the body.
2. **An order is paid only after a verified `order:amount_paid_updated`** whose order
   status is `paid` (SDK: `isFullyPaid()`), for an order you know by `merchant_order_id`, in
   the currency you stored. For a fixed-amount order the amount must equal the one you
   stored; for a payer-set order it must be within the limits you set. Never mark an order
   paid when the payer returns to `success_url`: that redirect is only for the page the
   payer sees. Do not compare the raw amount paid with the order amount yourself, because the
   amount paid includes tips.
3. **Webhooks are idempotent.** Deliveries repeat and arrive out of order. Apply each change
   as one conditional update (set paid only if not yet paid; never move a paid order back).
   Answer duplicates and unknown events with 2xx, within about 10 seconds.
4. **Refunds carry your own `Idempotency-Key`.** Store the key before the call and reuse it
   on every retry of the same refund. Never start a new refund because one reported
   `failed`: the money may still be paid out.
5. **No secrets in code, repositories, logs or the browser.** Examples read them from the
   environment.

## References

| File | Open it for |
|------|-------------|
| [payments.md](references/payments.md) | Token, order, payment link, redirect — PHP SDK and REST |
| [callbacks.md](references/callbacks.md) | Webhook events, signature check, the paid rule, idempotency, SDK handler |
| [refunds.md](references/refunds.md) | Eligibility, creating a refund safely, refund status |
| [payment-links.md](references/payment-links.md) | Link options and invoices without a shop |
| [test-mode.md](references/test-mode.md) | Testing without real money |
| [security.md](references/security.md) | Handling `client_secret`, endpoint hardening, rotation |

Public documentation: https://developers.paysera.com/guides/checkout-modern
