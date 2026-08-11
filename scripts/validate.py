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
# match HOSTNAMES only (never prose), so a sentence containing the word "internal" is
# fine while a link to `wiki.internal` is not.
# "intranet"/"internal" anywhere in the name (paysera.intranet.lt, wiki.internal), ...
INTERNAL_HOST_LABEL = re.compile(r"(?:^|\.)(?:intranet|internal)(?:\.|$)", re.IGNORECASE)
# ... and these only as the final label, where they are non-routable by convention.
INTERNAL_HOST_TLD = re.compile(r"\.(?:local|lan|corp|localdomain|home|test|invalid)$", re.IGNORECASE)
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
    for entry in marketplace["plugins"]:
        name = entry["name"]
        listed.add(name)
        source = ROOT / entry["source"]
        if not source.is_dir():
            errors.append(f"{name}: source {entry['source']} does not exist")
            continue

        manifest = json.loads((source / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        if manifest["name"] != name:
            errors.append(f"{name}: plugin.json name is {manifest['name']!r}")
        if manifest["version"] != entry["version"]:
            errors.append(
                f"{name}: version {manifest['version']} in plugin.json, "
                f"{entry['version']} in marketplace.json"
            )
        if manifest["description"] != entry["description"]:
            errors.append(f"{name}: description differs between plugin.json and marketplace.json")

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


def scan_published_content():
    """Flag internal references in anything this repository publishes.

    This file is itself exempt: it has to name the patterns it looks for.
    """
    errors = []
    for path in published_files():
        if path.resolve() == Path(__file__).resolve():
            continue
        rel = path.relative_to(ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for host in HOSTNAME.findall(line):
                host = host.lower()
                if PAYSERA_HOST.search(host) and host not in PUBLIC_PAYSERA_HOSTS:
                    errors.append(
                        f"{rel}:{number}: '{host}' is not a public Paysera host — "
                        f"published content may only reference "
                        f"{', '.join(sorted(PUBLIC_PAYSERA_HOSTS))}"
                    )
                elif INTERNAL_HOST_LABEL.search(host) or INTERNAL_HOST_TLD.search(host):
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
