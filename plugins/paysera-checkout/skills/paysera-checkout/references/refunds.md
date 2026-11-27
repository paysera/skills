# Refunds

Refunds use the payment, not the order: you need the payment's id. It is in the order snapshot
webhook (`order.payment_links[].payments[].id`) and in `payment:status_updated` (`payment.id`).

The PHP SDK 3.13.1 has no client for this refund API, so PHP integrations call it over REST
too; the SDK can still supply the access token.

## 1. Check eligibility

`GET /payment-executor/integration/v1/payments/{paymentId}/refunds/eligibility`

<!-- names: response GET /payment-executor/integration/v1/payments/{paymentId}/refunds/eligibility -->
```json
{
  "eligible": true,
  "reason": null,
  "remaining_refundable": 2500,
  "currency": "EUR",
  "min_refund_amount": 1,
  "max_refund_amount": 2500,
  "requires_manual_payer_data": false
}
```

- Refund only when `eligible` is true, and only an amount between `min_refund_amount` and
  `max_refund_amount`.
- When `requires_manual_payer_data` is true, collect the payer's IBAN and name and send both as
  `payer_iban` and `payer_name`. Otherwise leave them out.
- Read eligibility right before the refund; do not reuse an old answer.

## 2. Create the refund with your own Idempotency-Key

`POST /payment-executor/integration/v1/payments/{paymentId}/refunds` with the header
`Idempotency-Key`.

1. Create the key once per refund you intend to make (for example a random UUID) and **store it
   with the refund record before the call**.
2. Send the request.
3. On a timeout, a network error or a `5xx`, retry with the **same** key and the same amount and
   currency. Paysera then returns the refund it already created (`200`) instead of a second one.
   The first creation returns `201`.
4. The same key with a different amount or currency returns `409`; that is a bug, not a retry
   case. `422` means the payment is not eligible.

```sh
curl -X POST https://api.paysera.com/payment-executor/integration/v1/payments/$PAYMENT_ID/refunds \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $REFUND_IDEMPOTENCY_KEY" \
  -d '{"amount": 2500, "currency": "EUR", "reference": "INV-1001 refund"}'
```

<!-- names: response POST /payment-executor/integration/v1/payments/{paymentId}/refunds -->
```json
{
  "refund_id": "1a3b5c7d-2e4f-4a6b-8c0d-3e5f7a9b1c2d",
  "payment_id": "019ed03a-8f12-7503-8369-9c01999bf6cb",
  "amount": 2500,
  "currency": "EUR",
  "status": "initiated",
  "idempotency_key": "7f3c1e90-1b2a-4d5e-9f8a-0c1d2e3f4a5b",
  "created_at": 1736440000
}
```

```php
<?php

use Paysera\CheckoutSdk\Entity\PaymentApiCredentials;
use Paysera\CheckoutSdk\SdkFacadeBuilder;

function createPayseraRefund(string $paymentId, int $amount, string $currency, string $idempotencyKey): array
{
    $sdk = (new SdkFacadeBuilder())->build();
    $token = $sdk->getAuthorizationFacade()->authorize(
        new PaymentApiCredentials(getenv('PAYSERA_CLIENT_ID'), getenv('PAYSERA_CLIENT_SECRET'))
    );

    $curl = curl_init("https://api.paysera.com/payment-executor/integration/v1/payments/{$paymentId}/refunds");
    curl_setopt_array($curl, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 30,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $token->getAccessToken(),
            'Content-Type: application/json',
            'Idempotency-Key: ' . $idempotencyKey,
        ],
        CURLOPT_POSTFIELDS => json_encode(['amount' => $amount, 'currency' => $currency]),
    ]);
    $body = curl_exec($curl);
    $status = curl_getinfo($curl, CURLINFO_RESPONSE_CODE);
    curl_close($curl);

    if ($body === false || $status >= 500) {
        throw new RuntimeException('Refund outcome unknown: retry with the same Idempotency-Key');
    }
    if ($status !== 200 && $status !== 201) {
        throw new RuntimeException('Refund refused with HTTP ' . $status . ': ' . $body);
    }

    return json_decode($body, true);
}
```

`$idempotencyKey` comes from your refund record, created before the first attempt
(`bin2hex(random_bytes(16))` or a UUID), never inside this function.

## 3. Follow the refund to its end

A new refund is `initiated` or `processing`; money has not moved yet. The status ends as
`completed` or `failed`, reported by `refund:status_updated`
([callbacks.md](callbacks.md)) or by reading the refund:
`GET /payment-executor/integration/v1/payments/{paymentId}/refunds/{refundId}`.

- **Never create a new refund because one reported `failed`.** A refund can read `failed` while
  Paysera still pays it out, and nothing in the answer tells the two apart. Refund again only
  after Paysera support confirms that no money moved.
- A `completed` status wins over a later `failed` for the same refund; deliveries can arrive
  out of order.

Public guide: https://developers.paysera.com/guides/checkout-modern/api-integration/refunds
