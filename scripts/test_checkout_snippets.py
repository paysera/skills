import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CALLBACKS = ROOT / "plugins" / "paysera-checkout" / "skills" / "paysera-checkout" / "references" / "callbacks.md"
SNIPPET = re.compile(r"<!-- snippet: ([\w-]+) -->\n\x60{3}\w*\n(.*?)^\x60{3}\s*$", re.M | re.S)

SIGNING_KEY = "dummy-signing-key-for-tests"
BODY = b'{"event":{"type":"order","name":"amount_paid_updated"},"order":{"status":"paid"}}'


def snippets():
    return dict(SNIPPET.findall(CALLBACKS.read_text(encoding="utf-8")))


def sign(body, key=SIGNING_KEY):
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


CASES = [
    ("valid", BODY, sign(BODY), True),
    ("valid, upper-case hex", BODY, sign(BODY).upper(), True),
    ("signed with another key", BODY, sign(BODY, "another-key"), False),
    ("tampered body", BODY.replace(b"paid", b"pending"), sign(BODY), False),
    ("wrong length", BODY, sign(BODY)[:-2], False),
    ("empty header", BODY, "", False),
]


def python_namespace(*names):
    namespace = {}
    for name in names:
        exec(compile(snippets()[name], name, "exec"), namespace)
    return namespace


class FakeOrders:
    def __init__(self):
        self.calls = []

    def mark_paid_once(self, *args):
        self.calls.append(("paid",) + args)

    def cancel_unless_paid(self, *args):
        self.calls.append(("cancel",) + args)

    def record_status(self, kind, payload):
        self.calls.append(("status", kind))


def scratch_env(box):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": box, "TMPDIR": box, "TEMP": box, "TMP": box}
    env["PAYSERA_CLIENT_SECRET"] = SIGNING_KEY
    return env


def with_key(value):
    return mock.patch.dict(os.environ, {"PAYSERA_CLIENT_SECRET": value})


class TestPythonVerification(unittest.TestCase):
    def test_cases(self):
        verify = python_namespace("verify-python")["verify_paysera_signature"]
        with with_key(SIGNING_KEY):
            for label, body, signature, expected in CASES:
                with self.subTest(label):
                    self.assertIs(verify(body, signature), expected)

    def test_a_missing_key_is_an_error_not_a_rejection(self):
        verify = python_namespace("verify-python")["verify_paysera_signature"]
        with with_key(""):
            with self.assertRaises(RuntimeError):
                verify(BODY, sign(BODY))


class TestPythonHandler(unittest.TestCase):
    def handle(self, payload, signature=None, key=SIGNING_KEY):
        handler = python_namespace("verify-python", "handler-python")["handle_paysera_webhook"]
        body = json.dumps(payload).encode()
        orders = FakeOrders()
        headers = {"X-Paysera-Signature": sign(body) if signature is None else signature}
        with with_key(key):
            status = handler(body, headers, orders)
        return status, orders.calls

    def order_event(self, status, amount=2500, amount_paid=2500, tip=None):
        return {
            "event": {"type": "order", "name": "amount_paid_updated"},
            "order": {"merchant_order_id": "INV-1001", "currency": "EUR", "amount": amount,
                      "amount_paid": amount_paid, "tip_amount": tip, "status": status},
        }

    def test_a_paid_order_is_marked(self):
        self.assertEqual(self.handle(self.order_event("paid")), (200, [("paid", "INV-1001", "EUR", 2500)]))

    def test_a_tipped_partial_payment_is_not_paid(self):
        self.assertEqual(self.handle(self.order_event("pending_payment", 4000, 4100, 400)), (200, []))

    def test_a_payer_set_order_passes_the_entered_amount(self):
        payload = self.order_event("paid", 1800, 1800)
        payload["order"]["payer_sets_amount"] = True
        self.assertEqual(self.handle(payload), (200, [("paid", "INV-1001", "EUR", 1800)]))

    def test_a_bad_signature_is_401(self):
        self.assertEqual(self.handle(self.order_event("paid"), signature="0" * 64), (401, []))

    def test_a_missing_key_is_503(self):
        self.assertEqual(self.handle(self.order_event("paid"), key=""), (503, []))

    def test_unknown_and_status_events(self):
        self.assertEqual(self.handle({"event": {"type": "order", "name": "something_new"}}), (200, []))
        refund = {"event": {"type": "refund", "name": "status_updated"}, "refund": {"status": "completed"}}
        self.assertEqual(self.handle(refund), (200, [("status", "refund:status_updated")]))

    def test_a_cancellation(self):
        cancel = {"event": {"type": "order", "name": "status_updated"}, "order": {"merchant_order_id": "INV-1001", "status": "cancelled"}}
        self.assertEqual(self.handle(cancel), (200, [("cancel", "INV-1001")]))


class TestOtherLanguages(unittest.TestCase):
    def run_cases(self, command, program):
        cases = [[label, body.decode(), signature, expected] for label, body, signature, expected in CASES]
        with tempfile.TemporaryDirectory() as box:
            result = subprocess.run(
                command + [program], input=json.dumps(cases), capture_output=True, text=True,
                env=scratch_env(box), cwd=box, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [c[3] for c in cases])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node(self):
        program = snippets()["verify-node"] + """
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(([, body, sig]) => verifyPayseraSignature(Buffer.from(body), sig))));
"""
        self.run_cases(["node", "-e"], program)

    @unittest.skipUnless(shutil.which("php"), "php is not installed")
    def test_php(self):
        program = snippets()["verify-php"].replace("<?php", "", 1) + """
$cases = json_decode(stream_get_contents(STDIN), true);
echo json_encode(array_map(function ($c) { return verifyPayseraSignature($c[1], $c[2]); }, $cases));
"""
        self.run_cases(["php", "-r"], program)


if __name__ == "__main__":
    unittest.main()
