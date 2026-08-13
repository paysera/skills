"""Tests for create-payment.py.

Weighted towards the things that can cost money or hide a payment: the ledger state
machine that prevents double payments, the Vilnius day boundary that decides whether a
transfer is signable on a phone, beneficiary selection, and the duplicate-match rules.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Work regardless of the runner's rootdir/sys.path handling (pytest from the repo root,
# unittest from this directory, or a plain `python3 test_create_payment.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _testsupport  # noqa: E402  (for the leak check's own test, below)
from _testsupport import (  # noqa: E402
    SCRIPTS,
    assert_tempdir_is_empty,
    capture_curl,
    frozen_clock,
    isolate_tempdir,
    load,
    mode_of,
    temp_ledger,
)

cp = load("create-payment.py", "create_payment")
V = cp._VILNIUS

# This module gets its own temporary directory, and must leave it empty. Nothing here
# reaches outside the plugin: `claude plugin install` copies the plugin directory only, so
# a check that needs a repository file fails from an installed copy for no real defect.
setUpModule = isolate_tempdir
tearDownModule = assert_tempdir_is_empty


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

    def test_tzname_matches_offset(self):
        self.assertEqual(vilnius(2026, 8, 10, 12).tzname(), "EEST")
        self.assertEqual(vilnius(2026, 1, 15, 12).tzname(), "EET")


class TestVilniusFallback(unittest.TestCase):
    """The project's OWN timezone rule, tested directly.

    `_VILNIUS` is `ZoneInfo` wherever tzdata exists, so on a modern CI image the tests
    above exercise Python's tz database rather than this code. These build the fallback
    explicitly, so the rule is covered on every interpreter.
    """

    def setUp(self):
        self.tz = cp._VilniusFallback()

    def test_offsets_across_transitions(self):
        for date, expected_hours in [
            ((2026, 1, 15), 2),
            ((2026, 3, 28), 2),
            ((2026, 3, 30), 3),
            ((2026, 8, 10), 3),
            ((2026, 10, 24), 3),
            ((2026, 10, 26), 2),
        ]:
            with self.subTest(date=date):
                moment = datetime.datetime(*date, 12, tzinfo=self.tz)
                self.assertEqual(moment.utcoffset(), datetime.timedelta(hours=expected_hours))

    def test_last_sunday_matches_the_eu_rule(self):
        # Independently checkable: the EU switches on the last Sunday of March/October.
        for year, march, october in [
            (2024, 31, 27),
            (2025, 30, 26),
            (2026, 29, 25),
            (2027, 28, 31),
        ]:
            with self.subTest(year=year):
                self.assertEqual(cp._VilniusFallback._last_sunday(year, 3).day, march)
                self.assertEqual(cp._VilniusFallback._last_sunday(year, 10).day, october)
                self.assertEqual(cp._VilniusFallback._last_sunday(year, 3).weekday(), 6)

    def test_never_behaves_like_utc(self):
        # The bug this replaced: no tzdata meant plain UTC, a 2-3 hour error that moved
        # the day boundary and hid transfers from the mobile app.
        for month in (1, 8):
            with self.subTest(month=month):
                moment = datetime.datetime(2026, month, 15, 12, tzinfo=self.tz)
                self.assertNotEqual(moment.utcoffset(), datetime.timedelta(0))

    def test_agrees_with_zoneinfo_when_zoneinfo_is_available(self):
        try:
            from zoneinfo import ZoneInfo

            real = ZoneInfo("Europe/Vilnius")
        except Exception:  # pragma: no cover - depends on the host having tzdata
            self.skipTest("no tzdata on this interpreter")
        for month in range(1, 13):
            for day in (1, 15, 28):
                with self.subTest(month=month, day=day):
                    naive = datetime.datetime(2026, month, day, 12)
                    self.assertEqual(
                        naive.replace(tzinfo=self.tz).utcoffset(),
                        naive.replace(tzinfo=real).utcoffset(),
                    )

    def test_the_module_reports_which_implementation_is_in_use(self):
        self.assertIsInstance(cp._VILNIUS_IS_FALLBACK, bool)
        if cp._VILNIUS_IS_FALLBACK:
            self.assertIsInstance(cp._VILNIUS, cp._VilniusFallback)

    def test_utc_conversion_is_exact_across_the_october_transition(self):
        # Inheriting tzinfo.fromutc() made the hour 01:00-02:00 UTC on the last Sunday of
        # October convert an hour late: the base implementation adds the STANDARD offset
        # and asks dst() about the result, so one boundary constant had to serve both
        # that convention and the wall-clock one utcoffset() gets. Deleting the explicit
        # fromutc() override fails this.
        expected = {
            (0, 59): "2026-10-25 03:59:00+03:00",
            (1, 0): "2026-10-25 03:00:00+02:00",  # the switch
            (1, 30): "2026-10-25 03:30:00+02:00",
            (2, 0): "2026-10-25 04:00:00+02:00",
        }
        for (hh, mm), want in expected.items():
            with self.subTest(utc=f"{hh:02d}:{mm:02d}"):
                moment = datetime.datetime(2026, 10, 25, hh, mm, tzinfo=datetime.timezone.utc)
                self.assertEqual(str(moment.astimezone(self.tz)), want)

    def test_a_converted_time_still_names_the_instant_it_came_from(self):
        # The invariant the old boundary broke: wall time minus the offset the zone
        # reports for it must be the UTC reading again. 01:00 UTC used to convert to
        # 04:00 while reporting +02:00 — i.e. it claimed to be 02:00 UTC, an hour that
        # had not happened. (Wall time itself is NOT monotonic here: at a fall-back the
        # clock legitimately repeats an hour, which is why this checks the round trip.)
        for date in ((2026, 3, 29), (2026, 10, 25)):
            for minute in range(0, 5 * 60, 5):
                naive_utc = datetime.datetime(*date, minute // 60, minute % 60)
                moment = naive_utc.replace(tzinfo=datetime.timezone.utc)
                with self.subTest(utc=moment):
                    local = moment.astimezone(self.tz)
                    self.assertEqual(
                        local.replace(tzinfo=None) - local.utcoffset(), naive_utc
                    )

    def test_wall_time_advances_across_the_spring_transition(self):
        # No ambiguity in March — the clock jumps forward, never back.
        previous = None
        for minute in range(0, 5 * 60, 5):
            moment = datetime.datetime(
                2026, 3, 29, minute // 60, minute % 60, tzinfo=datetime.timezone.utc
            )
            local = moment.astimezone(self.tz).replace(tzinfo=None)
            if previous is not None:
                self.assertGreater(local, previous, f"went backwards at {moment}")
            previous = local

    def test_october_transition_agrees_with_zoneinfo_minute_by_minute(self):
        # The existing agreement test samples days 1/15/28 at 12:00, so it cannot see a
        # boundary defect. This walks the transition itself.
        try:
            from zoneinfo import ZoneInfo

            real = ZoneInfo("Europe/Vilnius")
        except Exception:  # pragma: no cover - depends on the host having tzdata
            self.skipTest("no tzdata on this interpreter")
        for date in ((2026, 3, 29), (2026, 10, 25)):
            for minute in range(0, 5 * 60, 5):
                moment = datetime.datetime(
                    *date, minute // 60, minute % 60, tzinfo=datetime.timezone.utc
                )
                with self.subTest(utc=moment):
                    self.assertEqual(
                        moment.astimezone(self.tz).replace(tzinfo=None),
                        moment.astimezone(real).replace(tzinfo=None),
                    )


class TestSepaZone(unittest.TestCase):
    def test_recent_sepa_members_are_recognised(self):
        # Joined the SEPA schemes 2023-2024. Missing, they were classified as
        # international wires needing a BIC, an address and a city.
        for country in ("AL", "MD", "MK", "ME"):
            with self.subTest(country=country):
                self.assertIn(country, cp.SEPA_COUNTRIES)

    def test_non_sepa_countries_are_still_outside(self):
        for country in ("UA", "AM", "GE", "US", "TR"):
            with self.subTest(country=country):
                self.assertNotIn(country, cp.SEPA_COUNTRIES)


class TestTokenFilePermissions(unittest.TestCase):
    """A PAT in a group- or world-readable file is exposed to every local user, for good.

    The docs promised 0600 but nothing created or checked it: `curl ... > token` under the
    usual umask 022 leaves 0644.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paysera-token-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "token")
        with open(self.path, "w") as f:
            f.write("a-token\n")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("PAYSERA_PAT", None)
        self.addCleanup(self.env.stop)

    def test_a_private_token_file_is_accepted(self):
        os.chmod(self.path, 0o600)
        self.assertEqual(cp.read_token(self.path), "a-token")

    def test_a_readable_token_file_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path, mode)
                with self.assertRaises(SystemExit) as raised:
                    cp.read_token(self.path)
                message = str(raised.exception)
                self.assertIn("readable by other users", message)
                self.assertIn("chmod 600", message)

    def test_the_env_var_path_does_not_need_a_file(self):
        os.environ["PAYSERA_PAT"] = "from-env"
        os.chmod(self.path, 0o644)
        self.assertEqual(cp.read_token(self.path), "from-env")


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
        # argparse.Namespace, not mock.Mock: a Mock invents truthy attributes on demand,
        # so a newly added option would silently read as set and every test here would
        # follow the new branch while still passing.
        base = dict(perform_at=None, advance=False, today=False, due_date=None, invoice_id=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_namespace_rejects_unknown_options(self):
        # Guards the guard: proves _args() has no Mock-style auto-attributes.
        with self.assertRaises(AttributeError):
            self._args().some_option_that_does_not_exist

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
        with mock.patch("sys.stderr"):  # the warning is expected; keep it out of the output
            clipped = cp._clip_purpose("word " * 40)
        self.assertFalse(clipped.endswith("wor"))

    def test_long_address_is_clipped_and_warns(self):
        address = "Very Long Street Name 123, Apartment 45, Some District, Big City, 01234 Country"
        with mock.patch("sys.stderr") as err:
            clipped = cp._clip_address(address)
        self.assertLessEqual(len(clipped), cp.ADDRESS_MAX)
        self.assertTrue(err.write.called, "address clipping must not be silent")

    def test_short_address_is_untouched(self):
        self.assertEqual(cp._clip_address("Gedimino pr. 1, Vilnius"), "Gedimino pr. 1, Vilnius")


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


class TestResolvePayer(unittest.TestCase):
    """resolve_payer() decides whose money moves. Every refusal path is a guard against
    paying an invoice from the wrong company's account."""

    ACCOUNTS = {"EVP0000000000001": "Company A", "EVP0000000000002": "Company B"}

    def setUp(self):
        self.patches = [
            mock.patch.object(cp, "ALLOWED_ACCOUNTS", dict(self.ACCOUNTS)),
            mock.patch.object(cp, "BUYER_CODE_TO_ACCOUNT", {"111": "EVP0000000000001"}),
            mock.patch.object(cp, "BUYER_NAME_TO_ACCOUNT", {"COMPANY A UAB": "EVP0000000000001"}),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _args(self, **over):
        base = dict(buyer_code=None, buyer_name=None, payer=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_known_code_resolves_the_payer(self):
        self.assertEqual(cp.resolve_payer(self._args(buyer_code="111")), "EVP0000000000001")

    def test_known_name_resolves_when_the_code_is_absent(self):
        with mock.patch("sys.stderr"):
            payer = cp.resolve_payer(self._args(buyer_name="Company A, UAB"))
        self.assertEqual(payer, "EVP0000000000001")

    def test_name_resolution_is_announced(self):
        # The name path is the safety-sensitive one; it must leave an audit trail.
        with mock.patch("sys.stderr") as err:
            cp.resolve_payer(self._args(buyer_name="Company A, UAB"))
        self.assertTrue(err.write.called)

    def test_code_and_name_disagreement_is_refused(self):
        with mock.patch.object(cp, "BUYER_NAME_TO_ACCOUNT", {"COMPANY B UAB": "EVP0000000000002"}):
            with self.assertRaises(SystemExit) as ctx:
                cp.resolve_payer(self._args(buyer_code="111", buyer_name="Company B, UAB"))
        self.assertIn("mismatch", str(ctx.exception))

    def test_code_and_explicit_payer_disagreement_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            cp.resolve_payer(self._args(buyer_code="111", payer="EVP0000000000002"))
        self.assertIn("wrong account", str(ctx.exception))

    def test_unresolvable_buyer_is_refused_rather_than_guessed(self):
        with self.assertRaises(SystemExit) as ctx:
            cp.resolve_payer(self._args(buyer_code="999"))
        self.assertIn("could not resolve", str(ctx.exception))

    def test_payer_outside_the_token_scope_is_refused(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                cp.resolve_payer(self._args(payer="EVP9999999999999"))

    def test_unmapped_code_with_explicit_payer_is_allowed_but_announced(self):
        with mock.patch("sys.stderr") as err:
            payer = cp.resolve_payer(self._args(buyer_code="999", payer="EVP0000000000002"))
        self.assertEqual(payer, "EVP0000000000002")
        self.assertTrue(err.write.called, "an unverified guard must not be silent")

    def test_no_buyer_information_at_all_is_refused(self):
        with self.assertRaises(SystemExit):
            cp.resolve_payer(self._args())


class TestListTransfers(unittest.TestCase):
    """Two shipped defects lived here — the wrong direction parameter and broken
    pagination — and both made the duplicate check silently incomplete."""

    def _paged_api(self, total, with_metadata_total=False):
        """A stub endpoint holding `total` transfers, answered 100 to a page."""
        def fake(method, url, token, payload=None):
            offset = int(url.split("offset=")[1])
            rows = [{"id": f"T{i}"} for i in range(offset, min(offset + 100, total))]
            body = {"items": rows}
            if with_metadata_total:
                body["_metadata"] = {"total": total}
            return "200", body
        return fake

    def test_hitting_the_page_cap_is_reported_not_silent(self):
        # Every other incomplete path warns; running out of pages returned a partial list
        # that the caller could not tell from a complete one. It then prints its usual
        # "scanned payments … since <date>" line over a truncated result.
        with mock.patch.object(cp, "http_json", self._paged_api(12000)):
            with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                got = cp.list_transfers("t", "EVP1", 0)
        self.assertEqual(len(got), 5000, "the cap itself is unchanged")
        message = err.getvalue()
        self.assertIn("INCOMPLETE", message)
        self.assertIn("5000", message, "the operator needs to know how much was read")
        self.assertIn("--invoice-date", message, "and what to do about it")

    def test_a_complete_scan_says_nothing(self):
        # The warning must not fire on an ordinary run, or it becomes noise and stops
        # being read. Exercised for each of the three honest ways the walk can end.
        for label, total, meta in [
            ("short final page", 250, False),
            ("exact page boundary", 200, False),
            ("total reported in metadata", 500, True),
        ]:
            with self.subTest(ending=label):
                with mock.patch.object(cp, "http_json", self._paged_api(total, meta)):
                    with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                        got = cp.list_transfers("t", "EVP1", 0)
                self.assertEqual(len(got), total)
                self.assertEqual(err.getvalue(), "", f"{label} warned when complete")

    def test_a_scan_that_exactly_fills_the_cap_is_not_a_false_alarm(self):
        # 5000 rows in 50 full pages: page 51 comes back empty, which is an honest end.
        with mock.patch.object(cp, "http_json", self._paged_api(5000)):
            with mock.patch.object(sys, "stderr", io.StringIO()) as err:
                got = cp.list_transfers("t", "EVP1", 0, max_pages=51)
        self.assertEqual(len(got), 5000)
        self.assertEqual(err.getvalue(), "")

    def test_queries_the_payer_side_not_the_beneficiary_side(self):
        # credit_account_number = the account is the PAYER (outgoing). Querying
        # debit_ instead returns INCOMING transfers and never sees a duplicate.
        seen = []

        def fake(method, url, token):
            seen.append(url)
            return "200", {"items": []}

        with mock.patch.object(cp, "http_json", fake):
            cp.list_transfers("tok", "EVP1", 0)
        self.assertIn("credit_account_number=EVP1", seen[0])
        self.assertNotIn("debit_account_number", seen[0])

    def test_uses_offset_pagination_and_walks_every_page(self):
        pages = {0: [{"id": f"a{i}"} for i in range(100)], 100: [{"id": "b1"}]}

        def fake(method, url, token):
            offset = int(url.split("offset=")[1])
            return "200", {"items": pages.get(offset, [])}

        with mock.patch.object(cp, "http_json", fake):
            items = cp.list_transfers("tok", "EVP1", 0)
        self.assertEqual(len(items), 101)

    def test_duplicate_ids_across_pages_are_collapsed(self):
        page = [{"id": f"a{i}"} for i in range(100)]

        def fake(method, url, token):
            offset = int(url.split("offset=")[1])
            # Page 2 repeats page 1 — the shape the broken cursor pagination produced.
            return "200", {"items": page if offset < 200 else []}

        with mock.patch.object(cp, "http_json", fake):
            items = cp.list_transfers("tok", "EVP1", 0)
        self.assertEqual(len({i["id"] for i in items}), 100)

    def test_a_failed_page_warns_rather_than_looking_empty(self):
        with mock.patch.object(cp, "http_json", return_value=("ERR", {})):
            with mock.patch("sys.stderr") as err:
                items = cp.list_transfers("tok", "EVP1", 0)
        self.assertEqual(items, [])
        self.assertTrue(err.write.called, "an incomplete dup-check must announce itself")

    def test_transfer_items_accepts_the_shapes_the_api_returns(self):
        self.assertEqual(cp._transfer_items([{"id": "a"}]), [{"id": "a"}])
        self.assertEqual(cp._transfer_items({"items": [{"id": "a"}]}), [{"id": "a"}])
        self.assertEqual(cp._transfer_items({"transfers": [{"id": "a"}]}), [{"id": "a"}])
        self.assertEqual(cp._transfer_items({"unexpected": 1}), [])
        self.assertEqual(cp._transfer_items(None), [])


class TestCurrencyAwareDuplicateMatch(unittest.TestCase):
    def _find(self, transfer_currency, our_currency):
        row = {
            "id": "H1",
            "status": "done",
            "purpose": {"details": "unrelated"},
            "amount": {"amount": "100.00", "currency": transfer_currency},
            "beneficiary": {"iban": "LT121000011101001000"},
        }
        with temp_ledger(cp):
            with mock.patch.object(cp, "list_transfers", return_value=[row]):
                blocking, _ = cp.find_blocking(
                    "INV-1",
                    token="t",
                    payer="EVP1",
                    ibans=["LT121000011101001000"],
                    amount="100.00",
                    currency=our_currency,
                )
        return blocking

    def test_same_amount_same_currency_blocks(self):
        self.assertEqual(len(self._find("EUR", "EUR")), 1)

    def test_same_amount_different_currency_does_not_block(self):
        self.assertEqual(self._find("USD", "EUR"), [])

    def test_unknown_currency_is_treated_as_possible_duplicate(self):
        # Fail-safe: if we cannot tell, assume it might be the same payment.
        self.assertEqual(len(self._find(None, "EUR")), 1)


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


class TestTheSuiteCleansUpAfterItself(unittest.TestCase):
    """Every temporary directory here holds a ledger with test IBANs and amounts, and CI
    runs the whole suite twice. One directory per test method, left behind, adds up."""

    def test_temp_ledger_removes_its_directory(self):
        with temp_ledger(cp) as path:
            cp._ensure_config_dir()
            cp.append_ledger({"invoice_id": "INV-1"})
            self.assertTrue(os.path.exists(path))
            directory = os.path.dirname(os.path.dirname(path))
        self.assertFalse(os.path.exists(directory), "temp_ledger left its directory behind")

    def test_temp_ledger_cleans_up_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with temp_ledger(cp) as path:
                directory = os.path.dirname(os.path.dirname(path))
                raise ValueError("boom")
        self.assertFalse(os.path.exists(directory))

    def test_the_module_temporary_directory_is_isolated_and_checked(self):
        """The leak check itself is `tearDownModule`; this pins that it is armed.

        Two earlier versions of this check were worse. The first counted
        `tempfile.mkdtemp(` occurrences against `shutil.rmtree` occurrences per module: it
        read its own module, so both strings counted themselves and the balance was
        arithmetic coincidence, and equal counts never proved pairing anyway. The second
        re-ran every test module in a subprocess with its own TMPDIR — which did test the
        real property, but ran the whole suite a second time inside itself (four times per
        CI job, this module carrying 63 subprocess call sites), and reached out to
        `scripts/test_validate.py`, a repository file that `claude plugin install` does not
        copy. From an installed plugin that path does not exist and the check failed for a
        defect that was not there.

        Giving each module its own temporary directory and requiring it to be empty
        afterwards tests the same property, costs nothing, needs no path outside the
        plugin, and covers a fixture added later without anyone remembering to.
        """
        box = _testsupport._TEMPBOX.get("path")
        self.assertIsNotNone(box, "setUpModule did not run")
        self.assertIn("paysera-tempbox-", box)
        # Asserted against tempfile.tempdir, NOT gettempdir(): gettempdir() falls back to
        # $TMPDIR, so it reports the box either way and cannot see this half go missing.
        self.assertEqual(tempfile.tempdir, box, "this process still writes outside the box")
        # And the environment, because the end-to-end tests spawn subprocesses, which read
        # that and not tempfile.tempdir. Both halves, or the check has a hole.
        for key in ("TMPDIR", "TEMP", "TMP"):
            self.assertEqual(os.environ.get(key), box, key)
        made = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, made, ignore_errors=True)
        self.assertTrue(made.startswith(box))

    def test_a_full_cycle_leaves_the_process_as_it_found_it(self):
        # Under pytest all three test modules share one process, so a teardown that does
        # not put TMPDIR back hands the next module a path that no longer exists.
        keys = ("TMPDIR", "TEMP", "TMP")
        before = {k: os.environ.get(k) for k in keys}
        saved, prior_tempdir = dict(_testsupport._TEMPBOX), tempfile.tempdir
        try:
            isolate_tempdir()
            self.assertNotEqual(os.environ["TMPDIR"], before["TMPDIR"])
            assert_tempdir_is_empty()
            self.assertEqual({k: os.environ.get(k) for k in keys}, before)
            self.assertEqual(tempfile.tempdir, prior_tempdir)
        finally:
            _testsupport._TEMPBOX.clear()
            _testsupport._TEMPBOX.update(saved)
            tempfile.tempdir = prior_tempdir
            for key, value in before.items():
                os.environ.pop(key, None) if value is None else os.environ.update({key: value})

    def test_a_disarmed_check_is_not_a_pass(self):
        # The teardown used to return quietly when setUpModule had not run. That is the
        # failure mode this whole check exists to prevent, one level up: a check that
        # never fires and a run that leaked nothing produce the same green.
        saved = dict(_testsupport._TEMPBOX)
        _testsupport._TEMPBOX.clear()
        try:
            with self.assertRaises(AssertionError) as raised:
                _testsupport.assert_tempdir_is_empty()
        finally:
            _testsupport._TEMPBOX.update(saved)
        self.assertIn("disarmed", str(raised.exception))

    def test_the_leak_check_fails_when_something_is_left_behind(self):
        # The check runs at module teardown, where a test cannot observe it. Call it
        # directly against a planted leak instead, so it is not merely assumed to work.
        box = tempfile.mkdtemp(prefix="paysera-leakprobe-")
        self.addCleanup(shutil.rmtree, box, ignore_errors=True)
        os.mkdir(os.path.join(box, "left-behind"))
        saved = dict(_testsupport._TEMPBOX)
        # Captured, not assumed to be the box: restoring it to the box would REPAIR a
        # broken isolate_tempdir for whatever test runs next, and that is not this test's
        # business. (It did, until a mutation of isolate_tempdir survived because of it.)
        prior_tempdir = tempfile.tempdir
        _testsupport._TEMPBOX.update(path=box, tempdir=prior_tempdir, env={})
        try:
            with self.assertRaises(AssertionError) as raised:
                _testsupport.assert_tempdir_is_empty()
        finally:
            _testsupport._TEMPBOX.clear()
            _testsupport._TEMPBOX.update(saved)
            tempfile.tempdir = prior_tempdir
        self.assertIn("left-behind", str(raised.exception))


class TestBeneficiaryAccountKeys(unittest.TestCase):
    """The account sits under one of three keys, and the scan must read all three.

    The one it used to miss is the one THIS TOOL writes for a non-IBAN account:
    beneficiary.bank_account.bank_account_number. For every Armenian/Georgian-style
    national account number the candidate set could not match, so the scan found nothing
    and the run printed its all-clear — a partial check reporting as complete.
    """

    ACCOUNT = "20507231000012345"

    def _find(self, beneficiary):
        row = {
            "id": "H1",
            "status": "done",
            "purpose": {"details": "Invoice INV-77"},
            "amount": {"amount": "250.00", "currency": "EUR"},
            "beneficiary": beneficiary,
        }
        with temp_ledger(cp):
            with mock.patch.object(cp, "list_transfers", return_value=[row]):
                return cp.find_blocking(
                    "INV-77",
                    token="t",
                    payer="EVP1",
                    ibans=[self.ACCOUNT],
                    amount="250.00",
                    currency="EUR",
                )

    def test_a_national_account_number_blocks_a_second_payment(self):
        blocking, seen = self._find({"bank_account": {"bank_account_number": self.ACCOUNT}})
        self.assertEqual(len(blocking), 1, "the prior payment was invisible to the scan")
        self.assertEqual(len(seen), 1, "and absent from the human-review list too")

    def test_an_iban_under_bank_account_still_blocks(self):
        blocking, seen = self._find({"bank_account": {"iban": self.ACCOUNT}})
        self.assertEqual(len(blocking), 1)
        self.assertEqual(len(seen), 1)

    def test_a_paysera_beneficiary_iban_still_blocks(self):
        blocking, seen = self._find({"iban": self.ACCOUNT})
        self.assertEqual(len(blocking), 1)
        self.assertEqual(len(seen), 1)

    def test_a_different_account_under_the_same_key_does_not_block(self):
        # Otherwise the fix above could be "match everything", which blocks every invoice.
        blocking, seen = self._find({"bank_account": {"bank_account_number": "99999999999"}})
        self.assertEqual(blocking, [])
        self.assertEqual(seen, [])

    def test_the_documentation_does_not_call_the_scanned_accounts_IBANs(self):
        # The rename in the code was half of the fix; the documentation is where an
        # operator paying an Armenian or Georgian account decides whether the check
        # covers them. "Scans all of the beneficiary's IBANs" says it does not.
        skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        section = skill[skill.index("The dedup checks") :]
        section = section[: section.index("\n## ")]
        # Collapsed, because the prose is hard-wrapped: "to those\nIBANs" is the same
        # sentence as "to those IBANs" and must not escape the check by line break. The
        # blockquote markers go too — half of this section is a "> " callout, and a "> "
        # landing mid-sentence hides the phrase just as effectively as a newline.
        section = " ".join(
            word
            for line in section.splitlines()
            for word in re.sub(r"^\s*>\s?", "", line).split()
        )
        for wrong in ("beneficiary's IBANs", "those IBANs", "the IBANs you give"):
            self.assertNotIn(wrong, section, f"documentation still says {wrong!r}")
        self.assertIn("beneficiary's accounts", section)

    def test_the_key_the_tool_writes_is_the_key_the_scan_reads(self):
        # Pins the two halves together: whichever key create builds its payload with must
        # be one the scan looks under. A rename on one side alone fails here.
        source = (SCRIPTS / "create-payment.py").read_text(encoding="utf-8")
        self.assertIn('{"bank_account_number": acct}', source)
        self.assertIn('acct_field.get("bank_account_number")', source)


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


class ScriptHarness:
    """Shared fixture for the end-to-end tests, deliberately NOT a TestCase.

    A subclass of a TestCase inherits its test methods too, so unittest collects and runs
    every one of them a second time under the subclass's name. Each of these tests spawns
    a Python subprocess, and CI runs the whole suite twice (with pytest and without), so
    the duplicates cost real wall-clock for no extra coverage. Keeping the fixture in a
    plain mixin lets both test classes share it without sharing tests.
    """

    # A fresh HOME per test: a shared one lets an earlier test's ledger change what a
    # later one observes, and makes "assert no ledger was written" quietly meaningless.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paysera-cli-test-")
        # One directory per test METHOD, so without this the suite leaves one behind for
        # every test it runs — each holding a ledger with test IBANs and amounts.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.write_stub('printf \'{"items":[]}\\nHTTP:200\'')

    def write_stub(self, shell_body):
        stub = os.path.join(self.bin, "curl")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n" + shell_body + "\n")
        os.chmod(stub, 0o755)

    @property
    def ledger_path(self):
        return os.path.join(self.tmp, ".config", "paysera-payments", "ledger.json")

    def read_ledger(self):
        with open(self.ledger_path) as f:
            return json.load(f)

    def run_script(self, *extra):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env["PAYSERA_PAT"] = "test-token"
        base = [
            sys.executable,
            str(SCRIPTS / "create-payment.py"),
            "--payer", "EVP0000000000001",
            # The shipped ALLOWED_ACCOUNTS labels are placeholders, and the script now
            # refuses to send one as the payer display name — so supply a real one.
            "--payer-name", "Test Company, UAB",
            "--beneficiary-name", "Acme UAB",
            "--iban", "LT121000011101001000",
            "--purpose", "test",
        ]
        return subprocess.run(base + list(extra), capture_output=True, text=True, env=env, timeout=60)

    def run_script_bare(self, *argv):
        """Run the script with ONLY the given arguments — no payment defaults.

        --register-only creates nothing, so it must work without --amount/--iban/etc;
        run_script's defaults would hide that.
        """
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["HOME"] = self.tmp
        env["PAYSERA_PAT"] = "test-token"
        cmd = [sys.executable, str(SCRIPTS / "create-payment.py")] + list(argv)
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)


class TestAddressRequirementIsStatedAsItIsChecked(ScriptHarness, unittest.TestCase):
    """--help said one thing, the pre-flight check did another.

    The help called --beneficiary-address REQUIRED for any cross-border transfer; the
    check only demands it outside the SEPA zone. An operator who believes the help either
    passes an address the API does not want, or distrusts the check that let them through.
    """

    def test_a_cross_border_sepa_transfer_needs_no_address(self):
        # Poland, --priority normal: cross-border and not SEPA-Instant, so the old help
        # text said an address was required. It is not.
        out = self.run_script(
            "--amount", "10.00", "--iban", "PL27114020040000300201355387",
            "--beneficiary-type", "legal", "--priority", "normal",
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_the_help_states_the_rule_the_check_applies(self):
        out = self.run_script_bare("--help")
        self.assertEqual(out.returncode, 0, out.stderr)
        # The wrapped help is re-flowed by argparse, so match on collapsed whitespace.
        help_text = " ".join(out.stdout.split())
        # rindex, not index: the first occurrence is in the usage line, which carries no
        # help text at all — matching there would make this test unable to fail.
        start = help_text.rindex("--beneficiary-address")
        section = help_text[start : help_text.index("--iban", start)]
        self.assertIn("OUTSIDE the SEPA zone", section)


class TestCommandLineValidation(ScriptHarness, unittest.TestCase):
    """End-to-end argument checks: these live in main(), so they are exercised by running
    the script with a stubbed curl on PATH."""

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

    def test_placeholder_account_label_is_refused(self):
        # Without --payer-name the shipped label would reach the beneficiary.
        env_free = [
            sys.executable,
            str(SCRIPTS / "create-payment.py"),
            "--payer", "EVP0000000000001",
            "--beneficiary-name", "Acme UAB",
            "--iban", "LT121000011101001000",
            "--purpose", "test",
            "--amount", "12.34",
        ]
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   HOME=self.tmp, PAYSERA_PAT="test-token")
        out = subprocess.run(env_free, capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("placeholder", out.stdout + out.stderr)

    def test_an_explicit_payer_name_is_never_second_guessed(self):
        # A real company may legitimately be called "Example ..."; the guard is about the
        # unedited config label, not about a name the operator typed deliberately.
        out = self.run_script("--amount", "12.34", "--payer-name", "Example Holdings, UAB")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("DRY-RUN", out.stdout)

    def test_configuration_errors_are_reported_before_any_request(self):
        # A cross-border run with no --beneficiary-type must fail without spending the
        # duplicate check's network calls first.
        self.write_stub('echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\''
                        % self.tmp)
        out = self.run_script(
            "--amount", "50.00", "--invoice-id", "INV-INTL",
            "--iban", "UA213223130000026007233566001",
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--beneficiary-type is required", out.stdout + out.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "calls.log")),
            "no request should be made before a known-bad configuration is rejected",
        )

    def test_force_announces_that_the_duplicate_check_was_skipped(self):
        out = self.run_script("--amount", "12.34", "--invoice-id", "INV-X", "--force")
        self.assertIn("--force", out.stderr)
        self.assertIn("SKIPPED", out.stderr)

    def _payload(self, out):
        return json.loads(out.stdout.split("Payload:", 1)[1].split("\n\n", 1)[0])

    def test_the_payload_carries_the_validated_amount_not_the_raw_text(self):
        # "1e2" passes every check (Decimal("1e2") is finite, positive, exponent 2) and
        # used to reach the API and the ledger verbatim, in a notation it may read
        # differently. So did " 12.34" and "+12.34".
        for given, expected in [
            ("1e2", "100"),
            (" 12.34", "12.34"),
            ("+12.34", "12.34"),
            ("100.00", "100.00"),  # a written scale must survive untouched
        ]:
            with self.subTest(amount=given):
                out = self.run_script("--amount", given)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertEqual(self._payload(out)["amount"]["amount"], expected)

    def test_the_ledger_records_the_validated_amount(self):
        self.write_stub('printf \'{"id":"H1","status":"new"}\\nHTTP:201\'')
        out = self.run_script("--amount", "1e2", "--invoice-id", "INV-AMT", "--confirm")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.read_ledger()[0]["amount"], "100")

    def test_a_non_iban_account_without_a_bic_is_refused(self):
        # An Armenian-style national account number yields no country, which used to read
        # as "domestic, in SEPA": the cross-border checks were all skipped and the API
        # refused the transfer afterwards with mapper_beneficiary_country_not_set.
        self.write_stub('echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp)
        out = self.run_script("--amount", "50.00", "--iban", "20507231000012345")
        self.assertNotEqual(out.returncode, 0)
        # The specific message, not the generic unknown-country backstop below it: the
        # operator needs to be told WHICH input is missing and why the BIC supplies it.
        self.assertIn("is not an IBAN", out.stdout + out.stderr)
        self.assertIn("--beneficiary-bic", out.stdout + out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.log")))

    def test_a_non_iban_account_with_a_bic_is_routed_by_the_bic_country(self):
        # AM is outside SEPA, so the international requirements now apply — which is the
        # whole point of refusing to guess the country.
        out = self.run_script(
            "--amount", "50.00", "--iban", "20507231000012345",
            "--beneficiary-bic", "ARMJAM22", "--beneficiary-type", "legal",
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("outside the SEPA zone", out.stdout + out.stderr)

        ok = self.run_script(
            "--amount", "50.00", "--iban", "20507231000012345",
            "--beneficiary-bic", "ARMJAM22", "--beneficiary-type", "legal",
            "--beneficiary-address", "1 Main St", "--beneficiary-city", "Yerevan",
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        payload = self._payload(ok)
        self.assertEqual(payload["beneficiary"]["bank_account"]["bank_account_number"],
                         "20507231000012345")
        self.assertEqual(payload["beneficiary"]["additional_information"]["country"], "AM")
        # Not instant: the SEPA-Instant rail does not reach a non-SEPA country.
        self.assertNotIn("urgency", payload)

    def test_a_malformed_bic_on_a_non_iban_account_is_refused(self):
        out = self.run_script(
            "--amount", "50.00", "--iban", "20507231000012345", "--beneficiary-bic", "1234",
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("cannot determine the beneficiary's country", out.stdout + out.stderr)

    def test_a_recent_sepa_member_is_not_treated_as_an_international_wire(self):
        # An Albanian IBAN: in SEPA since 2023, so no BIC/address/city is demanded.
        out = self.run_script(
            "--amount", "50.00", "--iban", "AL35202111090000000001234567",
            "--beneficiary-type", "legal",
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("outside the SEPA zone", out.stdout)

    def test_a_paysera_iban_wins_even_when_written_with_separators(self):
        # 1.7.1 stripped separators at one call site, AFTER the payee had been chosen,
        # and never touched --also-iban at all. A Paysera IBAN pasted from an invoice
        # therefore lost the payment while the printed reason said none was listed.
        out = self.run_script(
            "--amount", "10.00",
            "--iban", "LT121000011101001000",
            "--also-iban", "LT60-3500-0100-0123-4567",
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Paysera IBAN (bank code 35000)", out.stdout)
        self.assertEqual(
            self._payload(out)["beneficiary"]["bank_account"]["iban"], "LT603500010001234567"
        )
        self.assertNotIn("-", self._payload(out)["beneficiary"]["bank_account"]["iban"])

    def test_every_listed_iban_is_normalised_not_just_the_first(self):
        out = self.run_script(
            "--amount", "10.00",
            "--iban", "LT12-1000-0111-0100-1000",
            "--also-iban", "LT74 4010 0510 0324 1521",
            "--also-iban", "LT36.7300.0101.2345.6789",
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        listed = out.stdout.split("dup-check only:", 1)[1].splitlines()[0]
        # Split on the display separator first — the remaining accounts must each be bare.
        accounts = [a.strip() for a in listed.split(",") if a.strip()]
        self.assertEqual(len(accounts), 2, listed)
        for account in accounts:
            with self.subTest(account=account):
                self.assertRegex(account, r"\A[A-Z]{2}[0-9A-Z]+\Z")
        self.assertIn("LT744010051003241521", accounts)
        self.assertIn("LT367300010123456789", accounts)

    def test_a_malformed_invoice_date_is_refused_not_quietly_ignored(self):
        # It used to warn on stderr, keep the default window, and then print "since
        # <the malformed value>" on stdout — two streams disagreeing about the period a
        # money-safety check covered. A day-first date is the realistic way to hit it.
        self.write_stub(
            'echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp
        )
        out = self.run_script(
            "--amount", "10.00", "--invoice-id", "INV-1", "--invoice-date", "15/06/2026"
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is not YYYY-MM-DD", out.stdout + out.stderr)
        self.assertNotIn("since 15/06/2026", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.log")))

    def test_the_reported_period_is_the_period_actually_scanned(self):
        # The report and the scan must come from one source. Checked by comparing the
        # printed period against the created_date_from the request really carried.
        self.write_stub(
            'echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp
        )
        out = self.run_script(
            "--amount", "10.00", "--invoice-id", "INV-1", "--invoice-date", "2026-06-15"
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("since 2026-06-15", out.stdout)
        with open(os.path.join(self.tmp, "calls.log")) as f:
            sent = int(re.search(r"created_date_from=(\d+)", f.read()).group(1))
        expected = cp.parse_invoice_date("2026-06-15") - 86400
        self.assertEqual(sent, expected)

    def test_a_future_invoice_date_is_refused(self):
        # A window starting after today can hold no transfer, so the live half of the
        # duplicate check is disabled — and it then reports "no prior payments", which
        # reads as an all-clear. A mistyped year is the usual way in.
        self.write_stub(
            'echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp
        )
        future = (datetime.date.today() + datetime.timedelta(days=400)).isoformat()
        out = self.run_script(
            "--amount", "10.00", "--invoice-id", "INV-1", "--invoice-date", future
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is in the future", out.stdout + out.stderr)
        self.assertNotIn("No prior payments", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.log")))

    def test_todays_invoice_date_is_accepted(self):
        # End-to-end companion to TestInvoiceDateBoundary below. On its own this cannot
        # see the boundary — it runs a subprocess against the real clock — so the day
        # boundary itself is pinned there, with a frozen clock.
        out = self.run_script(
            "--amount", "10.00", "--invoice-id", "INV-1",
            "--invoice-date", datetime.date.today().isoformat(),
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_an_iban_written_with_separators_is_normalised_not_rejected(self):
        # A hyphenated IBAN is a formatting slip. Telling the operator it "is not an
        # IBAN" and demanding a BIC sends them after the wrong problem entirely.
        out = self.run_script("--amount", "10.00", "--iban", "LT12-1000-0111-0100-1000")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("separators", out.stderr)
        self.assertEqual(
            self._payload(out)["beneficiary"]["bank_account"]["iban"],
            "LT121000011101001000",
        )

    def test_a_genuine_non_iban_account_is_still_refused(self):
        # The separator rule must not swallow the case it sits in front of: stripping
        # hyphens from an Armenian account number does not make it an IBAN.
        out = self.run_script("--amount", "10.00", "--iban", "2050-7231-0000-12345")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is not an IBAN", out.stdout + out.stderr)

    def test_the_config_directory_is_tightened_to_0700(self):
        # os.makedirs(mode=...) only applies the mode to a directory it CREATES, and the
        # user normally creates this one first, for the token, under umask 022.
        d = os.path.join(self.tmp, ".config", "paysera-payments")
        os.makedirs(d, mode=0o755)
        os.chmod(d, 0o755)
        self.write_stub('printf \'{"id":"H1","status":"new"}\\nHTTP:201\'')
        self.run_script("--amount", "10.00", "--invoice-id", "INV-MODE", "--confirm")
        self.assertEqual(mode_of(d), 0o700)
        self.assertEqual(mode_of(self.ledger_path), 0o600)

    def test_dry_run_writes_no_ledger_entry(self):
        # Unconditional: setUp gives this test its own HOME, so the ledger cannot
        # pre-exist and the assertion cannot silently turn itself off.
        self.assertFalse(os.path.exists(self.ledger_path))
        self.run_script("--amount", "12.34", "--invoice-id", "INV-DRY")
        self.assertFalse(os.path.exists(self.ledger_path), "a dry run must not record an attempt")


class TestWriteAheadLedger(ScriptHarness, unittest.TestCase):
    """The 1.5.0 fix is the ORDER of the write relative to the POST.

    Moving append_ledger() after http_json_post() leaves every unit test green while
    restoring the double-payment defect, so it has to be pinned end to end.
    """

    def test_attempt_is_recorded_even_when_the_answer_never_arrives(self):
        self.write_stub("exit 28")  # curl's timeout exit code
        out = self.run_script("--amount", "100.00", "--invoice-id", "INV-WA", "--confirm")

        self.assertNotEqual(out.returncode, 0)
        self.assertTrue(
            os.path.exists(self.ledger_path),
            "a request that was sent but not answered must still be recorded — "
            "otherwise a retry creates a second draft",
        )
        rows = self.read_ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["transfer_hash"])
        self.assertIn("NOT known whether the transfer was created", out.stderr)

    def test_the_retry_after_an_unanswered_request_is_refused(self):
        self.write_stub("exit 28")
        self.run_script("--amount", "100.00", "--invoice-id", "INV-WA", "--confirm")

        # Second run, API healthy again: it must NOT create another draft.
        self.write_stub('printf \'{"items":[]}\\nHTTP:200\'')
        retry = self.run_script("--amount", "100.00", "--invoice-id", "INV-WA", "--confirm")

        self.assertEqual(retry.returncode, 3, "an unconfirmed attempt must block the retry")
        self.assertIn("UNCONFIRMED", retry.stdout)
        self.assertEqual(len(self.read_ledger()), 1, "no second row may be written")

    def test_a_definite_refusal_does_not_block_the_retry(self):
        self.write_stub('printf \'{"error":"bad_request"}\\nHTTP:400\'')
        first = self.run_script("--amount", "100.00", "--invoice-id", "INV-400", "--confirm")
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(self.read_ledger()[0]["state"], "failed")

        self.write_stub('printf \'{"items":[]}\\nHTTP:200\'')
        retry = self.run_script("--amount", "100.00", "--invoice-id", "INV-400")
        self.assertNotIn("SKIP", retry.stdout)

    def test_a_successful_create_is_recorded_with_its_hash(self):
        self.write_stub('printf \'{"id":"HASH123","status":"new"}\\nHTTP:201\'')
        out = self.run_script("--amount", "100.00", "--invoice-id", "INV-OK", "--confirm")
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = self.read_ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "created")
        self.assertEqual(rows[0]["transfer_hash"], "HASH123")


class TestInvoiceDateBoundary(unittest.TestCase):
    """"Is this date in the future?" is a CALENDAR question, in Vilnius.

    The guard first compared UTC midnight of the given day against the current instant.
    For the first 2-3 hours of every Vilnius day, UTC midnight of that day has not
    arrived, so TODAY was refused as future — in a message that printed today's Vilnius
    date as the reason it was not today. An ordinary run with a correct invoice date
    stopped every night, and the natural workaround (dropping --invoice-date) puts the
    scan back on the wide default window that 1.7.0 narrowed on purpose.

    A frozen clock is the point: the end-to-end test used date.today() against the real
    clock, which on a UTC runner can never fail.
    """

    def _refused(self, when, spec):
        with frozen_clock(cp, when):
            return cp.invoice_date_error(spec)

    def test_today_is_accepted_at_every_hour_including_the_small_ones(self):
        for month, day in [(8, 13), (1, 15)]:  # EEST (+3) and EET (+2)
            for hour in (0, 1, 2, 3, 12, 23):
                when = vilnius(2026, month, day, hour, 30)
                today = when.date().isoformat()
                with self.subTest(when=str(when)):
                    self.assertIsNone(
                        self._refused(when, today),
                        f"today ({today}) was refused at {hour:02d}:30 Vilnius",
                    )

    def test_yesterday_and_older_are_accepted(self):
        when = vilnius(2026, 8, 13, 0, 30)
        for spec in ("2026-08-12", "2026-01-01", "2025-06-15"):
            with self.subTest(spec=spec):
                self.assertIsNone(self._refused(when, spec))

    def test_tomorrow_is_refused_even_late_at_night(self):
        # The other side of the boundary must not move either: at 23:30 Vilnius,
        # tomorrow's UTC midnight has already passed, so an epoch comparison would have
        # let it through.
        when = vilnius(2026, 8, 13, 23, 30)
        error = self._refused(when, "2026-08-14")
        self.assertIsNotNone(error)
        self.assertIn("is in the future", error)

    def test_a_mistyped_year_is_refused(self):
        error = self._refused(vilnius(2026, 8, 13, 12, 0), "2027-08-13")
        self.assertIn("is in the future", error)
        self.assertIn("2026-08-13", error, "the message must name today in Vilnius")

    def test_the_error_message_never_contradicts_itself(self):
        # The old message said "<date> is in the future (today is <the same date>)".
        for hour in range(0, 24):
            when = vilnius(2026, 8, 13, hour, 30)
            error = self._refused(when, when.date().isoformat())
            with self.subTest(hour=hour):
                self.assertIsNone(error, error)

    def test_a_malformed_date_is_still_reported_first(self):
        error = self._refused(vilnius(2026, 8, 13, 0, 30), "15/06/2026")
        self.assertIn("is not YYYY-MM-DD", error)

    def test_no_date_is_not_an_error(self):
        self.assertIsNone(self._refused(vilnius(2026, 8, 13, 0, 30), None))
        self.assertIsNone(self._refused(vilnius(2026, 8, 13, 0, 30), ""))


class TestAccountNormalisation(unittest.TestCase):
    """_norm_iban drives three decisions — which listed IBAN is paid, the duplicate
    check's candidate set, and de-duplication of the listed IBANs — so a spelling it
    cannot see through breaks all three at once."""

    def test_norm_iban_sees_through_every_separator(self):
        for spelling in [
            "LT60 3500 0100 0123 4567",
            "LT60-3500-0100-0123-4567",
            "LT60.3500.0100.0123.4567",
            "lt6035000100 0123-4567",
            "LT60‐3500‑0100–0123—4567",  # unicode dashes
        ]:
            with self.subTest(spelling=spelling):
                self.assertEqual(cp._norm_iban(spelling), "LT603500010001234567")

    def test_a_paysera_iban_is_recognised_through_separators(self):
        for spelling in ["LT60-3500-0100-0123-4567", "LT60 3500 0100 0123 4567"]:
            with self.subTest(spelling=spelling):
                self.assertTrue(cp.is_paysera_iban(spelling))

    def test_the_duplicate_check_matches_a_separated_candidate(self):
        # Scenario B of the report: the API returns the IBAN without separators, so an
        # unnormalised candidate could never match, and the account contributed nothing
        # to the check while still being counted as scanned.
        rows = [
            {
                "id": "OLD1",
                "status": "done",
                "amount": {"amount": "250.00", "currency": "EUR"},
                "beneficiary": {"bank_account": {"iban": "LT603500010001234567"}},
                "purpose": {"details": "unrelated"},
            }
        ]
        for spelling in ["LT60-3500-0100-0123-4567", "LT603500010001234567"]:
            with self.subTest(spelling=spelling):
                with mock.patch.object(cp, "list_transfers", lambda *a, **k: rows), \
                        mock.patch.object(cp, "load_ledger", lambda: []):
                    blocking, seen = cp.find_blocking(
                        "INV-1", "t", payer="EVP1",
                        ibans=["LT121000011101001000", spelling],
                        amount="250.00", currency="EUR",
                    )
                self.assertTrue(blocking, "a prior payment to this account was missed")
                self.assertTrue(seen)

    def test_clean_account_leaves_a_non_iban_number_alone(self):
        # Stripping punctuation from a national account number is not safe — there is no
        # rule saying its separators are decorative.
        cleaned, note = cp.clean_account("2050-7231-0000-12345")
        self.assertEqual(cleaned, "2050-7231-0000-12345")
        self.assertIsNone(note)

    def test_clean_account_reports_what_it_changed(self):
        cleaned, note = cp.clean_account("LT12-1000-0111-0100-1000")
        self.assertEqual(cleaned, "LT121000011101001000")
        self.assertIn("LT121000011101001000", note)


class TestEveryHashInAUrlIsChecked(unittest.TestCase):
    """The shape check guarded the two hashes that come from a person, and neither of the
    two that come from a file or from the network.

    Not a security boundary — the ledger is 0600 in a 0700 directory, and whoever can
    write it can edit the script. It is the rule the project states about this exact URL
    construction, applied everywhere the URL is built rather than at two of four sites.
    """

    BAD = ["../../transfers/OTHER", "H?status=done", "H/register", "a b", "", None, "x" * 200]

    def test_the_checker_accepts_a_hash_and_refuses_the_rest(self):
        self.assertEqual(cp._checked_hash("  HASH-123_x  "), "HASH-123_x")
        for bad in self.BAD:
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    cp._checked_hash(bad)

    def test_a_malformed_ledger_hash_blocks_instead_of_being_fetched(self):
        calls = []

        def spy(method, url, token, **kw):
            calls.append(url)
            return "200", {"id": "x", "status": "done"}

        with temp_ledger(cp):
            cp._ensure_config_dir()
            cp.append_ledger(
                {"invoice_id": "INV-1", "state": "created",
                 "transfer_hash": "../../transfers/OTHER"}
            )
            with mock.patch.object(cp, "http_json", spy):
                blocking, _ = cp.find_blocking("INV-1", token="t")
        self.assertEqual(calls, [], "a URL was built from the malformed row")
        self.assertEqual(len(blocking), 1, "the row must still block — fail safe")
        self.assertIn("malformed", blocking[0][3])

    def test_register_refuses_an_id_it_cannot_put_in_a_url(self):
        with mock.patch.object(cp, "http_json") as http:
            ok, code, body = cp.register_transfer("../../other/register", "t")
        self.assertFalse(ok)
        self.assertEqual(code, "unusable-id")
        http.assert_not_called()

    def test_a_good_id_still_reaches_the_register_endpoint(self):
        # Otherwise "refuse everything" would pass the test above.
        with mock.patch.object(cp, "http_json", return_value=("200", {})) as http:
            ok, code, _ = cp.register_transfer("HASH123", "t")
        self.assertTrue(ok)
        self.assertTrue(http.call_args[0][1].endswith("/HASH123/register"))

    def test_the_code_text_helper_reads_correctly_either_way(self):
        self.assertEqual(cp._code_text("500"), "HTTP 500")
        self.assertEqual(cp._code_text("unusable-id"), "unusable-id")


class TestExitCodeContract(ScriptHarness, unittest.TestCase):
    """SKILL.md is what an agent reads to decide what a run meant, so every code the
    script can produce has to be in it. Exit 2 was not — and a --payer outside the token's
    scope is an ordinary mistake, not a rare one."""

    def _paragraph(self, marker):
        """The one paragraph that starts with `marker`, as a set of codes.

        Per paragraph, not over the whole block: the two scripts are documented one after
        the other, and pooling their digits let a code go missing from one list while the
        other still spelled it (which is how a mutation of this test's own subject passed).
        """
        skill = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(marker, skill, f"SKILL.md no longer documents {marker!r}")
        start = skill.index(marker)
        para = skill[start : skill.index("\n\n", start)]
        return {int(n) for n in re.findall(r"`(\d)`", para)}, para

    def test_a_payer_outside_the_scope_exits_2(self):
        out = self.run_script("--amount", "10.00", "--payer", "EVP9999999999999")
        self.assertEqual(out.returncode, 2)
        self.assertIn("scoped accounts", out.stdout + out.stderr)

    def test_a_missing_required_argument_exits_2(self):
        out = self.run_script_bare("--payer", "EVP0000000000001")
        self.assertEqual(out.returncode, 2)

    def test_every_code_the_script_can_exit_with_is_documented(self):
        documented, _ = self._paragraph("Exit codes:")
        # Read off the source, not off a list kept by hand next to it: a new sys.exit(N)
        # is exactly the change that would leave the documentation behind.
        source = (SCRIPTS / "create-payment.py").read_text(encoding="utf-8")
        used = {int(n) for n in re.findall(r"sys\.exit\((\d+)\)", source)}
        used |= {0, 1}  # a bare sys.exit(message) is 1; falling off the end is 0
        used.add(2)  # argparse's ap.error()
        self.assertTrue(
            used <= documented, f"undocumented exit code(s): {sorted(used - documented)}"
        )

    def test_the_cancel_script_codes_are_documented_too(self):
        documented, _ = self._paragraph("`cancel-payment.py` has its own")
        source = (SCRIPTS / "cancel-payment.py").read_text(encoding="utf-8")
        used = {0, 1} | {int(n) for n in re.findall(r"rc = (\d)", source)}
        used.add(2)  # argparse
        self.assertEqual(used, documented)


class TestRegisterStep(ScriptHarness, unittest.TestCase):
    """A transfer that is created but not registered stays in `new`, which is shown
    nowhere for signing — the original "invisible draft" problem.

    There was no coverage here at all: the one test that reached this code stubbed a curl
    answering 201 to everything, so the register call succeeded by accident of the stub.
    """

    def _stub(self, register_status):
        self.write_stub(
            'case "$*" in\n'
            '  *register*) printf \'{"error":"e"}\\nHTTP:%s\';;\n'
            '  *credit_account_number*) printf \'{"items":[]}\\nHTTP:200\';;\n'
            '  *transfers/HASH123*) printf \'{"id":"HASH123","status":"new"}\\nHTTP:200\';;\n'
            '  *) printf \'{"id":"HASH123","status":"new"}\\nHTTP:201\';;\n'
            'esac' % register_status
        )

    def _create(self, *extra):
        return self.run_script(
            "--amount", "10.00", "--invoice-id", "INV-REG", "--confirm", *extra
        )

    def test_a_failed_register_does_not_report_success(self):
        self._stub("500")
        out = self._create()
        self.assertEqual(
            out.returncode, 4,
            "exit 0 tells a wrapper the payment is ready to sign when it is invisible",
        )
        combined = out.stdout + out.stderr
        self.assertIn("NOT SIGNABLE", combined)
        self.assertIn("--register-only HASH123", combined)
        # The old run ended on this sentence, four lines after saying it was invisible.
        self.assertNotIn("Open the Paysera app and SIGN", out.stdout)

    def test_a_failed_register_is_recorded_in_the_ledger(self):
        self._stub("500")
        self._create()
        row = self.read_ledger()[0]
        self.assertEqual(row["transfer_hash"], "HASH123")
        self.assertIs(row["registered"], False)

    def test_a_successful_register_is_recorded_in_the_ledger(self):
        # Both outcomes used to be written as state="created", indistinguishable.
        self.write_stub('printf \'{"id":"HASH123","status":"new"}\\nHTTP:201\'')
        out = self._create()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIs(self.read_ledger()[0]["registered"], True)
        self.assertIn("Open the Paysera app and SIGN", out.stdout)

    def test_the_exit_code_distinguishes_unregistered_from_not_created(self):
        # 1 invites a retry; this draft exists and must NOT be created again.
        self._stub("500")
        self.assertEqual(self._create().returncode, cp.EXIT_UNREGISTERED)
        self.assertNotEqual(cp.EXIT_UNREGISTERED, 1)

    def test_a_later_run_points_at_the_remedy_and_not_at_force(self):
        self._stub("500")
        self._create()
        second = self._create()
        self.assertEqual(second.returncode, 3)
        self.assertIn("never REGISTERED", second.stdout)
        self.assertIn("--register-only HASH123", second.stdout)
        self.assertNotIn(
            "Use --force to override.", second.stdout,
            "--force creates a second draft; it is the opposite of the remedy above",
        )

    def test_no_register_says_the_transfer_is_not_yet_signable(self):
        self.write_stub('printf \'{"id":"HASH123","status":"new"}\\nHTTP:201\'')
        out = self._create("--no-register")
        self.assertEqual(out.returncode, 0, "an explicit choice is not an error")
        self.assertIn("NOT yet signable", out.stdout)
        self.assertIn("--register-only HASH123", out.stdout)
        self.assertNotIn("Open the Paysera app and SIGN", out.stdout)

    def test_register_only_finishes_the_draft(self):
        self._stub("500")
        self._create()
        self.write_stub('printf \'{"id":"HASH123","status":"registered"}\\nHTTP:200\'')
        out = self.run_script_bare("--register-only", "HASH123", "--confirm")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("OK registered HASH123", out.stdout)
        self.assertIs(self.read_ledger()[0]["registered"], True)

    def test_register_only_is_dry_run_by_default(self):
        self.write_stub('echo "$@" >> %s/calls.log; printf \'{}\\nHTTP:200\'' % self.tmp)
        out = self.run_script_bare("--register-only", "HASH123")
        self.assertEqual(out.returncode, 0)
        self.assertIn("DRY-RUN", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.log")))

    def test_register_only_needs_no_payment_arguments(self):
        # It creates nothing, so --amount/--iban/--purpose do not apply. argparse used to
        # demand them, which made the documented remedy impossible to run.
        self.write_stub('printf \'{"id":"HASH123","status":"registered"}\\nHTTP:200\'')
        out = self.run_script_bare("--register-only", "HASH123", "--confirm")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_the_payment_arguments_are_still_required_for_a_payment(self):
        out = self.run_script_bare("--payer", "EVP0000000000001", "--amount", "10.00")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("the following arguments are required", out.stderr)
        for flag in ("--beneficiary-name", "--iban", "--purpose"):
            self.assertIn(flag, out.stderr)

    def test_a_malformed_hash_is_refused_before_any_request(self):
        self.write_stub('echo "$@" >> %s/calls.log; printf \'{}\\nHTTP:200\'' % self.tmp)
        for bad in ["../../etc/passwd", "HASH 123", "a/b", ""]:
            with self.subTest(hash=bad):
                out = self.run_script_bare("--register-only", bad, "--confirm")
                self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.log")))

    def test_a_failed_register_only_reports_and_does_not_claim_success(self):
        self.write_stub('printf \'{"error":"e"}\\nHTTP:500\'')
        out = self.run_script_bare("--register-only", "HASH123", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("register FAILED", out.stdout + out.stderr)
        self.assertNotIn("OK registered", out.stdout)


class TestDuplicateCheckWindow(ScriptHarness, unittest.TestCase):
    """Without --invoice-date the live scan used to read the FULL account history, where
    the amount rule alone blocks — a supplier paid the same sum monthly was refused every
    month after the first, and the only documented escape (--force) turns the entire
    duplicate check off, ledger included."""

    def _prior_payment(self, purpose):
        row = {
            "items": [
                {
                    "id": "OLD1",
                    "status": "done",
                    "amount": {"amount": "250.00", "currency": "EUR"},
                    "beneficiary": {"bank_account": {"iban": "LT121000011101001000"}},
                    "purpose": {"details": purpose},
                }
            ]
        }
        # Only the LIST call may return a row; the create POST must answer separately.
        self.write_stub(
            'case "$*" in *credit_account_number*) printf \'%s\\nHTTP:200\';; '
            '*) printf \'{"id":"NEW1","status":"new"}\\nHTTP:201\';; esac'
            % json.dumps(row).replace("%", "%%")
        )

    def test_the_scan_is_bounded_when_no_invoice_date_is_given(self):
        self.write_stub(
            'echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp
        )
        out = self.run_script("--amount", "250.00", "--invoice-id", "INV-NODATE")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with open(os.path.join(self.tmp, "calls.log")) as f:
            calls = f.read()
        match = re.search(r"created_date_from=(\d+)", calls)
        self.assertIsNotNone(match, calls)
        window_days = (time.time() - int(match.group(1))) / 86400
        self.assertNotEqual(int(match.group(1)), 0, "the full account history is too wide")
        self.assertAlmostEqual(window_days, cp.DEFAULT_LOOKBACK_DAYS, delta=1)
        self.assertIn(f"last {cp.DEFAULT_LOOKBACK_DAYS} days", out.stdout)

    def test_an_amount_only_match_says_so_and_does_not_push_towards_force(self):
        self._prior_payment("Monthly retainer 2026-07")
        out = self.run_script("--amount", "250.00", "--invoice-id", "INV-AUG")
        self.assertEqual(out.returncode, 3)
        self.assertIn("SAME AMOUNT only", out.stdout)
        self.assertIn("--invoice-date", out.stdout)
        # --force must not be the headline remedy for a circumstantial match.
        self.assertNotIn("Use --force to override.", out.stdout)

    def test_an_invoice_id_match_still_blocks_outright(self):
        self._prior_payment("Payment for INV-AUG")
        out = self.run_script("--amount", "999.00", "--invoice-id", "INV-AUG")
        self.assertEqual(out.returncode, 3)
        self.assertIn("purpose quotes this invoice id", out.stdout)
        self.assertIn("Use --force to override.", out.stdout)


class TestConcurrentRuns(ScriptHarness, unittest.TestCase):
    """The ledger's read-append-write is a lost-update race between two runs, and the row
    it loses is usually the write-ahead `pending` one — the ONLY record of an unsigned
    draft, since GET /transfers cannot list them."""

    @property
    def lock_path(self):
        return os.path.join(self.tmp, ".config", "paysera-payments", ".lock")

    def _hold_the_lock(self):
        import fcntl

        os.makedirs(os.path.dirname(self.lock_path), mode=0o700, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)

    def test_a_second_run_refuses_rather_than_racing(self):
        self._hold_the_lock()
        self.write_stub('printf \'{"id":"H1","status":"new"}\\nHTTP:201\'')
        out = self.run_script("--amount", "10.00", "--invoice-id", "INV-LOCK", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("another paysera-payments run", out.stdout + out.stderr)
        self.assertFalse(
            os.path.exists(self.ledger_path), "the blocked run must not touch the ledger"
        )

    def test_the_lock_is_taken_before_the_duplicate_check_not_after(self):
        # The critical section is check-THEN-record: locking only the ledger write still
        # lets two runs both pass their own duplicate check and both go on to create a
        # draft. Taken up front, a blocked run stops before it issues a single request.
        self._hold_the_lock()
        self.write_stub(
            'echo "$@" >> %s/calls.log; printf \'{"items":[]}\\nHTTP:200\'' % self.tmp
        )
        out = self.run_script("--amount", "10.00", "--invoice-id", "INV-LOCK", "--confirm")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp, "calls.log")),
            "the duplicate check ran anyway — the lock is being taken too late",
        )

    def test_a_dry_run_is_not_blocked(self):
        # A preview mutates nothing, so it must not be stopped by a real payment in
        # flight — nor take the lock and stop one.
        self._hold_the_lock()
        out = self.run_script("--amount", "10.00", "--invoice-id", "INV-LOCK")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("DRY-RUN", out.stdout)

    def test_a_normal_run_releases_the_lock_for_the_next_one(self):
        self.write_stub('printf \'{"id":"H1","status":"new"}\\nHTTP:201\'')
        first = self.run_script("--amount", "10.00", "--invoice-id", "INV-A", "--confirm")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script("--amount", "10.00", "--invoice-id", "INV-B", "--confirm")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.read_ledger()), 2, "both rows must survive")

    def test_the_lock_is_reentrant_within_one_process(self):
        # append_ledger and update_ledger both take it, and main() holds it around them.
        # flock is per open-file-description, so a second open would deadlock on itself.
        with temp_ledger(cp):
            with cp.ledger_lock():
                cp.append_ledger({"attempt_id": "x", "state": "pending", "invoice_id": "I"})
                self.assertTrue(cp.update_ledger("x", state="created"))
            self.assertEqual(cp.load_ledger()[0]["state"], "created")

    def test_the_lock_is_released_when_the_run_exits_early(self):
        """main() must own the lock's lifetime, not the garbage collector.

        The ExitStack was a bare local with nothing closing it, so the release depended on
        CPython refcounting collecting the stack — and through it closing the ledger_lock
        generator. Run in-process, because a subprocess would exit and let the kernel drop
        the flock regardless, which is the interpreter detail this is meant to stop
        relying on.

        The traceback is kept alive on purpose. A live traceback holds the frames, and
        the frames hold the ExitStack, so refcounting cannot collect it — which is what
        an implementation without refcounting looks like from here. (`assertRaises` is no
        good for this: it calls traceback.clear_frames(), which drops the locals and
        releases the lock by itself, hiding the difference.)
        """
        with temp_ledger(cp):
            cp.append_ledger(
                {
                    "attempt_id": "a1",
                    "state": "pending",
                    "invoice_id": "INV-EXIT",
                    "transfer_hash": None,
                    "created_at_iso": "2026-08-12T00:00:00+00:00",
                }
            )
            argv = [
                "create-payment.py",
                "--payer", "EVP0000000000001",
                "--payer-name", "Test Company, UAB",
                "--beneficiary-name", "Acme UAB",
                "--iban", "LT121000011101001000",
                "--purpose", "test",
                "--amount", "10.00",
                "--invoice-id", "INV-EXIT",
                "--confirm",
            ]
            held = None
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(
                os.environ, {"PAYSERA_PAT": "t"}
            ), mock.patch.object(cp, "list_transfers", lambda *a, **k: []):
                try:
                    cp.main()
                except SystemExit as exit_:
                    # Keeping the traceback referenced pins every frame below main(),
                    # so nothing here can be released by refcounting alone.
                    held = (exit_.code, exit_.__traceback__)
            self.assertIsNotNone(held, "the run was expected to exit")
            # The pending row blocks, so the run exits 3 while still holding the lock.
            self.assertEqual(held[0], 3)
            self.assertEqual(cp._lock_depth, 0, "the lock outlived the run that took it")
            self.assertIsNone(cp._lock_fd)
            del held

    def test_the_lock_file_is_private(self):
        with temp_ledger(cp):
            with cp.ledger_lock():
                self.assertEqual(mode_of(cp._lock_path()), 0o600)


if __name__ == "__main__":
    unittest.main()
