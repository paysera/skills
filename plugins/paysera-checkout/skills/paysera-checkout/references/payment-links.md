# Payment links

Every payment goes through a payment link ([payments.md](payments.md)). This page covers the
link options and the case without a shop: an invoice or a one-off payment request that you
send to the payer by email or chat.

## Invoice without a shop

1. Create an order with your invoice number as `purchase.reference` and a `callback_url` for
   webhooks. Success and failure URLs are optional here.
2. Create a payment link for that order and send its `payment_URL` to the payer.
3. Mark the invoice paid only on a verified `order:amount_paid_updated` whose order status is
   `paid` ([callbacks.md](callbacks.md)). A payer opening the link proves nothing.

<!-- names: request POST /checkout-payment-link/integration/v1/payment-links -->
```json
{
  "order_id": "019ed03a-84f0-7ba0-874a-f7473738875b",
  "name": "Invoice INV-1001",
  "lifetime": 604800,
  "purchase": {"amount": 12000},
  "experience": {"language": "en"},
  "payment_details": {"purpose": "Invoice INV-1001"},
  "payer_information": {"name": "Jonas Jonaitis", "email": "payer@example.com"}
}
```

## Create options

| Field | Meaning |
|---|---|
| `name` | Display name of the link (required) |
| `purchase.amount` | Amount in minor units; leave it out for a payer-set order |
| `experience.language` | Payment page language, ISO 639-1 (required) |
| `experience.payment_flow` | `paysera_checkout` (default, the payer picks a method) or `direct_payment` |
| `payment_details.key`, `payment_details.country_code` | The method and its bank country; required for `direct_payment` |
| `payment_details.purpose` | Payer-facing payment purpose, up to 255 characters (shown up to 140) |
| `payer_information.name`, `payer_information.email` | Prefill the payer's details |
| `lifetime` | Seconds until the link expires |

## Update a link

`PUT /checkout-payment-link/integration/v1/payment-links/{id}` uses a **flat** body with
different names: `amount`, `currency`, `language`, `payer` and `link_lifetime`. Do not send
these names to the create call, and do not send `lifetime` or `experience` to the update call.

Cancel a link with `PUT /checkout-payment-link/integration/v1/payment-links/{id}/cancel`.

The full parameter list is in the public guide:
https://developers.paysera.com/guides/checkout-modern/api-integration/payment-links
