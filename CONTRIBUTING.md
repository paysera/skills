# Contributing & publishing

This repository is **public**. Everything committed here is permanently visible, including
in the git history after a later deletion. Treat every commit as a publication.

## What may be published here

Only client-facing skills. A skill qualifies when all of the following hold:

- It uses **publicly documented Paysera APIs** (`api.paysera.com`, `auth-api.paysera.com`)
  that a client can call with their own credentials.
- It references **only publicly reachable Paysera hosts**. Any Paysera hostname that a
  client outside the company network cannot resolve does not belong here, and neither do
  links to internal source control, issue trackers, or wikis.
- It contains **no internal database, service, or infrastructure names**, and no references
  to internal-only tooling or skills.
- It contains **no internal issue-tracker keys or ticket references**, in files, commit
  messages, or pull request descriptions.
- It contains **no credentials, tokens, real account numbers, real IBANs, or personal data**
  — examples must use obvious placeholders (`EVP0000000000001`, `LT000000000000000000`).
- Any operation that moves money is **gated**: dry-run by default, explicit `--confirm`, and
  no `transfers:sign` scope.

### What the automated check does and does not cover

`scripts/validate.py` scans **every** `.md`, `.py`, `.json` and `.yml` file in the
repository (not just `plugins/`) and fails on:

- a `paysera.*` hostname that is not on its allowlist of public hosts;
- a hostname containing `intranet`/`internal`, or ending in `.local`, `.lan`, `.corp`,
  `.localdomain`, `.home`, `.test`, `.invalid`;
- a URL that looks like an issue tracker or forge link (`/browse/KEY-123`, `/jira/`,
  `/confluence/`, `/-/merge_requests/`).

It is a **backstop, not the review**. It cannot see an internal service or database name
written in prose, an internal tool referred to by name, an internal wiki link on a domain
it does not recognise, a real account number that looks like a placeholder, or a ticket key
in a commit message. A human reviewer must check those, and must state in the pull request
that they did.

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

The same two commands CI runs on every pull request:

```bash
python3 scripts/validate.py     # manifests, skill frontmatter, published-content scan
python3 -m pytest plugins -q    # exit code 5 (no tests collected) is tolerated for now
```

> **The payment scripts currently have no automated tests.** `pytest` collects nothing, so
> it exits 5 and CI treats that as a pass. Do not read a green pipeline as "the behaviour
> is tested" — it means the manifests are consistent and nothing internal leaked. New
> skills should ship with tests, and tests for the existing scripts are welcome.

## Releasing

Merging to `main` publishes: `claude plugin marketplace add paysera/skills` resolves the
default branch, so users get the change on their next `claude plugin marketplace update`.
Tag the merge commit `<plugin>-v<version>` (e.g. `paysera-payments-v1.3.2`) so each published
version is traceable.

## Issues

Public issues are enabled for installation and usage problems with the skills. Do **not**
post account numbers, transfer hashes, tokens, or any personal data in an issue. Security
reports go through [SECURITY.md](SECURITY.md), never a public issue.
