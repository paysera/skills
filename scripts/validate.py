#!/usr/bin/env python3
"""Validate the marketplace catalogue against the plugins it lists."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Everything published here is scanned, not just plugins/ — the marketplace catalogue
# duplicates each skill's description verbatim, and the README is as public as the rest.
PUBLISHED_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}

# Published content may only reference Paysera hosts a client can actually reach.
# This is an allowlist on purpose: naming the hosts that are *not* public would
# put them in this repository, which is exactly what the check exists to prevent.
HOSTNAME = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
# A hostname sits in an explicit URL when the text right before it is `//` — with ANY
# scheme in front of it, or none at all. Restricting this to http/https missed the most
# ordinary way to write an internal host in a contributing guide or a workflow `run:`
# step: `git clone ssh://host/repo.git`. Those fell through to looks_like_prose(), whose
# path rule then cleared them, because the text before the host ends in `/`.
URL_PREFIX = re.compile(r"(?:[a-z][a-z0-9+.-]*:)?//\Z", re.IGNORECASE)
PAYSERA_HOST = re.compile(r"(?:^|\.)paysera\.[a-z]{2,}$", re.IGNORECASE)
PUBLIC_PAYSERA_HOSTS = frozenset(
    {
        "paysera.com",
        "www.paysera.com",
        "bank.paysera.com",
        "api.paysera.com",
        "auth-api.paysera.com",
        "developers.paysera.com",
    }
)

# An allowlist keyed on "paysera.*" cannot see an internal host on some other domain, so
# these patterns cover the shapes an internal link takes regardless of its domain. They
# are applied only where a dotted token is really a hostname (see looks_like_prose), so
# `from pkg.internal.helpers import x`, `internal.md` and `app/config.test` do not trip
# them, while `https://wiki.internal/page` does.
# "intranet"/"internal" anywhere in the name (paysera.intranet.lt, wiki.internal), ...
INTERNAL_HOST_LABEL = re.compile(r"(?:^|\.)(?:intranet|internal)(?:\.|$)", re.IGNORECASE)
# ... and these only as the final label, where they are non-routable by convention.
INTERNAL_HOST_TLD = re.compile(
    r"\.(?:local|localhost|lan|corp|localdomain|home|test|invalid)$", re.IGNORECASE
)
# HOSTNAME cannot match an IP address — its last label must be letters — so a private
# address was invisible to every rule above. Naming an internal dashboard, database or CI
# server by IP is at least as common as naming it by hostname, so these are matched
# separately: RFC 1918 (10/8, 172.16/12, 192.168/16), loopback (127/8) and link-local
# (169.254/16). Public IPs are NOT flagged — a published example may legitimately use one.
PRIVATE_IPV4 = re.compile(
    r"(?<![\w.])(?:"
    r"10(?:\.\d{1,3}){3}"
    r"|127(?:\.\d{1,3}){3}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r"|169\.254(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r")(?![\w.])"
)
# `.test` and `.invalid` are reserved TLDs, but they are also ordinary file suffixes
# ("config.test"), so outside a URL they are treated as filenames. The rest have no such
# collision and are flagged wherever they appear.
URL_ONLY_TLD = re.compile(r"\.(?:test|invalid)$", re.IGNORECASE)
COMMON_GTLDS = frozenset(
    {
        "com", "net", "org", "info", "biz", "edu", "gov", "mil", "int",
        "app", "dev", "io", "ai", "cloud", "online", "site", "tech", "xyz",
    }
)
# Links into an issue tracker or source forge: the host allowlist would miss these when
# they live on a non-Paysera domain.
TRACKER_URL = re.compile(
    r"https?://[^\s<>\"')]*?(?:/browse/[A-Z][A-Z0-9]+-\d+|/jira/|/confluence/|/-/merge_requests/)",
    re.IGNORECASE,
)

# A ticket key needs no URL around it to be a leak — "see ABC-1234 for the rationale" in a
# comment or a commit message says just as much about internal work as a link does, and
# CONTRIBUTING.md forbids ticket references outright, not merely tracker links.
TICKET_KEY = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{1,9})-(\d{1,6})(?![\w-])")
# Prefixes that are not project keys. Standards and formats are spelled exactly like a
# ticket key, and so are this repository's own placeholder families. Anything NOT listed
# here is treated as a possible ticket, so the gate fails closed: a contributor who trips
# it on a genuine standard adds the prefix here, with the same scrutiny as a new sample.
NON_TICKET_PREFIXES = frozenset(
    {
        # standards, formats and algorithms
        "ISO", "UTF", "RFC", "PEP", "CVE", "CWE", "SHA", "MD", "AES", "RSA", "TLS",
        "SSL", "HTTP", "HTTPS", "IPV", "ASCII", "UTC", "GMT", "EET", "EEST", "SEPA",
        "SWIFT", "BIC", "IBAN", "EUR", "USD", "GBP", "PSD", "API", "SPDX",
        # placeholders used in this repository's own examples
        "INV", "KEY", "EX", "EVP", "LT",
    }
)


def load_json(path: Path, errors: list):
    """Parse a manifest, reporting a syntax error the way every other failure here is
    reported. Returns None when it could not be parsed.

    Same rule as the missing-key check below: this runs as a CI gate, and a
    JSONDecodeError traceback tells a contributor nothing about which file to fix.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f"{path.relative_to(ROOT)}: cannot be read as JSON — {e}")
        return None


def read_frontmatter(skill_file: Path) -> dict:
    match = FRONTMATTER.match(skill_file.read_text(encoding="utf-8"))
    if match is None:
        # CRLF line ends are the usual cause: the pattern anchors on "\n".
        # No file name here: every caller prefixes the path it wants to show.
        raise ValueError(
            "no YAML frontmatter found — the file must start with a '---' block "
            "(LF line ends)"
        )
    fields = {}
    for line in match.group(1).splitlines():
        if line.startswith(" ") or ": " not in line:
            continue
        key, value = line.split(": ", 1)
        fields[key] = value
    return fields


def validate() -> list:
    errors = []
    marketplace = load_json(ROOT / ".claude-plugin/marketplace.json", errors)
    if marketplace is None:
        # Nothing else can be checked against a catalogue that will not parse, but the
        # published-content scan is independent of it and still runs.
        return errors + scan_published_content()

    listed = set()
    for index, entry in enumerate(marketplace.get("plugins", [])):
        # A missing key is a manifest error to report, not a traceback: this runs as a CI
        # gate, and a KeyError tells a contributor nothing useful.
        missing = [k for k in ("name", "source", "version", "description") if k not in entry]
        if missing:
            errors.append(
                f"marketplace.json plugins[{index}]: missing {', '.join(missing)}"
            )
            continue
        name = entry["name"]
        listed.add(name)
        source = ROOT / entry["source"]
        if not source.is_dir():
            errors.append(f"{name}: source {entry['source']} does not exist")
            continue

        manifest_path = source / ".claude-plugin/plugin.json"
        if not manifest_path.is_file():
            errors.append(f"{name}: {manifest_path.relative_to(ROOT)} is missing")
            continue
        manifest = load_json(manifest_path, errors)
        if manifest is None:
            continue
        manifest_missing = [k for k in ("name", "version", "description") if k not in manifest]
        if manifest_missing:
            errors.append(f"{name}: plugin.json missing {', '.join(manifest_missing)}")
            continue

        if manifest["name"] != name:
            errors.append(f"{name}: plugin.json name is {manifest['name']!r}")
        if manifest["version"] != entry["version"]:
            errors.append(
                f"{name}: version {manifest['version']} in plugin.json, "
                f"{entry['version']} in marketplace.json"
            )
        if manifest["description"] != entry["description"]:
            errors.append(f"{name}: description differs between plugin.json and marketplace.json")
        # Discovery metadata is duplicated across the two manifests; nothing else keeps
        # them in step, so they drift silently.
        if sorted(manifest.get("keywords", [])) != sorted(entry.get("tags", [])):
            errors.append(
                f"{name}: plugin.json keywords {sorted(manifest.get('keywords', []))} "
                f"differ from marketplace.json tags {sorted(entry.get('tags', []))}"
            )

        skills = sorted((source / "skills").glob("*/SKILL.md"))
        if not skills:
            errors.append(f"{name}: no skills/*/SKILL.md found")
        for skill_file in skills:
            try:
                fields = read_frontmatter(skill_file)
            except (OSError, ValueError) as e:
                errors.append(f"{skill_file.relative_to(ROOT)}: {e}")
                continue
            # Relative, like every other message: an absolute path here is the CI
            # runner's own checkout directory, which means nothing to the reader.
            rel_skill = skill_file.relative_to(ROOT)
            if fields.get("name") != skill_file.parent.name:
                errors.append(f"{rel_skill}: frontmatter name does not match its directory")
            if not fields.get("description"):
                errors.append(f"{rel_skill}: frontmatter has no description")

    plugins_root = ROOT / "plugins"
    for plugin_dir in sorted(plugins_root.iterdir()) if plugins_root.is_dir() else []:
        if plugin_dir.is_dir() and plugin_dir.name not in listed:
            errors.append(f"{plugin_dir.name}: present in plugins/ but not in marketplace.json")

    errors.extend(scan_published_content())
    return errors


def published_files():
    """Every file that goes public — the whole repository, not only plugins/."""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in PUBLISHED_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def looks_like_prose(line, match, is_python=False):
    """True when a dotted token is code or a filename rather than a hostname.

    The host patterns below use words that occur naturally in source and documentation, so
    without this a contributor gets a CI failure that names the wrong cause.
    """
    token = match.group(0)
    before = line[: match.start()]
    after = line[match.end() :]
    # NOTE: there is deliberately no "wrapped in backticks" rule. Backticks are how this
    # repository writes hostnames (`bank.paysera.com`, `api.paysera.com`), so exempting
    # them would skip the most likely spelling of a leaked internal host.
    #
    # A module path in an import statement.
    if re.match(r"\s*(?:from|import)\s", line):
        return True
    # A filename: the final label is a known extension.
    if re.search(r"\.(?:md|py|json|ya?ml|txt|cfg|ini|toml|sh|lock)\Z", token, re.I):
        return True
    # A filesystem path, or an attribute chain hanging off something.
    if before.endswith(("/", "\\")) or re.search(r"[\w)\]]\Z", before):
        return True
    # The right-hand side of a PYTHON assignment: `handler = pkg.internal.io` is code.
    # Deliberately narrow, because a shell assignment is a realistic way to write a real
    # internal host (`HOST=wiki.example` in a bash block or a workflow `run:` step):
    #   * Python files only — shell assignments do not appear in .py source;
    #   * whitespace on BOTH sides of `=`, which shell assignment syntax forbids;
    #   * no quote earlier on the line, so a host inside a string stays checked.
    # `=` only, never `:` — a YAML value like `host: db.corp.local` IS a hostname.
    if (
        is_python
        and re.search(r"\s=\s+\Z", before)
        and not re.search(r"['\"`]", before)
    ):
        return True
    # A dotted name continuing into a call or subscript.
    if re.match(r"\s*[(\[]", after):
        return True
    # A dotted name whose last label cannot be a TLD is a code path, not a host:
    # `pkg.internal.helpers` ends in "helpers"; `wiki.internal` and `paysera.intranet.lt`
    # end in something a host can really end with.
    if not is_plausible_tld(token.rsplit(".", 1)[-1]):
        return True
    return False


def is_internal_host(host, in_url):
    """True if `host` names something only reachable inside a private network.

    KNOWN LIMIT: an `internal`/`intranet` label under an uncommon gTLD (one outside
    COMMON_GTLDS and not two letters) is not detected, because at that point a hostname
    and a dotted code path cannot be told apart — see looks_like_prose(). The realistic
    internal suffixes are covered; this check is a backstop, not the review.
    """
    if INTERNAL_HOST_LABEL.search(host):
        return True
    if URL_ONLY_TLD.search(host):
        return bool(in_url)
    return bool(INTERNAL_HOST_TLD.search(host))


def is_plausible_tld(label):
    """True if `label` could end a real hostname."""
    label = label.lower()
    if INTERNAL_HOST_TLD.search("." + label) or label in {"internal", "intranet"}:
        return True
    # Every ccTLD is two letters; beyond those, a short allowlist of the gTLDs that
    # plausibly appear here. An unknown long label means "not a hostname".
    return len(label) == 2 or label in COMMON_GTLDS


# The checker and its tests are exempt: both have to name the patterns they look for, and
# the test file's sample table deliberately contains internal hostnames. Nothing else in
# the repository is exempt — in particular there is no way for ordinary content to opt out.
SELF_EXEMPT = {"validate.py", "test_validate.py"}


def scan_published_content():
    """Flag internal references in anything this repository publishes."""
    errors = []
    here = Path(__file__).resolve().parent
    for path in published_files():
        if path.name in SELF_EXEMPT and path.resolve().parent == here:
            continue
        rel = path.relative_to(ROOT)
        is_python = path.suffix.lower() == ".py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in HOSTNAME.finditer(line):
                host = match.group(0).lower()
                # Per OCCURRENCE, not per line: the same name can appear twice on one
                # line, once as a URL host and once as a filename, and they are different.
                in_url = bool(URL_PREFIX.search(line[: match.start()]))
                if PAYSERA_HOST.search(host) and host not in PUBLIC_PAYSERA_HOSTS:
                    # A Paysera hostname is unambiguous whether or not it sits in a URL.
                    errors.append(
                        f"{rel}:{number}: '{host}' is not a public Paysera host — "
                        f"published content may only reference "
                        f"{', '.join(sorted(PUBLIC_PAYSERA_HOSTS))}"
                    )
                elif is_internal_host(host, in_url) and (
                    in_url or not looks_like_prose(line, match, is_python)
                ):
                    # These markers are ordinary words in code and prose
                    # ("pkg.internal.helpers", "config.test"), so they are only flagged
                    # where the token is really being used as a host.
                    errors.append(
                        f"{rel}:{number}: '{host}' looks like an internal-only hostname"
                    )
            for match in PRIVATE_IPV4.finditer(line):
                # No prose exemption: unlike `internal`/`test`, a dotted quad in a
                # published file is not a word that occurs naturally in code, and the
                # numbers here are reserved — 10.0.0.5 is never a version or an ordinary
                # decimal. A version string like 10.20.30.40 would be a false positive,
                # which is the right way round for a backstop.
                errors.append(
                    f"{rel}:{number}: '{match.group(0)}' is a private/loopback IP "
                    f"address — it names a host only reachable inside a network"
                )
            for match in TICKET_KEY.finditer(line):
                if match.group(1).upper() in NON_TICKET_PREFIXES:
                    continue
                errors.append(
                    f"{rel}:{number}: '{match.group(0)}' looks like an issue-tracker key "
                    f"— published content must not reference internal tickets. If this is "
                    f"a standard or a placeholder, add its prefix to NON_TICKET_PREFIXES"
                )
            for url in TRACKER_URL.findall(line):
                errors.append(
                    f"{rel}:{number}: '{url}' links into an issue tracker or forge — "
                    f"published content must not reference internal tickets"
                )
    return errors


if __name__ == "__main__":
    found = validate()
    for error in found:
        print(f"ERROR {error}", file=sys.stderr)
    if found:
        sys.exit(1)
    print("marketplace.json and all plugins are valid")
