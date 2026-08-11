"""Tests for create-payment.py.

Weighted towards the things that can cost money or hide a payment: the ledger state
machine that prevents double payments, the Vilnius day boundary that decides whether a
transfer is signable on a phone, beneficiary selection, and the duplicate-match rules.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Work regardless of the runner's rootdir/sys.path handling (pytest from the repo root,
# unittest from this directory, or a plain `python3 test_create_payment.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _testsupport import SCRIPTS, capture_curl, frozen_clock, load, mode_of, temp_ledger

cp = load("create-payment.py", "create_payment")
V = cp._VILNIUS


def vilnius(y, m, d, hh=0, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=V)


class TestVilniusTimezone(unittest.TestCase):
    """Vilnius is EET (+2) in winter, EEST (+3) in summer. Getting this wrong shifts the
    day boundary and silently makes transfers invisible in the mobile app."""

    def test_dst_boundaries_2026(self):
        # Last Sunday of March 2026 is the 29th; last Sunday of October is the 25th.
        for date, expected_hours in [
            ((2026, 1, 15), 2),
            ((2026, 3, 28), 2),
            ((2026, 3, 30), 3),
            ((2026, 8, 10), 3),
            ((2026, 10, 24), 3),
            ((2026, 10, 26), 2),
        ]:
            with self.subTest(date=date):
                offset = vilnius(*date, 12).utcoffset()
                self.assertEqual(offset, datetime.timedelta(hours=expected_hours))

    def test_never_falls_back_to_utc(self):
        # The bug this replaced: _VILNIUS = None meant UTC, a 2-3 hour error.
        self.assertIsNotNone(cp._VILNIUS)
        self.assertNotEqual(vilnius(2026, 8, 10, 12).utcoffset(), datetime.timedelta(0))

    def test_tzname_matches_offset(self):
        self.assertEqual(vilnius(2026, 8, 10, 12).tzname(), "EEST")
        self.assertEqual(vilnius(2026, 1, 15, 12).tzname(), "EET")


class TestSameDayWindow(unittest.TestCase):
    """`--today` must keep operation_date on today's Vilnius date, or the transfer is
    web-bank-only and cannot be signed on a phone."""

    def test_deadline_never_rolls_into_tomorrow(self):
        for hh, mm in [(0, 5), (9, 0), (16, 51), (22, 0), (22, 45)]:
            with self.subTest(time=f"{hh:02d}:{mm:02d}"):
                now = vilnius(2026, 8, 10, hh, mm)
                with frozen_clock(cp, now):
                    epoch = cp._end_of_today_epoch()
                self.assertIsNotNone(epoch)
                deadline = datetime.datetime.fromtimestamp(epoch, V)
                self.assertEqual(deadline.date(), now.date())

    def test_returns_none_once_the_window_has_effectively_closed(self):
        # After 22:50 there is under 10 minutes left; callers fall back to ASAP rather
        # than receiving a timestamp that has rolled past midnight.
        for hh, mm in [(22, 55), (23, 30)]:
            with self.subTest(time=f"{hh:02d}:{mm:02d}"):
                with frozen_clock(cp, vilnius(2026, 8, 10, hh, mm)):
                    self.assertIsNone(cp._end_of_today_epoch())

    def test_vilnius_today_is_not_utc_today(self):
        # 00:30 Vilnius in summer is still the previous day in UTC.
        with frozen_clock(cp, vilnius(2026, 8, 11, 0, 30)):
            self.assertEqual(cp._vilnius_today(), datetime.date(2026, 8, 11))


class TestParsePerformAt(unittest.TestCase):
    def test_relative_hours_are_held_inside_today(self):
        # +6h at 20:00 would land at 02:00 tomorrow, hiding the transfer from mobile.
        with frozen_clock(cp, vilnius(2026, 8, 10, 20, 0)):
            epoch = cp.parse_perform_at("+6h")
        deadline = datetime.datetime.fromtimestamp(epoch, V)
        self.assertEqual(deadline.date(), datetime.date(2026, 8, 10))
        self.assertEqual(deadline.hour, 23)

    def test_relative_hours_within_today_are_untouched(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch = cp.parse_perform_at("+3h")
        self.assertEqual(datetime.datetime.fromtimestamp(epoch, V).hour, 12)

    def test_relative_days_may_cross_into_the_future(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch = cp.parse_perform_at("+2d")
        self.assertEqual(
            datetime.datetime.fromtimestamp(epoch, V).date(), datetime.date(2026, 8, 12)
        )

    def test_default_is_thirty_days(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch = cp.parse_perform_at(None)
        self.assertEqual(
            datetime.datetime.fromtimestamp(epoch, V).date(), datetime.date(2026, 9, 9)
        )

    def test_past_date_is_refused(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            with self.assertRaises(SystemExit):
                cp.parse_perform_at("2026-08-01")

    def test_today_late_at_night_is_refused_with_a_usable_message(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 22, 55)):
            with self.assertRaises(SystemExit) as ctx:
                cp.parse_perform_at("2026-08-10")
        self.assertIn("--advance", str(ctx.exception))

    def test_malformed_spec_is_refused(self):
        with self.assertRaises(SystemExit):
            cp.parse_perform_at("next tuesday")


class TestComputeSchedule(unittest.TestCase):
    def _args(self, **over):
        base = dict(perform_at=None, advance=False, today=False, due_date=None, invoice_id=None)
        base.update(over)
        return mock.Mock(**base)

    def test_no_invoice_id_defaults_to_today(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch, mode = cp.compute_schedule(self._args())
        self.assertEqual(mode, "today")
        self.assertEqual(datetime.datetime.fromtimestamp(epoch, V).date(), datetime.date(2026, 8, 10))

    def test_invoice_id_defaults_to_a_long_window(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch, mode = cp.compute_schedule(self._args(invoice_id="INV-1"))
        self.assertEqual(mode, "scheduled")
        self.assertGreater(epoch, vilnius(2026, 9, 1).timestamp())

    def test_today_falls_back_to_asap_when_no_window_remains(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 23, 30)):
            epoch, mode = cp.compute_schedule(self._args(today=True))
        # ASAP omits perform_at entirely, which keeps operation_date on today.
        self.assertIsNone(epoch)
        self.assertEqual(mode, "asap")

    def test_advance_omits_perform_at(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            self.assertEqual(cp.compute_schedule(self._args(advance=True)), (None, "asap"))

    def test_explicit_perform_at_wins_over_today(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch, mode = cp.compute_schedule(self._args(perform_at="+2d", today=True))
        self.assertEqual(mode, "scheduled")
        self.assertEqual(
            datetime.datetime.fromtimestamp(epoch, V).date(), datetime.date(2026, 8, 12)
        )

    def test_due_date_pays_the_day_before(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            epoch, mode = cp.compute_schedule(self._args(due_date="2026-08-20"))
        self.assertEqual(mode, "scheduled")
        self.assertEqual(
            datetime.datetime.fromtimestamp(epoch, V).date(), datetime.date(2026, 8, 19)
        )

    def test_due_date_already_upon_us_falls_back_to_asap(self):
        with frozen_clock(cp, vilnius(2026, 8, 10, 9, 0)):
            self.assertEqual(cp.compute_schedule(self._args(due_date="2026-08-10"))[1], "asap")


class TestBeneficiarySelection(unittest.TestCase):
    PAYSERA = "LT603500010001234567"
    OTHER = "LT121000011101001000"

    def test_paysera_iban_wins_even_when_passed_as_also_iban(self):
        chosen, others, reason = cp.select_beneficiary_iban(self.OTHER, [self.PAYSERA])
        self.assertEqual(chosen, self.PAYSERA)
        self.assertEqual(others, [self.OTHER])
        self.assertIn("Paysera", reason)

    def test_first_listed_wins_when_no_paysera_iban(self):
        chosen, others, _ = cp.select_beneficiary_iban(self.OTHER, ["LT947300010000000000"])
        self.assertEqual(chosen, self.OTHER)
        self.assertEqual(others, ["LT947300010000000000"])

    def test_duplicates_are_collapsed(self):
        chosen, others, _ = cp.select_beneficiary_iban(self.OTHER, [self.OTHER.lower(), " " + self.OTHER])
        self.assertEqual(chosen, self.OTHER)
        self.assertEqual(others, [])

    def test_empty_iban_is_a_clear_error_not_an_index_error(self):
        with self.assertRaises(SystemExit):
            cp.select_beneficiary_iban("", [])

    def test_is_paysera_iban(self):
        self.assertTrue(cp.is_paysera_iban(self.PAYSERA))
        self.assertTrue(cp.is_paysera_iban("lt60 3500 0100 0123 4567"))
        self.assertFalse(cp.is_paysera_iban(self.OTHER))
        self.assertFalse(cp.is_paysera_iban(""))


class TestInvoiceIdMatching(unittest.TestCase):
    """A false positive here refuses a good payment and pushes the operator towards
    --force, which disables the duplicate check entirely."""

    def test_short_ids_do_not_match(self):
        self.assertFalse(cp._purpose_quotes_invoice("Uz prekes 12 vnt", "12"))
        self.assertFalse(cp._purpose_quotes_invoice("A1 kodas", "A1"))

    def test_exact_token_matches(self):
        self.assertTrue(cp._purpose_quotes_invoice("Pagal saskaita EX000123", "EX000123"))

    def test_trailing_punctuation_still_matches(self):
        self.assertTrue(cp._purpose_quotes_invoice("saskaita EX000123.", "EX000123"))

    def test_longer_surrounding_token_does_not_match(self):
        self.assertFalse(cp._purpose_quotes_invoice("ref EX0001234", "EX000123"))

    def test_case_insensitive(self):
        self.assertTrue(cp._purpose_quotes_invoice("saskaita ex000123", "EX000123"))

    def test_empty_inputs(self):
        self.assertFalse(cp._purpose_quotes_invoice("", "EX000123"))
        self.assertFalse(cp._purpose_quotes_invoice("anything", ""))
        self.assertFalse(cp._purpose_quotes_invoice(None, "EX000123"))


class TestPurposeClipping(unittest.TestCase):
    def test_short_purpose_is_untouched(self):
        self.assertEqual(cp._clip_purpose("Pagal saskaita INV-1"), "Pagal saskaita INV-1")

    def test_long_purpose_is_clipped_and_warns(self):
        text = "word " * 40
        with mock.patch("sys.stderr") as err:
            clipped = cp._clip_purpose(text)
        self.assertLessEqual(len(clipped), cp.PURPOSE_MAX)
        self.assertTrue(err.write.called, "clipping must not be silent")

    def test_clip_respects_word_boundaries(self):
        clipped = cp._clip_purpose("word " * 40)
        self.assertFalse(clipped.endswith("wor"))


class TestLedgerStateMachine(unittest.TestCase):
    """The write-ahead ledger is what stops a retry from creating a second draft after a
    request that was sent but never answered."""

    def test_permissions_are_restrictive(self):
        with temp_ledger(cp) as path:
            cp.append_ledger({"invoice_id": "INV-1"})
            self.assertEqual(mode_of(path), 0o600)
            self.assertEqual(mode_of(os.path.dirname(path)), 0o700)

    def test_update_ledger_merges_into_the_matching_attempt(self):
        with temp_ledger(cp):
            cp.append_ledger({"attempt_id": "a1", "state": "pending", "invoice_id": "INV-1"})
            cp.append_ledger({"attempt_id": "a2", "state": "pending", "invoice_id": "INV-2"})
            self.assertTrue(cp.update_ledger("a1", state="created", transfer_hash="H"))
            rows = {e["attempt_id"]: e for e in cp.load_ledger()}
            self.assertEqual(rows["a1"]["state"], "created")
            self.assertEqual(rows["a1"]["transfer_hash"], "H")
            self.assertEqual(rows["a2"]["state"], "pending")

    def test_update_ledger_reports_a_missing_attempt(self):
        with temp_ledger(cp):
            self.assertFalse(cp.update_ledger("nope", state="created"))

    def test_missing_ledger_reads_as_empty(self):
        with temp_ledger(cp):
            self.assertEqual(cp.load_ledger(), [])

    def _blocking_for(self, rows, invoice_id="INV-1"):
        with temp_ledger(cp):
            for row in rows:
                cp.append_ledger(row)
            with mock.patch.object(cp, "http_json", return_value=("200", {})):
                blocking, _ = cp.find_blocking(invoice_id, token="t")
        return blocking

    def test_unanswered_attempt_blocks_a_retry(self):
        # The double-payment case: no hash, no answer, and GET /transfers cannot see
        # unsigned drafts — so only this row stands between us and a second payment.
        blocking = self._blocking_for(
            [{"attempt_id": "a1", "state": "unknown", "invoice_id": "INV-1", "transfer_hash": None}]
        )
        self.assertEqual(len(blocking), 1)
        self.assertIn("UNCONFIRMED", blocking[0][1])

    def test_pending_attempt_blocks_a_retry(self):
        blocking = self._blocking_for(
            [{"attempt_id": "a1", "state": "pending", "invoice_id": "INV-1", "transfer_hash": None}]
        )
        self.assertEqual(len(blocking), 1)

    def test_definitely_refused_attempt_does_not_block(self):
        # A 4xx means the API refused it; wedging the invoice would be wrong.
        blocking = self._blocking_for(
            [{"attempt_id": "a1", "state": "failed", "invoice_id": "INV-1", "transfer_hash": None}]
        )
        self.assertEqual(blocking, [])

    def test_rows_for_other_invoices_are_ignored(self):
        blocking = self._blocking_for(
            [{"attempt_id": "a1", "state": "unknown", "invoice_id": "OTHER", "transfer_hash": None}]
        )
        self.assertEqual(blocking, [])

    def test_live_transfer_still_blocks(self):
        with temp_ledger(cp):
            cp.append_ledger({"invoice_id": "INV-1", "transfer_hash": "H1", "state": "created"})
            with mock.patch.object(cp, "http_json", return_value=("200", {"id": "H1", "status": "signed"})):
                blocking, _ = cp.find_blocking("INV-1", token="t")
        self.assertEqual(len(blocking), 1)

    def test_terminal_transfer_does_not_block(self):
        with temp_ledger(cp):
            cp.append_ledger({"invoice_id": "INV-1", "transfer_hash": "H1", "state": "created"})
            with mock.patch.object(cp, "http_json", return_value=("200", {"id": "H1", "status": "canceled"})):
                blocking, _ = cp.find_blocking("INV-1", token="t")
        self.assertEqual(blocking, [])

    def test_unreadable_transfer_is_treated_as_blocking(self):
        with temp_ledger(cp):
            cp.append_ledger({"invoice_id": "INV-1", "transfer_hash": "H1", "state": "created"})
            with mock.patch.object(cp, "http_json", return_value=("ERR", {})):
                blocking, _ = cp.find_blocking("INV-1", token="t")
        self.assertEqual(len(blocking), 1)

    def test_attempt_ids_are_unique(self):
        self.assertNotEqual(cp._new_attempt_id(), cp._new_attempt_id())


class TestTokenHandling(unittest.TestCase):
    def test_token_never_appears_in_argv(self):
        secret = "SUPERSECRET-TOKEN"
        with capture_curl(cp) as calls:
            cp._curl("GET", "https://api.paysera.com/x", secret)
        argv = " ".join(calls[0]["argv"])
        self.assertNotIn(secret, argv, "token must not be readable via ps/proc")
        self.assertIn(secret, calls[0]["input"])
        self.assertIn("-K", calls[0]["argv"])

    def test_post_body_does_not_go_through_argv_either(self):
        with capture_curl(cp) as calls:
            cp._curl("POST", "https://api.paysera.com/x", "tok", payload={"amount": "5.00"})
        self.assertIn("--data-binary", calls[0]["argv"])

    def test_requests_carry_a_timeout(self):
        with capture_curl(cp) as calls:
            cp._curl("GET", "https://api.paysera.com/x", "tok")
        self.assertEqual(calls[0]["kwargs"].get("timeout"), cp.HTTP_TIMEOUT)

    def test_tokens_that_could_break_config_quoting_are_refused(self):
        for bad in ['has"quote', "has\\backslash", "has\nnewline", ""]:
            with self.subTest(token=bad):
                with self.assertRaises(SystemExit):
                    cp._validate_token(bad)

    def test_environment_token_takes_precedence(self):
        with mock.patch.dict(os.environ, {"PAYSERA_PAT": "from-env"}):
            self.assertEqual(cp.read_token("/nonexistent"), "from-env")

    def test_missing_token_file_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                cp.read_token("/nonexistent/token")


class TestTransportFailures(unittest.TestCase):
    def test_missing_curl_is_reported_not_crashed(self):
        with mock.patch.object(cp.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(cp.HttpError):
                cp._curl("GET", "https://api.paysera.com/x", "tok")

    def test_timeout_is_reported(self):
        exc = subprocess.TimeoutExpired(cmd="curl", timeout=30)
        with mock.patch.object(cp.subprocess, "run", side_effect=exc):
            with self.assertRaises(cp.HttpError):
                cp._curl("GET", "https://api.paysera.com/x", "tok")

    def test_nonzero_exit_is_reported(self):
        with capture_curl(cp, returncode=7):
            with self.assertRaises(cp.HttpError):
                cp._curl("GET", "https://api.paysera.com/x", "tok")

    def test_http_json_surfaces_failures_as_ERR_rather_than_empty(self):
        # An empty result would be indistinguishable from "no transfers found", which
        # would silently weaken the duplicate check.
        with mock.patch.object(cp.subprocess, "run", side_effect=FileNotFoundError):
            code, _ = cp.http_json("GET", "https://api.paysera.com/x", "tok")
        self.assertEqual(code, "ERR")


class TestNullFields(unittest.TestCase):
    """The API returns keys present-but-null; a .get() default does not cover that."""

    def test_null_purpose_does_not_crash_the_duplicate_scan(self):
        row = {
            "id": "H1",
            "status": "done",
            "purpose": {"details": None},
            "amount": {"amount": "5.00"},
            "beneficiary": {"iban": "LT121000011101001000"},
        }
        with temp_ledger(cp):
            with mock.patch.object(cp, "list_transfers", return_value=[row]):
                _, seen = cp.find_blocking(
                    "INV-1", token="t", payer="EVP1", ibans=["LT121000011101001000"], amount="5.00"
                )
        self.assertEqual(seen[0][4], "")


class TestMiscHelpers(unittest.TestCase):
    def test_beneficiary_country_prefers_bic(self):
        self.assertEqual(cp.beneficiary_country("LT121000011101001000", "DEUTDEFF"), "DE")

    def test_beneficiary_country_falls_back_to_iban(self):
        self.assertEqual(cp.beneficiary_country("LT121000011101001000", None), "LT")

    def test_beneficiary_country_handles_junk(self):
        self.assertIsNone(cp.beneficiary_country("", None))

    def test_buyer_name_normalisation_is_diacritic_safe(self):
        self.assertEqual(cp._norm_buyer_name("Įmonė, UAB"), cp._norm_buyer_name("ĮMONĖ UAB"))

    def test_noon_epoch_is_inside_the_day(self):
        epoch = cp._noon_epoch(datetime.date(2026, 8, 12))
        self.assertEqual(
            datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).date(),
            datetime.date(2026, 8, 12),
        )


class TestCommandLineValidation(unittest.TestCase):
    """End-to-end argument checks: these live in main(), so they are exercised by running
    the script with a stubbed curl on PATH."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paysera-cli-test-")
        cls.bin = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.bin)
        stub = os.path.join(cls.bin, "curl")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\nprintf \'{"items":[]}\\nHTTP:200\'\n')
        os.chmod(stub, 0o755)

    def run_script(self, *extra):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env["PAYSERA_PAT"] = "test-token"
        base = [
            sys.executable,
            str(SCRIPTS / "create-payment.py"),
            "--payer", "EVP0000000000001",
            "--beneficiary-name", "Acme UAB",
            "--iban", "LT121000011101001000",
            "--purpose", "test",
        ]
        return subprocess.run(base + list(extra), capture_output=True, text=True, env=env, timeout=60)

    def test_rejects_non_finite_amount(self):
        for bad in ["Infinity", "NaN"]:
            with self.subTest(amount=bad):
                out = self.run_script("--amount", bad)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("finite", out.stdout + out.stderr)

    def test_rejects_absurd_amount(self):
        out = self.run_script("--amount", "1e999")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("implausibly large", out.stdout + out.stderr)

    def test_rejects_sub_cent_precision(self):
        out = self.run_script("--amount", "12.345")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("decimal places", out.stdout + out.stderr)

    def test_rejects_negative_amount(self):
        out = self.run_script("--amount", "-5")
        self.assertNotEqual(out.returncode, 0)

    def test_accepts_a_normal_amount_and_stays_dry_run(self):
        out = self.run_script("--amount", "12.34")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("DRY-RUN", out.stdout)

    def test_currency_is_normalised_in_the_payload(self):
        out = self.run_script("--amount", "12.34", "--currency", "eur")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout.split("Payload:", 1)[1].split("\n\n", 1)[0])
        self.assertEqual(payload["amount"]["currency"], "EUR")

    def test_bad_currency_is_refused(self):
        out = self.run_script("--amount", "12.34", "--currency", "euro")
        self.assertNotEqual(out.returncode, 0)

    def test_charge_type_is_restricted_to_api_values(self):
        out = self.run_script("--amount", "12.34", "--charge-type", "ben")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("invalid choice", out.stderr)

    def test_force_announces_that_the_duplicate_check_was_skipped(self):
        out = self.run_script("--amount", "12.34", "--invoice-id", "INV-X", "--force")
        self.assertIn("--force", out.stderr)
        self.assertIn("SKIPPED", out.stderr)

    def test_dry_run_writes_no_ledger_entry(self):
        ledger = os.path.join(self.tmp, ".config", "paysera-payments", "ledger.json")
        before = os.path.exists(ledger)
        self.run_script("--amount", "12.34", "--invoice-id", "INV-DRY")
        if not before:
            self.assertFalse(os.path.exists(ledger), "a dry run must not record an attempt")


if __name__ == "__main__":
    unittest.main()
