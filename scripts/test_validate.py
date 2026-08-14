"""Tests for the publication gate.

This script decides what is allowed to become public, so its heuristics need the same
protection as the skill code. The table below is the specification: each sample is a line
that either must fail CI or must not.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402

# --- leak check -----------------------------------------------------------------------
# This module gets its own temporary directory and must leave it empty. Deliberately a
# local copy of what plugins/*/scripts/_testsupport.py does, not an import of it: a plugin
# is installed on its own, so a plugin file must never depend on a repository file, and
# the dependency in this direction would be just as wrong.
_TEMPBOX = {}


def setUpModule():
    # Made before tempfile.tempdir moves, so it does not land inside the box.
    _TEMPBOX["home"] = home = tempfile.mkdtemp(prefix="paysera-validate-testhome-")
    _TEMPBOX["path"] = box = tempfile.mkdtemp(prefix="paysera-validate-tempbox-")
    _TEMPBOX["tempdir"] = tempfile.tempdir
    _TEMPBOX["env"] = {k: os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP", "HOME")}
    # ALL THREE halves, exactly as the plugin copy does it: `tempfile.tempdir` for this
    # process, TMPDIR/TEMP/TMP for any subprocess, which inherits them, and HOME — which
    # is a different guarantee. The box catches what a module WRITES; HOME limits what it
    # REACHES, and the box cannot see a change made outside itself. This module starts no
    # subprocess and reads no HOME today — which is exactly why the halves went missing
    # before, and why leaving one out makes the first code to need it escape silently.
    tempfile.tempdir = box
    os.environ.update(TMPDIR=box, TEMP=box, TMP=box, HOME=home)


def tearDownModule():
    if "path" not in _TEMPBOX:
        raise AssertionError("setUpModule did not run — the leak check is disarmed")
    box = _TEMPBOX.pop("path")
    tempfile.tempdir = _TEMPBOX.pop("tempdir")
    for key, value in _TEMPBOX.pop("env").items():
        os.environ.pop(key, None) if value is None else os.environ.update({key: value})
    shutil.rmtree(_TEMPBOX.pop("home"), ignore_errors=True)
    left = sorted(Path(box).iterdir())
    shutil.rmtree(box, ignore_errors=True)
    if left:
        raise AssertionError(
            f"{len(left)} temporary item(s) left behind by this module — every mkdtemp() "
            f"needs a matching cleanup: {[p.name for p in left[:5]]}"
        )

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
    # --- a URL is a URL under any scheme --------------------------------------------
    # These all used to pass: the scheme was not http/https, so the host was not "in a
    # URL", and the file-path exemption then cleared it because the text before it ends
    # in `/`. A clone command for an internal repository is an ordinary thing to write.
    ("clone ssh://wiki.internal/repo.git", True, "ssh scheme"),
    ("git clone git://wiki.internal/x.git", True, "git scheme"),
    ("see //wiki.internal/page", True, "protocol-relative, no scheme at all"),
    ("jdbc:postgresql://db.corp.local:5432/main", True, "compound jdbc scheme"),
    ("ssh://config.test/x", True, "reserved TLD counts under a non-http scheme too"),
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
        # Named once, not twice: the caller supplies the path, so the exception must not
        # carry a file name of its own.
        message = next(e for e in errors if "frontmatter" in e)
        self.assertEqual(message.count("SKILL.md"), 1, message)

    def test_every_skill_error_names_a_repository_relative_path(self):
        # An absolute path here is the CI runner's own checkout directory, which the
        # reader has never seen. Every other message in the gate is relative.
        self.write(self.entry(), self.manifest())
        skill = self.plugin_dir / "skills" / "demo" / "SKILL.md"
        for body in (
            "# No frontmatter here\n",              # the ValueError path
            "---\nname: wrong\ndescription: x\n---\n",  # name does not match
            "---\nname: demo\n---\n",               # no description
        ):
            with self.subTest(body=body.splitlines()[0]):
                skill.write_text(body, encoding="utf-8")
                errors = [e for e in validate.validate() if "SKILL.md" in e]
                self.assertTrue(errors)
                for e in errors:
                    self.assertTrue(
                        e.startswith("plugins/demo/skills/demo/SKILL.md:"),
                        f"not a repository-relative path: {e}",
                    )
                    self.assertNotIn(str(self.root), e)

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


class TestTheLeakCheckItself(unittest.TestCase):
    """tearDownModule is where the check runs, and no test can observe it from inside the
    module. Call it directly against a planted leak, or "it never fires" and "nothing
    leaked" look identical."""

    def test_a_leftover_directory_fails_the_module(self):
        box = tempfile.mkdtemp(prefix="paysera-leakprobe-")
        self.addCleanup(shutil.rmtree, box, ignore_errors=True)
        (Path(box) / "left-behind").mkdir()
        saved = dict(_TEMPBOX)
        # The value it had, not the box: restoring it to the box would repair a broken
        # setUpModule for the next test rather than leaving it broken to be caught.
        prior_tempdir = tempfile.tempdir
        # env={} for the same reason: the real saved environment must not be restored by
        # a probe, or TMPDIR goes back to what it was before setUpModule and the rest of
        # the module runs outside its own box.
        probe_home = tempfile.mkdtemp(prefix="paysera-leakprobe-home-")
        self.addCleanup(shutil.rmtree, probe_home, ignore_errors=True)
        # clear() then set every key, NOT update(): update would leave the real `home`
        # entry in place and the teardown below would delete the module's own sandbox.
        _TEMPBOX.clear()
        _TEMPBOX.update(path=box, tempdir=prior_tempdir, env={}, home=probe_home)
        try:
            with self.assertRaises(AssertionError) as raised:
                tearDownModule()
        finally:
            _TEMPBOX.clear()
            _TEMPBOX.update(saved)
            tempfile.tempdir = prior_tempdir
        self.assertIn("left-behind", str(raised.exception))

    def test_this_module_writes_inside_its_own_box(self):
        box = _TEMPBOX.get("path")
        self.assertIsNotNone(box, "setUpModule did not run")
        # Both halves. The environment one has no subprocess to protect in this module
        # today; asserting it is what stops the first one added from escaping the box
        # while the module still reports clean.
        self.assertEqual(tempfile.tempdir, box, "this process writes outside the box")
        for key in ("TMPDIR", "TEMP", "TMP"):
            self.assertEqual(os.environ.get(key), box, key)
        # HOME is a sibling of the box, not the box itself: the teardown requires the box
        # to be EMPTY, and a home directory anything writes to would fail that check for
        # the wrong reason. What matters is that it is not the developer's real one.
        self.assertEqual(os.environ.get("HOME"), _TEMPBOX.get("home"))
        # And that it is genuinely not the one the process started with.
        self.assertNotEqual(os.environ.get("HOME"), _TEMPBOX["env"].get("HOME"))
        made = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, made, ignore_errors=True)
        self.assertTrue(made.startswith(box))

    def test_a_full_cycle_leaves_the_process_as_it_found_it(self):
        # setUpModule/tearDownModule mutate process-wide state. Under pytest all three
        # test modules share one process, so a teardown that does not put TMPDIR back
        # hands the next module a value pointing at a directory that no longer exists.
        keys = ("TMPDIR", "TEMP", "TMP", "HOME")
        before = {k: os.environ.get(k) for k in keys}
        saved, prior_tempdir = dict(_TEMPBOX), tempfile.tempdir
        try:
            setUpModule()
            self.assertNotEqual(os.environ["TMPDIR"], before["TMPDIR"])
            tearDownModule()
            self.assertEqual({k: os.environ.get(k) for k in keys}, before)
            self.assertEqual(tempfile.tempdir, prior_tempdir)
        finally:
            _TEMPBOX.clear()
            _TEMPBOX.update(saved)
            tempfile.tempdir = prior_tempdir
            for key, value in before.items():
                os.environ.pop(key, None) if value is None else os.environ.update({key: value})

    def test_a_disarmed_check_is_not_a_pass(self):
        # tearDownModule with no setUpModule must say so, not return quietly: silence
        # here is indistinguishable from a clean run.
        saved = dict(_TEMPBOX)
        _TEMPBOX.clear()
        try:
            with self.assertRaises(AssertionError) as raised:
                tearDownModule()
        finally:
            _TEMPBOX.update(saved)
        self.assertIn("disarmed", str(raised.exception))


class TestNothingPublishableCanHideInASkippedDirectory(unittest.TestCase):
    """Every name in SKIP_DIRS must also be in .gitignore.

    SKIP_DIRS exists so that a local .venv or node_modules does not make the gate slow
    and red on somebody else's code. But a directory the gate does not read is a blind
    spot in a check whose whole job is to stop internal content going public. Pairing it
    with .gitignore closes that: an untracked directory is never published, so skipping
    it can never hide anything. Adding a name to one list without the other fails here.
    """

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _ignored_names(self):
        text = (self.REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        names = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.add(line.rstrip("/").lstrip("/"))
        return names

    def test_every_skipped_directory_is_git_ignored(self):
        ignored = self._ignored_names()
        # .git is the exception, and the only one: git cannot ignore its own directory.
        unpaired = {d for d in validate.SKIP_DIRS if d != ".git"} - ignored
        self.assertEqual(
            unpaired,
            set(),
            f"in SKIP_DIRS but not in .gitignore, so published content could hide "
            f"there unseen: {sorted(unpaired)}",
        )

    def test_every_skipped_file_is_git_ignored(self):
        unpaired = set(validate.SKIP_FILES) - self._ignored_names()
        self.assertEqual(
            unpaired,
            set(),
            f"in SKIP_FILES but not in .gitignore, so it could be committed and then "
            f"published without ever being scanned: {sorted(unpaired)}",
        )

    def test_the_pairing_check_can_fail(self):
        # Without this, a .gitignore that happened to list everything would make the two
        # tests above pass for a reason unrelated to what they claim to check.
        with mock.patch.object(validate, "SKIP_DIRS", validate.SKIP_DIRS | {"secrets"}):
            with self.assertRaises(AssertionError):
                self.test_every_skipped_directory_is_git_ignored()
        with mock.patch.object(validate, "SKIP_FILES", validate.SKIP_FILES | {"notes.md"}):
            with self.assertRaises(AssertionError):
                self.test_every_skipped_file_is_git_ignored()

    def test_the_contributing_guide_names_both_skip_mechanisms(self):
        # CONTRIBUTING.md's "what the check does and does not cover" section is the list a
        # reviewer is told to confirm by hand. A blind spot the list does not name cannot
        # be checked, and until 1.8.8 it described SKIP_DIRS only — while also claiming
        # the gate scanned *every* file in the repository.
        doc = (self.REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        collapsed = " ".join(doc.split())
        self.assertIn("SKIP_DIRS", collapsed)
        self.assertIn("SKIP_FILES", collapsed)
        self.assertIn("repository root", collapsed, "the root-only restriction is the "
                      "part that makes the file skip safe, so it has to be stated")
        self.assertNotIn(
            "scans **every** `.md`", collapsed,
            "the guide is claiming coverage the gate no longer has",
        )

    def test_a_skipped_file_is_skipped_at_the_root_only(self):
        # A REVIEW.md inside a plugin ships with `claude plugin install`, so it is
        # published content and must still be scanned. Only the root copy is a working
        # note. Skipping by bare name everywhere would be a hole with a plausible name.
        root = Path(tempfile.mkdtemp(prefix="paysera-skipfile-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "REVIEW.md").write_text("scratch\n", encoding="utf-8")
        (root / "plugins" / "p").mkdir(parents=True)
        (root / "plugins" / "p" / "REVIEW.md").write_text("shipped\n", encoding="utf-8")
        with mock.patch.object(validate, "ROOT", root):
            found = {str(p.relative_to(root)) for p in validate.published_files()}
        self.assertEqual(found, {os.path.join("plugins", "p", "REVIEW.md")})

    def test_a_skipped_directory_is_actually_skipped(self):
        # And that the skip works on a nested path, not only a top-level one.
        root = Path(tempfile.mkdtemp(prefix="paysera-skipdir-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / ".venv" / "lib").mkdir(parents=True)
        (root / ".venv" / "lib" / "vendored.py").write_text("x = 1\n", encoding="utf-8")
        (root / "kept.md").write_text("hello\n", encoding="utf-8")
        with mock.patch.object(validate, "ROOT", root):
            found = {p.name for p in validate.published_files()}
        self.assertEqual(found, {"kept.md"})


if __name__ == "__main__":
    unittest.main()
