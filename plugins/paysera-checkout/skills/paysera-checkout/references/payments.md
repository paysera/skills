# Payments: token, order, payment link, redirect

A payment is always three API calls: get a token, create an order, create a payment link for
that order. The payer pays on the page the link's `payment_URL` opens. The result reaches you
only through a webhook ([callbacks.md](callbacks.md)).

Amounts are integers in minor units (`2500` = 25.00 EUR). Currencies are ISO 4217 codes.

## 1. Token

`POST https://api.paysera.com/auth/realms/Paysera/protocol/openid-connect/token` with the
OAuth2 client-credentials grant (RFC 6749). Send the token as `Authorization: Bearer …` on
every API call. Cache it until it expires instead of asking for a new one per request.

```sh
curl -X POST https://api.paysera.com/auth/realms/Paysera/protocol/openid-connect/token \
  -d grant_type=client_credentials \
  -d client_id="$PAYSERA_CLIENT_ID" \
  -d client_secret="$PAYSERA_CLIENT_SECRET"
```

The answer carries `access_token` and `expires_in`.

## 2. Order

`POST /merchant-order/integration/v1/orders`. Only `purchase` is required; set
`redirect_urls` too, because `callback_url` is the address Paysera sends webhooks to.

```sh
curl -X POST https://api.paysera.com/merchant-order/integration/v1/orders \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"purchase": {"reference": "INV-1001", "amount": 2500, "currency": "EUR"}, "redirect_urls": {"success_url": "https://shop.example.com/checkout/return", "failure_url": "https://shop.example.com/checkout/failed", "cancel_url": "https://shop.example.com/cart", "callback_url": "https://shop.example.com/paysera/webhook"}}'
```

<!-- names: response POST /merchant-order/integration/v1/orders -->
```json
{
  "order_id": "019ed03a-84f0-7ba0-874a-f7473738875b",
  "purchase": {"reference": "INV-1001", "amount": 2500, "currency": "EUR"},
  "created_at": 1736433270
}
```

- `purchase.reference` is your own order reference. It reaches the bank payment purpose, so it
  allows only `A-Za-z0-9`, space and `_ / ? : ( ) . , ' + -`. Webhooks return it as
  `merchant_order_id`.
- Store `order_id`, your reference, the amount and the currency before you redirect. The
  webhook handler compares the paid order with these values.
- `success_url`, `failure_url` and `cancel_url` only decide which page the payer sees. They
  prove nothing about payment.
- Payer-set amounts (donations), amount buttons and tips are optional order features; see the
  public guide: https://developers.paysera.com/guides/checkout-modern/api-integration/payment-orders.

## 3. Payment link and redirect

`POST /checkout-payment-link/integration/v1/payment-links` for the `order_id`. Required:
`order_id`, `name`, `purchase` and `experience.language`.

```sh
curl -X POST https://api.paysera.com/checkout-payment-link/integration/v1/payment-links \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"order_id": "019ed03a-84f0-7ba0-874a-f7473738875b", "name": "INV-1001", "purchase": {"amount": 2500}, "experience": {"language": "en"}}'
```

<!-- names: response POST /checkout-payment-link/integration/v1/payment-links -->
```json
{
  "link_id": "019ed03a-8591-7197-9aad-d9c029286b77",
  "order_id": "019ed03a-84f0-7ba0-874a-f7473738875b",
  "payment_URL": "<the payment page address>",
  "expired_at": 1736692470
}
```

Redirect the payer to `payment_URL` (note the upper-case `URL`). Never build the address
yourself.

## PHP SDK

The SDK wraps the same three calls. `initiatePayment()` creates the order and its link and
returns the link; `getPaymentUrl()` gives the redirect address. Use the typed
`buildPaymentOrderCreateRequestFromValues()`; its `Metadata` needs your site's address as the
referer.

```php
<?php

use Paysera\CheckoutSdk\Entity\Metadata;
use Paysera\CheckoutSdk\Entity\PaymentApiCredentials;
use Paysera\CheckoutSdk\Entity\PaymentOrder\Purchase;
use Paysera\CheckoutSdk\Entity\PaymentOrder\RedirectUrls;
use Paysera\CheckoutSdk\SdkFacadeBuilder;

$sdk = (new SdkFacadeBuilder())->build();
$sdk->getAuthorizationFacade()->authorize(
    new PaymentApiCredentials(getenv('PAYSERA_CLIENT_ID'), getenv('PAYSERA_CLIENT_SECRET'))
);

$payments = $sdk->getPaymentsFacade();
$orderRequest = $payments->buildPaymentOrderCreateRequestFromValues(
    new Purchase('INV-1001', 2500, 'EUR'),
    new Metadata('https://shop.example.com'),
    new RedirectUrls(
        'https://shop.example.com/checkout/return',
        'https://shop.example.com/checkout/failed',
        'https://shop.example.com/paysera/webhook',
        'https://shop.example.com/cart'
    )
);
$linkRequest = $payments->buildPaymentLinkCreateRequest([
    'name' => 'INV-1001',
    'metadata' => ['referer' => 'https://shop.example.com'],
    'experience' => ['language' => 'en'],
]);

$link = $payments->initiatePayment($orderRequest, $linkRequest);

$shopOrders->rememberPayseraOrder('INV-1001', $link->getOrderId(), 2500, 'EUR');
header('Location: ' . $link->getPaymentUrl(), true, 303);
```

- `RedirectUrls` takes success, failure, callback (webhook) and an optional cancel address, in
  that order.
- The SDK keeps the token in memory by default. For a long-running process or several
  requests, give the builder a persistent token repository
  (`setPaymentApiAuthTokenRepository()`) so you do not ask for a token per request.
- `$shopOrders` stands for your own order storage; `rememberPayseraOrder()` is shown in
  [callbacks.md](callbacks.md).

## What comes next

Wait for `order:amount_paid_updated` on your `callback_url`. The return to `success_url` can
come before, after, or never.
