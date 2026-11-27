import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkout_names as cn

F = "\x60"
FENCE = F * 3

SNAPSHOT = {
    "sdk": {
        "version": "3.13.1",
        "classes": [
            "Paysera\\CheckoutSdk\\SdkFacade",
            "Paysera\\CheckoutSdk\\Entity\\OrderAmountPaidCallback",
        ],
        "methods": ["getCallbacksFacade", "processCallback", "getEvent", "getType", "initiatePayment", "getPaymentUrl", "initiateRefundOrder"],
        "constants": {"Paysera\\CheckoutSdk\\Entity\\OrderAmountPaidCallback": ["SOME_CONSTANT"]},
        "operations": ["POST /auth/realms/Paysera/protocol/openid-connect/token"],
    },
    "openapi": {
        "operations": {
            "POST /checkout-payment-link/integration/v1/payment-links": {
                "request": ["order_id", "lifetime", "experience", "experience.language", "metadata", "metadata.*"],
                "response": ["payment_URL", "link_id"],
            },
            "POST /merchant-order/integration/v1/orders": {"request": ["purchase", "purchase.amount"], "response": ["order_id"]},
        }
    },
    "guides": {
        "operations": {
            "PUT /checkout-payment-link/integration/v1/payment-links/{}": {"request": ["link_lifetime", "language"], "response": []},
            "POST /payment-executor/integration/v1/payments/{}/refunds": {"request": ["amount", "currency"], "response": ["refund_id"]},
        },
        "events": {
            "order:amount_paid_updated": ["event", "event.type", "event.name", "order", "order.status"],
            "refund:status_updated": ["event", "refund", "refund.status"],
        },
        "headers": ["X-Paysera-Signature", "Idempotency-Key"],
    },
    "standard_fields": ["client_secret"],
    "known_wrong": cn.KNOWN_WRONG,
}


def problems(text):
    checker = cn.Checker(cn.Truth(SNAPSHOT))
    checker.check_text(text, "sample.md")
    return checker.problems


def block(lang, body, annotation=None):
    head = "<!-- names: {} -->\n".format(annotation) if annotation else ""
    return "{}{}{}\n{}\n{}\n".format(head, FENCE, lang, body, FENCE)


MUST_FLAG = [
    ("dotted event in code", block("php", "if ($type === 'order.paid') {}")),
    ("old link event in prose", "Listen for {0}payment_link.completed{0}.".format(F)),
    ("removed SDK method", block("php", "$sdk->getCallbacksFacade()->processPaymentCallback($request);")),
    ("legacy refund method", block("php", "$refunds->initiateRefundOrder($request);")),
    ("link_lifetime in a create curl body", block(
        "sh",
        "curl -X POST https://api.paysera.com/checkout-payment-link/integration/v1/payment-links \\\n  -d '{\"order_id\": \"x\", \"link_lifetime\": 60}'",
    )),
    ("top-level language in a create request block", block(
        "json", '{"order_id": "x", "language": "en"}', "request POST /checkout-payment-link/integration/v1/payment-links"
    )),
    ("payment_url in a PHP key access", block("php", "header('Location: ' . $link['payment_url']);")),
    ("payment_url in a create response block", block(
        "json", '{"payment_url": "https://example.com"}', "response POST /checkout-payment-link/integration/v1/payment-links"
    )),
    ("invented refund path", "{0}POST /merchant-order/integration/v1/orders/{{id}}/refunds{0}".format(F)),
    ("unannotated json block", block("json", '{"order_id": "x"}')),
    ("nonexistent header", block("sh", "curl -H 'X-Paysera-Event: order' https://example.com")),
    ("unknown webhook event", "Handle {0}payment:completed{0}.".format(F)),
    ("wrong SDK version", "Targets {0}paysera/lib-checkout-integration-sdk{0} 3.17.0.".format(F)),
    ("unknown SDK class", block("php", "use Paysera\\CheckoutSdk\\Entity\\PaymentStatusUpdatedCallback;")),
]

MUST_PASS = [
    ("create link curl body", block(
        "sh",
        "curl -X POST https://api.paysera.com/checkout-payment-link/integration/v1/payment-links \\\n  -d '{\"order_id\": \"x\", \"lifetime\": 60, \"experience\": {\"language\": \"en\"}}'",
    )),
    ("update link uses flat names", block(
        "json", '{"link_lifetime": 60, "language": "en"}', "request PUT /checkout-payment-link/integration/v1/payment-links/{id}"
    )),
    ("free-form metadata", block(
        "json", '{"order_id": "x", "metadata": {"anything": "y"}}', "request POST /checkout-payment-link/integration/v1/payment-links"
    )),
    ("payment_URL access", block("php", "header('Location: ' . $link['payment_URL']);")),
    ("SDK calls", block("php", "use Paysera\\CheckoutSdk\\SdkFacade;\n$url = $sdk->initiatePayment($order, $link)->getPaymentUrl();")),
    ("known event and header", "Verify {0}X-Paysera-Signature{0}, then handle {0}order:amount_paid_updated{0}.".format(F)),
    ("refund path with interpolation", block("php", "$path = \"/payment-executor/integration/v1/payments/{$paymentId}/refunds\";")),
    ("negated known-wrong name in prose", "There is no {0}order.paid{0} event.".format(F)),
    ("webhook payload", block("json", '{"event": {"type": "order", "name": "amount_paid_updated"}, "order": {"status": "paid"}}', "webhook order:amount_paid_updated")),
    ("right SDK version", "Targets {0}paysera/lib-checkout-integration-sdk{0} 3.13.1.".format(F)),
    ("token endpoint", "{0}POST https://api.paysera.com/auth/realms/Paysera/protocol/openid-connect/token{0}".format(F)),
]


class TestCheckoutNames(unittest.TestCase):
    def test_must_flag(self):
        for label, text in MUST_FLAG:
            with self.subTest(label):
                self.assertTrue(problems(text), "not flagged: " + label)

    def test_must_pass(self):
        for label, text in MUST_PASS:
            with self.subTest(label):
                self.assertEqual(problems(text), [], label)

    def test_the_typed_builder_is_not_mistaken_for_the_deprecated_one(self):
        self.assertEqual(problems("Use {0}buildPaymentOrderCreateRequestFromValues{0}.".format(F)), [])
        self.assertTrue(problems("Use {0}buildPaymentOrderCreateRequest{0}.".format(F)))

    def test_path_normalisation(self):
        for raw in ("/a/v1/payments/{paymentId}/refunds", "/a/v1/payments/{$id}/refunds", "/a/v1/payments/${id}/refunds?x=1"):
            self.assertEqual(cn.normalise_path(raw), "/a/v1/payments/{}/refunds")


class TestTheRealSkill(unittest.TestCase):
    def test_the_published_skill_passes_against_the_committed_snapshot(self):
        self.assertEqual(cn.check_skill(cn.load_snapshot()), [])

    def test_the_snapshot_names_only_public_sources(self):
        text = cn.SNAPSHOT.read_text(encoding="utf-8")
        for marker in ("/home/", "gitlab", "paysera" + ".net", "localhost"):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
