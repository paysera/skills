"""Tests for cancel-payment.py.

Cancelling is destructive and irreversible, so the emphasis is on: never delete without
--confirm, never expose the token, and never act on a transfer whose state could not be
read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Work regardless of the runner's rootdir/sys.path handling (pytest from the repo root,
# unittest from this directory, or a plain `python3 test_cancel_payment.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testsupport import (
    SCRIPTS,
    assert_tempdir_is_empty,
    capture_curl,
    isolate_tempdir,
    load,
)

cancel = load("cancel-payment.py", "cancel_payment")

# Own temporary directory, required to be empty at the end — see _testsupport.
setUpModule = isolate_tempdir
tearDownModule = assert_tempdir_is_empty


class TestTokenHandling(unittest.TestCase):
    def test_token_never_appears_in_argv(self):
        secret = "SUPERSECRET-TOKEN"
        with capture_curl(cancel) as calls:
            cancel.curl_json("GET", "https://api.paysera.com/x", secret)
        self.assertNotIn(secret, " ".join(calls[0]["argv"]))
        self.assertIn(secret, calls[0]["input"])

    def test_requests_carry_a_timeout(self):
        with capture_curl(cancel) as calls:
            cancel.curl_json("GET", "https://api.paysera.com/x", "tok")
        self.assertEqual(calls[0]["kwargs"].get("timeout"), cancel.HTTP_TIMEOUT)

    def test_tokens_that_could_break_config_quoting_are_refused(self):
        for bad in ['has"quote', "has\\backslash"]:
            with self.subTest(token=bad):
                with mock.patch.dict(os.environ, {"PAYSERA_PAT": bad}):
                    with self.assertRaises(SystemExit):
                        cancel.read_token("/nonexistent")

    def test_empty_token_is_refused(self):
        with mock.patch.dict(os.environ, {"PAYSERA_PAT": "   "}):
            with self.assertRaises(SystemExit):
                cancel.read_token("/nonexistent")

    def test_a_token_file_readable_by_others_is_refused(self):
        # This token carries transfers:cancel — a local reader can delete pending drafts.
        # Mode 0600 was documented but nothing enforced it, and a plain `>` redirect under
        # the usual umask 022 leaves 0644.
        tmp = tempfile.mkdtemp(prefix="paysera-cancel-token-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "token")
        with open(path, "w") as f:
            f.write("a-token\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAYSERA_PAT", None)
            os.chmod(path, 0o600)
            self.assertEqual(cancel.read_token(path), "a-token")
            for mode in (0o644, 0o640, 0o604):
                with self.subTest(mode=oct(mode)):
                    os.chmod(path, mode)
                    with self.assertRaises(SystemExit) as raised:
                        cancel.read_token(path)
                    self.assertIn("chmod 600", str(raised.exception))


class TestTransportFailures(unittest.TestCase):
    def test_missing_curl_raises_http_error(self):
        with mock.patch.object(cancel.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(cancel.HttpError):
                cancel.curl_json("GET", "https://api.paysera.com/x", "tok")

    def test_timeout_raises_http_error(self):
        exc = subprocess.TimeoutExpired(cmd="curl", timeout=30)
        with mock.patch.object(cancel.subprocess, "run", side_effect=exc):
            with self.assertRaises(cancel.HttpError):
                cancel.curl_json("GET", "https://api.paysera.com/x", "tok")

    def test_nonzero_exit_raises_http_error(self):
        with capture_curl(cancel, returncode=7):
            with self.assertRaises(cancel.HttpError):
                cancel.curl_json("GET", "https://api.paysera.com/x", "tok")

    def test_non_json_body_is_returned_verbatim(self):
        with capture_curl(cancel, stdout="not json\nHTTP:502"):
            code, body = cancel.curl_json("GET", "https://api.paysera.com/x", "tok")
        self.assertEqual(code, "502")
        self.assertEqual(body, "not json")


class TestCancelableStates(unittest.TestCase):
    def test_live_states_are_cancelable(self):
        for state in ["new", "reserved", "registered", "waiting_funds", "signing"]:
            self.assertIn(state, cancel.CANCELABLE_STATES)

    def test_terminal_states_are_not(self):
        for state in ["done", "failed", "rejected", "canceled", "expired"]:
            self.assertNotIn(state, cancel.CANCELABLE_STATES)


class TestTransferHashValidation(unittest.TestCase):
    """The hash goes straight into a URL path, so its shape is checked first."""

    def test_accepts_realistic_hashes(self):
        for good in ["H1", "abc123", "AB-cd_12", "a" * 128]:
            with self.subTest(hash=good):
                self.assertTrue(cancel.TRANSFER_HASH.match(good))

    def test_rejects_path_and_query_characters(self):
        for bad in ["../../admin", "a/b", "abc?x=1", "abc#frag", "a" * 129, "", "a b", "a%2Fb/c"]:
            with self.subTest(hash=bad):
                self.assertFalse(cancel.TRANSFER_HASH.match(bad))


class ScriptFixture:
    """Stubbed-curl fixture, shared by the end-to-end classes below.

    Deliberately NOT a TestCase: subclassing one to reuse its fixture re-runs every test
    it holds, once per subclass. That is a slower suite reporting the same coverage twice.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paysera-cancel-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")

    def write_stub(self, body, http="200"):
        stub = os.path.join(self.bin, "curl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'echo "$@" >> {self.log}\n'
                f"printf '%s\\nHTTP:%s' '{body}' '{http}'\n"
            )
        os.chmod(stub, 0o755)

    def run_script(self, *args):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["PAYSERA_PAT"] = "test-token"
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "cancel-payment.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().splitlines()


class TestCommandLine(ScriptFixture, unittest.TestCase):
    """Driven end to end with a stubbed curl, so the DELETE gate is tested for real."""

    def test_dry_run_does_not_delete(self):
        self.write_stub('{"id":"H1","status":"new","amount":{"amount":"5.00","currency":"EUR"}}')
        out = self.run_script("H1")
        self.assertIn("DRY-RUN", out.stdout)
        self.assertFalse(any("DELETE" in c for c in self.calls()), "dry run must not DELETE")

    def test_confirm_deletes(self):
        self.write_stub('{"id":"H1","status":"new","amount":{"amount":"5.00","currency":"EUR"}}')
        out = self.run_script("H1", "--confirm")
        self.assertTrue(any("DELETE" in c for c in self.calls()))
        self.assertIn("CANCELED", out.stdout)

    def test_terminal_transfer_is_skipped_even_with_confirm(self):
        self.write_stub('{"id":"H1","status":"done","amount":{"amount":"5.00","currency":"EUR"}}')
        out = self.run_script("H1", "--confirm")
        self.assertIn("not cancelable", out.stdout)
        self.assertFalse(any("DELETE" in c for c in self.calls()))

    def test_null_amount_does_not_crash(self):
        # The API can return the key present with a null value.
        self.write_stub('{"id":"H1","status":"new","amount":null}')
        out = self.run_script("H1")
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("DRY-RUN", out.stdout)

    def test_unreadable_transfer_is_not_deleted(self):
        self.write_stub('{"error":"nope"}', http="404")
        out = self.run_script("H1", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(any("DELETE" in c for c in self.calls()))

    def test_transport_failure_is_reported_without_a_traceback(self):
        stub = os.path.join(self.bin, "curl")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 7\n")
        os.chmod(stub, 0o755)
        out = self.run_script("H1", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn("Traceback", out.stderr)
        # stderr, as SKILL.md promises for a missing curl or a timeout — and as
        # create-payment.py has always done. This wrote to stdout until 1.8.6, so a
        # caller that separates the streams saw the failure in the report.
        self.assertIn("cannot read", out.stderr)
        self.assertNotIn("cannot read", out.stdout)

    def test_a_malformed_hash_reaches_no_request(self):
        self.write_stub('{"id":"H1","status":"new","amount":{"amount":"5.00","currency":"EUR"}}')
        out = self.run_script("../../admin", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self.calls(), [], "a malformed hash must not be sent anywhere")


class TestEveryFailureGoesToStderr(ScriptFixture, unittest.TestCase):
    """No message that makes this script exit non-zero may land on stdout.

    stdout is the report — "H1: status=… amount=…", DRY-RUN, CANCELED. A pipeline that
    keeps the report and drops stderr must not silently keep a failure line as if it were
    a result, and one that watches stderr for trouble must actually see it there.
    """

    def assert_failure_on_stderr(self, out, marker):
        self.assertNotEqual(out.returncode, 0, "this case must exit non-zero")
        self.assertIn(marker, out.stderr)
        self.assertNotIn(marker, out.stdout)

    def test_an_unreadable_transfer_reports_on_stderr(self):
        self.write_stub('{"error":"nope"}', http="404")
        self.assert_failure_on_stderr(self.run_script("H1", "--confirm"), "cannot read")

    def test_a_malformed_hash_reports_on_stderr(self):
        self.write_stub('{"id":"H1","status":"new","amount":{"amount":"5.00","currency":"EUR"}}')
        out = self.run_script("../../admin", "--confirm")
        self.assert_failure_on_stderr(out, "not a valid transferHash")

    def test_a_failed_delete_reports_on_stderr(self):
        # Readable (GET 200) but the DELETE is refused: the stub answers 500 for both, so
        # drive it through a transfer the GET can read by answering on the method.
        stub = os.path.join(self.bin, "curl")
        with open(stub, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'echo "$@" >> {self.log}\n'
                'case "$*" in\n'
                '  *DELETE*) echo \'{"error":"refused"}\'; echo "HTTP:500";;\n'
                '  *) echo \'{"id":"H1","status":"new","amount":{"amount":"5.00",'
                '"currency":"EUR"}}\'; echo "HTTP:200";;\n'
                "esac\n"
            )
        os.chmod(stub, 0o755)
        out = self.run_script("H1", "--confirm")
        self.assert_failure_on_stderr(out, "FAILED")
        self.assertIn("status=new", out.stdout, "the report itself still goes to stdout")

    def test_multiple_hashes_are_all_processed(self):
        self.write_stub('{"id":"H1","status":"new","amount":{"amount":"5.00","currency":"EUR"}}')
        self.run_script("H1", "H2", "H3")
        self.assertGreaterEqual(len(self.calls()), 3)


if __name__ == "__main__":
    unittest.main()
