# Webhooks (callbacks)

Paysera sends a signed `POST` with a JSON body to the order's `redirect_urls.callback_url`.
This is server-to-server; it is not the payer's browser returning to `success_url`.

## Events

Route on `event.type` and `event.name` inside the body. There is no event header.

| Event (`type:name`) | Body | Meaning |
|---|---|---|
| `order:amount_paid_updated` | full order snapshot | The amount paid on the order changed. **The only event that can mark an order paid.** |
| `order:status_updated` | thin order | The order was cancelled (sent only for cancellation). |
| `payment:status_updated` | thin payment | One payment's status changed. |
| `refund:status_updated` | thin refund | A refund's status changed. |

There is no `order.paid` or `payment_link.completed` event. Treat any event you do not know
as a no-op and answer 2xx. Split-payment integrations also receive distribution events with a
different, flat body (no `event` object); see the public webhook reference if you use split
payments.

<!-- names: webhook order:amount_paid_updated -->
```json
{
  "version": 1,
  "event": {"type": "order", "name": "amount_paid_updated"},
  "order": {
    "paysera_order_id": "019ed03a-84f0-7ba0-874a-f7473738875b",
    "merchant_order_id": "INV-1001",
    "amount": 2500,
    "amount_paid": 2500,
    "payer_sets_amount": false,
    "minimum_amount": null,
    "maximum_amount": null,
    "tip_amount": null,
    "currency": "EUR",
    "status": "paid"
  },
  "timestamp": 1736433570
}
```

<!-- names: webhook refund:status_updated -->
```json
{
  "version": 1,
  "event": {"type": "refund", "name": "status_updated"},
  "order": {"paysera_order_id": "019ed03a-84f0-7ba0-874a-f7473738875b", "merchant_order_id": "INV-1001"},
  "refund": {"id": "1a3b5c7d-2e4f-4a6b-8c0d-3e5f7a9b1c2d", "payment_id": "019ed03a-8f12-7503-8369-9c01999bf6cb", "status": "completed", "amount": 2500, "currency": "EUR"},
  "timestamp": 1736440600
}
```

The full payloads, including payment links and payments inside the order snapshot, are in the
public reference: https://developers.paysera.com/guides/checkout-modern/reference/webhook-events.

## Headers

| Header | Use |
|---|---|
| `X-Paysera-Signature` | Hex HMAC-SHA256 of the raw body. Verify it. |
| `X-Paysera-Signature-Alg` | `HMAC-SHA256` |
| `X-Paysera-Created-At` | Unix time the webhook was generated |
| `X-Paysera-Request-Id` | Request identifier for tracing |
| `X-Paysera-Callback-Id` | Callback identifier |

## Verify the signature

1. Read the **raw** body bytes. Do not parse and re-serialise it.
2. Compute HMAC-SHA256 of those bytes with the `client_secret` as key and hex-encode it.
3. Compare it with `X-Paysera-Signature` in constant time.
4. If the secret is not configured, answer `503` so Paysera retries later. Never answer `401`
   for your own configuration fault: `401` stops the retries.

<!-- snippet: verify-php -->
```php
<?php

function verifyPayseraSignature(string $rawBody, string $signatureHeader): bool
{
    $secret = getenv('PAYSERA_CLIENT_SECRET');
    if ($secret === false || $secret === '') {
        throw new RuntimeException('PAYSERA_CLIENT_SECRET is not set');
    }
    $expected = hash_hmac('sha256', $rawBody, $secret);

    return hash_equals($expected, strtolower(trim($signatureHeader)));
}
```

```php
<?php

$rawBody = file_get_contents('php://input');
try {
    $valid = verifyPayseraSignature($rawBody, $_SERVER['HTTP_X_PAYSERA_SIGNATURE'] ?? '');
} catch (RuntimeException $exception) {
    http_response_code(503);
    exit;
}
if (!$valid) {
    http_response_code(401);
    exit;
}
```

<!-- snippet: verify-node -->
```js
const crypto = require('crypto');

function verifyPayseraSignature(rawBody, signatureHeader) {
  const secret = process.env.PAYSERA_CLIENT_SECRET;
  if (!secret) {
    throw new Error('PAYSERA_CLIENT_SECRET is not set');
  }
  const expected = Buffer.from(crypto.createHmac('sha256', secret).update(rawBody).digest('hex'));
  const received = Buffer.from(String(signatureHeader || '').trim().toLowerCase());
  return received.length === expected.length && crypto.timingSafeEqual(received, expected);
}
```

In Express, read the body with `express.raw({ type: 'application/json' })` on the webhook
route, so `req.body` is the raw `Buffer`, not a parsed object.

<!-- snippet: verify-python -->
```python
import hashlib
import hmac
import os


def verify_paysera_signature(raw_body: bytes, signature_header: str) -> bool:
    secret = os.environ.get("PAYSERA_CLIENT_SECRET", "")
    if not secret:
        raise RuntimeError("PAYSERA_CLIENT_SECRET is not set")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature_header or "").strip().lower())
```

## When an order is paid

Act on `order:amount_paid_updated` only, after the signature check, and only when all of these hold:

- `order.status` is `paid`. A snapshot also arrives for a partial payment; that one changes
  nothing.
- `order.merchant_order_id` is an order you created, and `order.currency` equals the currency
  you stored.
- Fixed amount: `order.amount` equals the amount you stored. Payer-set amount
  (`order.payer_sets_amount` is true): `order.amount` is what the payer entered, so check it
  is within the limits you set.
- The order is not already paid. An older snapshot never moves a paid order back.

Do not compare `order.amount_paid` with `order.amount`: `amount_paid` includes any tip, so a
tipped order that is still partly unpaid can pass that comparison.

## Process each change once

Deliveries repeat (up to 4 attempts over about 31 hours) and can arrive out of order. Make the
state change itself idempotent with one conditional update, and act only when it changed a row:

```sql
UPDATE shop_orders
   SET status = 'paid', paid_at = CURRENT_TIMESTAMP
 WHERE reference = :merchant_order_id
   AND currency = :currency
   AND status = 'pending'
   AND ((payer_sets_amount = 0 AND amount = :amount)
     OR (payer_sets_amount = 1 AND :amount BETWEEN minimum_amount AND maximum_amount));
```

The public guide also suggests de-duplicating on `X-Paysera-Callback-Id`. You can store it
under a unique index in the same transaction, but do not rely on it alone: the conditional
update above is what keeps a repeated or late delivery harmless.

Answer within about 10 seconds. Paysera allows 3 seconds to connect and 10 to read the answer;
anything slower counts as a failed attempt. If processing takes longer, store the verified body
and answer `200` first.

| Your answer | Paysera does |
|---|---|
| any 2xx | stops retrying |
| `401` | stops retrying (treated as a rejected signature) |
| anything else, or a timeout | retries after about 1 h, 5 h and 25 h |

## A handler without the SDK

<!-- snippet: handler-python -->
```python
import json


def handle_paysera_webhook(raw_body: bytes, headers, orders) -> int:
    try:
        valid = verify_paysera_signature(raw_body, headers.get("X-Paysera-Signature", ""))
    except RuntimeError:
        return 503
    if not valid:
        return 401
    payload = json.loads(raw_body)
    event = payload.get("event") or {}
    kind = "{}:{}".format(event.get("type"), event.get("name"))
    order = payload.get("order") or {}
    if kind == "order:amount_paid_updated" and order.get("status") == "paid":
        orders.mark_paid_once(order["merchant_order_id"], order["currency"], order["amount"])
    elif kind == "order:status_updated" and order.get("status") in ("cancelled", "canceled"):
        orders.cancel_unless_paid(order["merchant_order_id"])
    elif kind in ("payment:status_updated", "refund:status_updated"):
        orders.record_status(kind, payload)
    return 200
```

`orders.mark_paid_once()` runs the conditional update above. Keep `cancel_unless_paid()`
conditional too: a cancellation can arrive after a payment.

## PHP SDK handler

`processCallback()` verifies the signature and returns a typed object for the order events. It
reads the `client_secret` from the SDK's credentials repository, which is empty in a fresh
webhook request unless you give the builder one. This handler routes on the event name, so it
keeps working when a later SDK version adds typed objects for more events.

```php
<?php

use Paysera\CheckoutSdk\Entity\OrderAmountPaidCallback;
use Paysera\CheckoutSdk\Entity\PaymentApiCredentials;
use Paysera\CheckoutSdk\Exception\CallbackBuildIntegrationException;
use Paysera\CheckoutSdk\Exception\CallbackVerificationIntegrationException;
use Paysera\CheckoutSdk\Repository\PaymentApiCredentialsRepositoryInterface;
use Paysera\CheckoutSdk\SdkFacadeBuilder;
use Psr\Http\Message\ResponseInterface;
use Psr\Http\Message\ServerRequestInterface;

final class EnvCredentialsRepository implements PaymentApiCredentialsRepositoryInterface
{
    public function save(PaymentApiCredentials $apiCredentials): void
    {
    }

    public function retrieve(): ?PaymentApiCredentials
    {
        $clientId = getenv('PAYSERA_CLIENT_ID');
        $clientSecret = getenv('PAYSERA_CLIENT_SECRET');
        if (!$clientId || !$clientSecret) {
            return null;
        }

        return new PaymentApiCredentials($clientId, $clientSecret);
    }
}

final class ShopOrders
{
    private PDO $db;

    public function __construct(PDO $db)
    {
        $this->db = $db;
    }

    public function rememberPayseraOrder(string $reference, string $payseraOrderId, int $amount, string $currency): void
    {
        $statement = $this->db->prepare(
            'UPDATE shop_orders SET paysera_order_id = ?, amount = ?, currency = ? WHERE reference = ?'
        );
        $statement->execute([$payseraOrderId, $amount, $currency, $reference]);
    }

    public function markPaidOnce(string $reference, string $currency, int $amount): bool
    {
        $statement = $this->db->prepare(
            "UPDATE shop_orders SET status = 'paid', paid_at = CURRENT_TIMESTAMP
              WHERE reference = ? AND currency = ? AND status = 'pending'
                AND ((payer_sets_amount = 0 AND amount = ?)
                  OR (payer_sets_amount = 1 AND ? BETWEEN minimum_amount AND maximum_amount))"
        );
        $statement->execute([$reference, $currency, $amount, $amount]);

        return $statement->rowCount() === 1;
    }
}

function handlePayseraWebhook(ServerRequestInterface $request, ResponseInterface $response, ShopOrders $orders): ResponseInterface
{
    $credentials = new EnvCredentialsRepository();
    if ($credentials->retrieve() === null) {
        return $response->withStatus(503);
    }
    $sdk = (new SdkFacadeBuilder())->setPaymentApiCredentialsRepository($credentials)->build();

    try {
        $callback = $sdk->getCallbacksFacade()->processCallback($request);
    } catch (CallbackVerificationIntegrationException $exception) {
        return $response->withStatus(401);
    } catch (CallbackBuildIntegrationException $exception) {
        return $response->withStatus(400);
    }

    $event = $callback->getEvent()->getType() . ':' . $callback->getEvent()->getName();
    if ($event === 'order:amount_paid_updated' && $callback instanceof OrderAmountPaidCallback) {
        $order = $callback->getOrder();
        $info = $order->getOrderInfo();
        if ($order->isFullyPaid() && $orders->markPaidOnce($info->getMerchantOrderId(), $info->getCurrency(), $info->getAmount())) {
            fulfil($info->getMerchantOrderId());
        }
    } elseif ($event === 'payment:status_updated' || $event === 'refund:status_updated') {
        $payload = json_decode((string) $request->getBody(), true);
        recordStatus($event, $payload);
    }

    return $response->withStatus(200);
}
```

- `fulfil()` and `recordStatus()` stand for your own code.
- `processCallback()` throws `CallbackBuildIntegrationException` for a body without an `event`
  object, such as a split-payment distribution event. If you use split payments, check the
  signature yourself (snippet above) and handle those bodies before calling it.
- For `order:status_updated` the SDK returns `OrderStatusUpdatedCallback`; act only when
  `isCancelled()` is true, and never cancel an order you already marked paid.
