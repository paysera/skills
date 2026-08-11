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
    # --- dotted code chains whose last label happens to be a plausible TLD -----------
    # Python module names like io/app/net/dev are all real TLDs, so the last-label rule
    # alone is not enough; an unquoted right-hand side is code.
    ("handler = mypkg.internal.io", False, "assignment, module name that is also a TLD"),
    ("cfg = mypkg.internal.app", False, "assignment, module name that is also a TLD"),
    ("client = pkg.internal.net", False, "assignment, module name that is also a TLD"),
    ("value = pkg.internal.helpers", False, "assignment, ordinary module name"),
    # ...but a quoted host on the right-hand side is still a host.
    ('HOST = "db.corp.local"', True, "quoted internal host in an assignment"),
    ("host: db.corp.local", True, "YAML value is a host, not an assignment"),
    # `.test` is a reserved TLD and a common file suffix; only a URL settles it.
    ("config.test holds the fixture", False, "bare fixture filename"),
    ("`config.test`", False, "bare fixture filename in backticks"),
    ("https://config.test/path", True, "reserved TLD used as a real host"),
    # Both spellings on one line: "in a URL" is decided per occurrence, not per line.
    ("`config.test` is a file; https://config.test/x is a host", True, "URL wins on its own"),
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


class TestPlausibleTld(unittest.TestCase):
    def test_accepts_real_endings(self):
        for label in ["com", "lt", "net", "io", "local", "internal"]:
            self.assertTrue(validate.is_plausible_tld(label), label)

    def test_rejects_code_identifiers(self):
        for label in ["helpers", "config", "utils", "handler"]:
            self.assertFalse(validate.is_plausible_tld(label), label)


class TestManifestValidation(unittest.TestCase):
    """The manifest checks must report problems, never raise."""

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


if __name__ == "__main__":
    unittest.main()
