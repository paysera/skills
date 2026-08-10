# Contributing & publishing

This repository is **public**. Everything committed here is permanently visible, including
in the git history after a later deletion. Treat every commit as a publication.

## What may be published here

Only client-facing skills. A skill qualifies when all of the following hold:

- It uses **publicly documented Paysera APIs** (`api.paysera.com`, `auth-api.paysera.com`)
  that a client can call with their own credentials.
- It references **only publicly reachable Paysera hosts**. Any Paysera hostname that a
  client outside the company network cannot resolve does not belong here, and neither do
  links to internal source control, issue trackers, or wikis. `scripts/validate.py`
  enforces this with an allowlist of public hosts.
- It contains **no internal database, service, or infrastructure names**, and no references
  to internal-only tooling or skills.
- It contains **no internal issue-tracker keys or ticket references**, in files, commit
  messages, or pull request descriptions.
- It contains **no credentials, tokens, real account numbers, real IBANs, or personal data**
  — examples must use obvious placeholders (`EVP0000000000001`, `LT000000000000000000`).
- Any operation that moves money is **gated**: dry-run by default, explicit `--confirm`, and
  no `transfers:sign` scope.

Internal tooling stays in the internal repository. This repository is not a mirror of it —
skills are added here deliberately, one at a time.

## Adding or updating a skill

1. Add the plugin under `plugins/<name>/` with:
   - `.claude-plugin/plugin.json` — `name`, `version` (semver), `description`, `author`,
     `license`
   - `skills/<name>/SKILL.md` — the skill itself, with YAML frontmatter (`name`,
     `description`)
   - `commands/<name>.md` — optional slash command
   - `CHANGELOG.md`
2. Register it in `.claude-plugin/marketplace.json` under `plugins`, with a `version`
   matching `plugin.json`.
3. Bump the version in **both** `plugin.json` and `marketplace.json`, and add a
   `CHANGELOG.md` entry.
4. Run the checks below, open a pull request, and get a review from a code owner. The
   review must explicitly confirm the "What may be published here" checklist.

## Checks

```bash
python3 scripts/validate.py                                   # manifests + skill frontmatter
python3 -m pytest plugins/paysera-payments/skills/paysera-payments/scripts -q
```

Both also run in CI on every pull request.

## Releasing

Merging to `main` publishes: `claude plugin marketplace add paysera/skills` resolves the
default branch, so users get the change on their next `claude plugin marketplace update`.
Tag the merge commit `<plugin>-v<version>` (e.g. `paysera-payments-v1.3.2`) so each published
version is traceable.

## Issues

Public issues are enabled for installation and usage problems with the skills. Do **not**
post account numbers, transfer hashes, tokens, or any personal data in an issue. Security
reports go through [SECURITY.md](SECURITY.md), never a public issue.
