"""Tests for the publication gate.

This script decides what is allowed to become public, so its heuristics need the same
protection as the skill code. The table below is the specification: each sample is a line
that either must fail CI or must not.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402

# (sample line, must_be_flagged, why)
SAMPLES = [
    # --- internal hosts that must never reach the public repository ----------------
    ("see https://wiki.internal/page", True, "internal host in a URL"),
    ("see wiki.internal for docs", True, "internal host, bare"),
    ("see `wiki.internal` for docs", True, "internal host in backticks"),
    ("host: db.corp.local", True, "internal TLD, bare"),
    ("host: `db.corp.local`", True, "internal TLD in backticks"),
    ("https://paysera.intranet.lt/x", True, "intranet label, real ccTLD"),
    ("`paysera.intranet.lt`", True, "intranet label in backticks"),
    ("mirror of gitlab.paysera.net", True, "non-public Paysera host, bare"),
    ("mirror of `gitlab.paysera.net`", True, "non-public Paysera host in backticks"),
    ("https://jira.example.com/browse/KEY-123", True, "issue tracker URL"),
    ("https://git.example.com/-/merge_requests/7", True, "forge merge request URL"),
    # --- ordinary code and prose that must not be flagged --------------------------
    ("from mypkg.internal.helpers import x", False, "import statement"),
    ("import os.path", False, "import statement"),
    ("use `pkg.internal.helpers` to reach it", False, "dotted code path in backticks"),
    ("call pkg.internal.helpers(arg)", False, "dotted call"),
    ("see docs in internal.md", False, "filename"),
    ("see docs in `internal.md`", False, "filename in backticks"),
    ("app/config.test holds the fixture", False, "file path"),
    ("`app/config.test`", False, "file path in backticks"),
    ("run pytest plugins -q", False, "ordinary prose"),
    ("the internal review must confirm this", False, "the word internal in prose"),
    ("https://api.paysera.com/public/transfer", False, "public Paysera host"),
    ("`bank.paysera.com`", False, "public Paysera host in backticks"),
    ("https://github.com/paysera/skills", False, "public third-party host"),
    ("https://code.claude.com/docs/en/plugin-marketplaces", False, "public docs link"),
    # --- shell assignments are a realistic way to write a REAL internal host ---------
    # (in a bash block or a workflow `run:` step) and must never be exempt.
    ("HOST=wiki.internal", True, "shell assignment, no spaces"),
    ("  HOST=db.corp.local", True, "indented shell assignment"),
    ("export API_HOST=paysera.intranet.lt", True, "exported shell assignment"),
    ("HOST=wiki.internal  # a comment", True, "shell assignment with a trailing comment"),
    # In a .md or .yml file, a spaced assignment is still checked — only Python source
    # gets the code-chain exemption (see TestPythonAssignmentExemption).
    ("handler = mypkg.internal.io", True, "spaced assignment outside Python source"),
    ('HOST = "db.corp.local"', True, "quoted internal host in an assignment"),
    ("host: db.corp.local", True, "YAML value is a host, not an assignment"),
    ("value = pkg.internal.helpers", False, "last label cannot be a TLD"),
    # `.test` is a reserved TLD and a common file suffix; only a URL settles it.
    ("config.test holds the fixture", False, "bare fixture filename"),
    ("`config.test`", False, "bare fixture filename in backticks"),
    ("https://config.test/path", True, "reserved TLD used as a real host"),
    # Both spellings on one line: "in a URL" is decided per occurrence, not per line.
    ("`config.test` is a file; https://config.test/x is a host", True, "URL wins on its own"),
    # --- private IP addresses -------------------------------------------------------
    # The HOSTNAME pattern requires a letters-only last label, so it can never match an
    # address. Naming an internal box by IP is at least as common as naming it by host.
    ("see http://10.20.30.40/dashboard for the queue", True, "RFC1918 10/8 in a URL"),
    ("see https://192.168.1.50:8080/jenkins", True, "RFC1918 192.168/16 with a port"),
    ("DB_HOST=172.16.4.9", True, "RFC1918 172.16/12 in a shell assignment"),
    ("ssh deploy@10.0.0.5", True, "private address outside any URL"),
    ("https://127.0.0.1:9000/admin", True, "loopback address"),
    ("host: 169.254.10.1", True, "link-local address in a YAML value"),
    ("`10.1.2.3`", True, "private address in backticks"),
    ("grafana at https://wiki.localhost/panel", True, "reserved .localhost TLD"),
    # A public address may legitimately appear in an example, and 172.32 is outside the
    # RFC 1918 block — the boundaries have to be exact or the gate cries wolf.
    ("connect to 8.8.8.8 for DNS", False, "public address"),
    ("172.32.0.1 is outside the private range", False, "just past 172.16/12"),
    ("172.15.0.1 is outside the private range", False, "just before 172.16/12"),
    ("11.0.0.1 is public", False, "just past 10/8"),
    ("192.169.0.1 is public", False, "just past 192.168/16"),
    ("requires version 10.0.0.5 or later", True, "a version string reads as an address"),
    # --- bare issue-tracker keys ----------------------------------------------------
    # CONTRIBUTING.md forbids ticket references outright, not merely tracker links: a
    # bare key in a comment says as much about internal work as a URL does.
    ("see ABC-1234 for the rationale", True, "bare ticket key in prose"),
    ("fixes XYZ-99", True, "bare ticket key, short number"),
    ("see `PROJ-42`", True, "bare ticket key in backticks"),
    ("# workaround for QWERTY-7", True, "bare ticket key in a code comment"),
    # Standards and formats are spelled exactly like ticket keys, so the prefix
    # allowlist is what separates them. Each of these must stay quiet.
    ("the ISO-2 country code", False, "standards prefix"),
    ("RFC-1918 private ranges", False, "standards prefix"),
    ("UTF-8 encoding throughout", False, "format prefix"),
    ("SHA-256 digest", False, "algorithm prefix"),
    ("CVE-2024-1234 is public", False, "public vulnerability id"),
    ("tracks INV-2026-001", False, "this repository's invoice placeholder"),
    ("a KEY-123 placeholder", False, "the documented tracker-URL placeholder"),
    ("version 2.1-3 here", False, "not a key: does not start with a letter"),
]


class TestPublishedContentScan(unittest.TestCase):
    """Each sample is written into a real file inside a temporary repository, so the
    check is exercised exactly as CI runs it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="paysera-validate-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "plugins").mkdir()
        self._real_root = validate.ROOT
        validate.ROOT = self.root
        self.addCleanup(setattr, validate, "ROOT", self._real_root)

    def scan(self, line):
        (self.root / "README.md").write_text(f"# Test\n\n{line}\n", encoding="utf-8")
        return validate.scan_published_content()

    def test_samples(self):
        for line, must_flag, why in SAMPLES:
            with self.subTest(sample=line, why=why):
                errors = self.scan(line)
                if must_flag:
                    self.assertTrue(errors, f"should have been flagged ({why}): {line!r}")
                else:
                    self.assertEqual(errors, [], f"false positive ({why}): {line!r}")

    def test_backticks_are_not_an_escape_hatch(self):
        """Pinned separately because it was a real defect: a backtick exemption made the
        most likely spelling of a leaked host the one spelling that passed."""
        for host in ["wiki.internal", "db.corp.local", "gitlab.paysera.net"]:
            with self.subTest(host=host):
                bare = self.scan(f"host: {host}")
                quoted = self.scan(f"host: `{host}`")
                self.assertTrue(bare, f"{host} must be flagged bare")
                self.assertTrue(quoted, f"{host} must be flagged in backticks too")

    def test_every_published_suffix_is_scanned(self):
        for name in ["README.md", "thing.py", "data.json", "ci.yml", "ci.yaml"]:
            with self.subTest(filename=name):
                (self.root / name).write_text("see https://wiki.internal/x\n", encoding="utf-8")
                errors = validate.scan_published_content()
                (self.root / name).unlink()
                self.assertTrue(errors, f"{name} must be scanned")

    def test_the_scan_covers_the_repository_root_not_only_plugins(self):
        # The marketplace catalogue copies each skill description verbatim.
        (self.root / ".claude-plugin").mkdir()
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"plugins": [{"description": "see https://wiki.internal/x"}]}),
            encoding="utf-8",
        )
        self.assertTrue(validate.scan_published_content())

    def test_the_validator_itself_is_exempt(self):
        """The exemption is the one rule that lets a file bypass the gate, so it is
        pinned with real files — and with a decoy that must NOT inherit it."""
        internal_host = "wiki." + "internal"
        line = f"see https://{internal_host}/page\n"

        scripts_dir = self.root / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "validate.py").write_text(line, encoding="utf-8")
        (scripts_dir / "test_validate.py").write_text(line, encoding="utf-8")
        # Same names, different directory: the exemption must not follow the name alone.
        decoy_dir = self.root / "plugins" / "demo" / "scripts"
        decoy_dir.mkdir(parents=True)
        (decoy_dir / "validate.py").write_text(line, encoding="utf-8")

        # The exemption is anchored to the real scripts/ directory, so point it at ours.
        with mock.patch.object(validate, "__file__", str(scripts_dir / "validate.py")):
            errors = validate.scan_published_content()

        flagged = {e.split(":")[0] for e in errors}
        self.assertNotIn("scripts/validate.py", flagged, "the checker must be exempt")
        self.assertNotIn("scripts/test_validate.py", flagged, "its tests must be exempt")
        self.assertIn(
            "plugins/demo/scripts/validate.py",
            flagged,
            "a file that merely shares the name must still be scanned",
        )


class TestPythonAssignmentExemption(unittest.TestCase):
    """`handler = pkg.internal.io` is code, but the same shape in a shell context is a
    real hostname. The exemption is therefore restricted to Python source, to spaced
    assignments (shell forbids the spaces), and to lines with no quote before the token."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="paysera-pyassign-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "plugins").mkdir()
        self._real_root = validate.ROOT
        validate.ROOT = self.root
        self.addCleanup(setattr, validate, "ROOT", self._real_root)

    def scan_as(self, filename, line):
        path = self.root / filename
        path.write_text(line + "\n", encoding="utf-8")
        errors = validate.scan_published_content()
        path.unlink()
        return errors

    def test_python_code_chain_is_exempt(self):
        for line in [
            "handler = mypkg.internal.io",
            "cfg = mypkg.internal.app",
            "client = pkg.internal.net",
        ]:
            with self.subTest(line=line):
                self.assertEqual(self.scan_as("thing.py", line), [])

    def test_the_same_line_in_markdown_is_checked(self):
        self.assertTrue(self.scan_as("doc.md", "handler = mypkg.internal.io"))

    def test_the_same_line_in_a_workflow_is_checked(self):
        self.assertTrue(self.scan_as("ci.yml", "handler = mypkg.internal.io"))

    def test_shell_assignment_in_python_is_still_checked(self):
        # No spaces around '=' is shell syntax, not a Python assignment.
        self.assertTrue(self.scan_as("thing.py", "HOST=wiki.internal"))

    def test_quoted_host_in_python_is_still_checked(self):
        for line in ['HOST = "wiki.internal"', "cmd = 'HOST=db.corp.local run'"]:
            with self.subTest(line=line):
                self.assertTrue(self.scan_as("thing.py", line), line)

    def test_shell_block_inside_a_markdown_file_is_checked(self):
        block = "```bash\nexport HOST=wiki.internal\ncurl -s $HOST\n```"
        self.assertTrue(self.scan_as("doc.md", block))


class TestPlausibleTld(unittest.TestCase):
    def test_accepts_real_endings(self):
        for label in ["com", "lt", "net", "io", "local", "internal"]:
            self.assertTrue(validate.is_plausible_tld(label), label)

    def test_rejects_code_identifiers(self):
        for label in ["helpers", "config", "utils", "handler"]:
            self.assertFalse(validate.is_plausible_tld(label), label)


class ValidatorFixture:
    """A throwaway repository for the manifest checks — a plain mixin, not a TestCase.

    Subclassing a TestCase to reuse a fixture makes unittest collect and re-run all of
    its test methods under the subclass's name too (the same defect fixed in
    test_create_payment.py).
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="paysera-manifest-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._real_root = validate.ROOT
        validate.ROOT = self.root
        self.addCleanup(setattr, validate, "ROOT", self._real_root)
        (self.root / ".claude-plugin").mkdir()
        self.plugin_dir = self.root / "plugins" / "demo"
        (self.plugin_dir / ".claude-plugin").mkdir(parents=True)
        (self.plugin_dir / "skills" / "demo").mkdir(parents=True)
        (self.plugin_dir / "skills" / "demo" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: A demo skill.\n---\n\nBody.\n", encoding="utf-8"
        )

    def write(self, marketplace_entry, plugin_manifest):
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "m", "plugins": [marketplace_entry]}), encoding="utf-8"
        )
        (self.plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(plugin_manifest), encoding="utf-8"
        )

    def entry(self, **over):
        base = {
            "name": "demo",
            "source": "./plugins/demo",
            "version": "1.0.0",
            "description": "A demo skill.",
            "tags": ["a", "b"],
        }
        base.update(over)
        return base

    def manifest(self, **over):
        base = {
            "name": "demo",
            "version": "1.0.0",
            "description": "A demo skill.",
            "keywords": ["a", "b"],
        }
        base.update(over)
        return base


class TestManifestValidation(ValidatorFixture, unittest.TestCase):
    """The manifest checks must report problems, never raise."""

    def test_a_consistent_pair_is_valid(self):
        self.write(self.entry(), self.manifest())
        self.assertEqual(validate.validate(), [])

    def test_missing_key_is_reported_not_raised(self):
        entry = self.entry()
        del entry["version"]
        self.write(entry, self.manifest())
        errors = validate.validate()  # must not raise KeyError
        self.assertTrue(any("missing version" in e for e in errors), errors)

    def test_version_drift_is_reported(self):
        self.write(self.entry(), self.manifest(version="9.9.9"))
        self.assertTrue(any("version" in e for e in validate.validate()))

    def test_tag_and_keyword_drift_is_reported(self):
        self.write(self.entry(), self.manifest(keywords=["a", "b", "c"]))
        self.assertTrue(any("keywords" in e for e in validate.validate()))

    def test_description_drift_is_reported(self):
        self.write(self.entry(), self.manifest(description="Something else."))
        self.assertTrue(any("description" in e for e in validate.validate()))

    def test_missing_plugin_manifest_is_reported(self):
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / ".claude-plugin" / "plugin.json").unlink()
        self.assertTrue(any("missing" in e for e in validate.validate()))

    def test_unlisted_plugin_directory_is_reported(self):
        self.write(self.entry(), self.manifest())
        (self.root / "plugins" / "orphan").mkdir()
        self.assertTrue(any("orphan" in e for e in validate.validate()))

    def test_skill_frontmatter_must_match_its_directory(self):
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / "skills" / "demo" / "SKILL.md").write_text(
            "---\nname: wrong\ndescription: x\n---\n", encoding="utf-8"
        )
        self.assertTrue(any("frontmatter name" in e for e in validate.validate()))


class TestMalformedInputIsReported(ValidatorFixture, unittest.TestCase):
    """A CI gate must name the file to fix.

    The missing-key path already did; three others still ended in a traceback, which
    tells a contributor nothing about which of their manifests is bad.
    """

    def test_a_syntax_error_in_the_catalogue_is_reported(self):
        self.write(self.entry(), self.manifest())
        (self.root / ".claude-plugin" / "marketplace.json").write_text(
            '{"plugins": [,]}', encoding="utf-8"
        )
        errors = validate.validate()  # must not raise JSONDecodeError
        self.assertTrue(any("marketplace.json" in e and "JSON" in e for e in errors), errors)

    def test_a_syntax_error_in_a_plugin_manifest_is_reported(self):
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "demo",}', encoding="utf-8"
        )
        errors = validate.validate()
        self.assertTrue(any("plugin.json" in e and "JSON" in e for e in errors), errors)

    def test_a_skill_without_frontmatter_is_reported(self):
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / "skills" / "demo" / "SKILL.md").write_text(
            "# No frontmatter here\n", encoding="utf-8"
        )
        errors = validate.validate()  # must not raise ValueError
        self.assertTrue(any("frontmatter" in e for e in errors), errors)

    def test_crlf_frontmatter_is_accepted(self):
        # The FRONTMATTER pattern anchors on "\n", which looks like it would reject a
        # CRLF file — it does not: Path.read_text() opens in universal-newline mode and
        # translates "\r\n" before the pattern ever sees it. Pinned so a later switch to
        # newline="" (or reading bytes) does not turn every Windows-authored SKILL.md
        # into a gate failure without anyone noticing.
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / "skills" / "demo" / "SKILL.md").write_bytes(
            b"---\r\nname: demo\r\ndescription: A demo skill.\r\n---\r\n\r\nBody.\r\n"
        )
        self.assertEqual(validate.validate(), [])

    def test_the_reported_error_names_the_file(self):
        self.write(self.entry(), self.manifest())
        (self.plugin_dir / "skills" / "demo" / "SKILL.md").write_text("nope\n", encoding="utf-8")
        errors = validate.validate()
        self.assertTrue(any("SKILL.md" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
