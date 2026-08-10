#!/usr/bin/env python3
"""Validate the marketplace catalogue against the plugins it lists."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Published skills may only reference Paysera hosts a client can actually reach.
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

    for path in sorted(plugins_root.rglob("*")) if plugins_root.is_dir() else []:
        if path.is_file() and path.suffix in {".md", ".py", ".json"}:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for host in HOSTNAME.findall(line):
                    host = host.lower()
                    if PAYSERA_HOST.search(host) and host not in PUBLIC_PAYSERA_HOSTS:
                        errors.append(
                            f"{path.relative_to(ROOT)}:{number}: '{host}' is not a public "
                            f"Paysera host — published skills may only reference "
                            f"{', '.join(sorted(PUBLIC_PAYSERA_HOSTS))}"
                        )

    return errors


if __name__ == "__main__":
    found = validate()
    for error in found:
        print(f"ERROR {error}", file=sys.stderr)
    if found:
        sys.exit(1)
    print("marketplace.json and all plugins are valid")
