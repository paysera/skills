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
# A hostname sits in an explicit URL when the text right before it is a scheme.
URL_PREFIX = re.compile(r"https?://\Z", re.IGNORECASE)
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
INTERNAL_HOST_TLD = re.compile(r"\.(?:local|lan|corp|localdomain|home|test|invalid)$", re.IGNORECASE)
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


def read_frontmatter(skill_file: Path) -> dict:
    match = FRONTMATTER.match(skill_file.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{skill_file}: missing YAML frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        if line.startswith(" ") or ": " not in line:
            continue
        key, value = line.split(": ", 1)
        fields[key] = value
    return fields


def validate() -> list:
    errors = []
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))

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
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            fields = read_frontmatter(skill_file)
            if fields.get("name") != skill_file.parent.name:
                errors.append(f"{skill_file}: frontmatter name does not match its directory")
            if not fields.get("description"):
                errors.append(f"{skill_file}: frontmatter has no description")

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


def looks_like_prose(line, match):
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
    # The right-hand side of an assignment, unquoted: `handler = pkg.internal.io` is code.
    # A quoted host — HOST = "db.corp.local" — is not exempt, because `before` then ends
    # with the quote character rather than the assignment.
    # (`=` only, never `:` — a YAML value like `host: db.corp.local` IS a hostname.)
    if re.search(r"=\s*\Z", before) and not before.rstrip().endswith(("'", '"', "`")):
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
                    in_url or not looks_like_prose(line, match)
                ):
                    # These markers are ordinary words in code and prose
                    # ("pkg.internal.helpers", "config.test"), so they are only flagged
                    # where the token is really being used as a host.
                    errors.append(
                        f"{rel}:{number}: '{host}' looks like an internal-only hostname"
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
