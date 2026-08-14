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

`scripts/validate.py` scans every `.md`, `.py`, `.json`, `.yml` and `.yaml` file in the
repository (not just `plugins/`) **except** the names in its `SKIP_DIRS` and `SKIP_FILES`
lists — see "What is not scanned" below, and treat those as blind spots. It fails on:

- a `paysera.*` hostname that is not on its allowlist of public hosts, wherever it appears;
- a hostname containing `intranet`/`internal`, or ending in `.local`, `.lan`, `.corp`,
  `.localdomain`, `.home`, `.test`, `.invalid` — but only where the token is genuinely
  used as a host. Not flagged: import paths (`pkg.internal.helpers`), filenames
  (`internal.md`), file paths (`app/config.test`), dotted calls, and any dotted name whose
  last label cannot be a TLD. **In Python source only**, a spaced assignment whose
  right-hand side is a bare dotted module chain is also exempt — a shell assignment
  (`NAME=value`, no spaces) never is, in any file, because that is a realistic way to write
  a real internal host in a bash block or a workflow `run:` step. A *quoted* host is always
  checked. **Backticks are not an exemption** — a hostname in backticks is checked exactly
  as a bare one is, because backticks are how this repository normally writes hostnames.
  `.test` and `.invalid` are reserved TLDs but also ordinary file suffixes, so they count
  only inside a URL. "Inside a URL" means **any** scheme, or none: `ssh://`, `git://`,
  `jdbc:postgresql://` and a bare `//` all count, not only `http`/`https`. A clone command
  for an internal repository is a usual thing to write in a contributing guide or a
  workflow `run:` step, and it was the exemption for file paths that used to clear it —
  the text before the host ends in `/`;
- a URL that looks like an issue tracker or forge link (`/browse/KEY-123`, `/jira/`,
  `/confluence/`, `/-/merge_requests/`);
- a **bare issue-tracker key** anywhere, with no URL around it — an uppercase project
  prefix, a hyphen and a number, the shape every tracker uses. A key in a comment or a
  commit message references internal work just as a link does, and the rule above forbids
  ticket references, not merely tracker links. (This paragraph describes the shape instead
  of giving an example, because the gate checks its own documentation and a realistic
  example would fail it — same as the IP ranges above.) Standards and formats are
  spelled identically (`ISO-2`, `RFC-1918`, `UTF-8`, `SHA-256`), so a prefix allowlist,
  `NON_TICKET_PREFIXES`, carries those plus this repository's own placeholders. Anything
  not on it is treated as a possible ticket: if you trip the gate on a genuine standard,
  add the prefix there, with the same scrutiny as a new sample;
- a private or loopback IPv4 address (`10/8`, `172.16/12`, `192.168/16`, `127/8`,
  `169.254/16`), anywhere, in or out of a URL. Hostname matching cannot see these — an
  address has no letters-only final label — and naming an internal dashboard, database or
  CI box by IP is at least as common as naming it by hostname. Public addresses are not
  flagged, since an example may legitimately use one. A four-part version string that
  falls inside one of those ranges is flagged too; that is the right way round for a
  backstop, and it is why this paragraph describes the ranges instead of spelling one out
  — the gate checks its own documentation.

It is a **backstop, not the review**. It cannot see an internal service or database name
written in prose, an internal tool referred to by name, an internal wiki link on a domain
it does not recognise, a real account number that looks like a placeholder, or a ticket key
in a commit message. Two limits are deliberate and worth knowing:

- an `internal`/`intranet` label under an *uncommon* gTLD is not detected, because at that
  point a hostname and a dotted code path are indistinguishable. Widening it would bring
  the code false positives back;
- only IPv4 is matched, and only the private ranges above. An IPv6 address, or an internal
  service on a routable address, is not detected.

A human reviewer must check those, and must state in the pull request that they did.

The gate has its own tests in `scripts/test_validate.py`, table-driven over the samples it
must flag and the samples it must not. Add to that table whenever you change the rules —
every exemption is a hole until a test says otherwise.

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

The same commands CI runs on every pull request:

```bash
python3 scripts/validate.py             # manifests, frontmatter, published-content scan
python3 -m pytest plugins scripts -q -p no:randomly   # skill tests AND the gate's own
```

`-p no:randomly` is not decoration: the suite is not safe to shuffle or parallelise (see
below), so leaving it off runs the tests in a different order than CI does.

The tests are plain `unittest`, so they also run without pytest installed — which is how
CI double-checks them:

```bash
for t in $(find plugins scripts -name 'test_*.py'); do python3 "$t"; done
```

Tests are **required** for anything that moves money or decides whether a payment is a
duplicate. CI fails if no tests are collected, so a green pipeline cannot be reached by
deleting them. CI runs them on Python 3.8 and 3.11 — 3.8 is the supported minimum and the
configuration where the built-in timezone fallback runs instead of `zoneinfo`.

Run them serially. `frozen_clock` in `_testsupport.py` patches the shared `datetime` and
`time` modules for the whole process, and each test module redirects `tempfile.tempdir`
(this process), `TMPDIR`/`TEMP`/`TMP` (any subprocess it spawns) and `HOME` — all of that
is process-wide, so parallel runners (`pytest -n`) are not supported.

The temp box and the `HOME` redirect answer different questions, and a test needs both.
The box catches what the suite **writes**; `HOME` limits what it **reaches**. Both scripts
chmod `~/.config/paysera-payments/` to `0700` on every run that reads the token, so a test
that inherits the real `HOME` changes a directory in the contributor's own home — and the
box cannot see that, because it only looks inside itself. A script resolves its
`HOME`-derived constants at **import** time, before any `setUpModule`, so redirecting the
variable is not enough on its own: `redirect_config_paths(module)` re-points those
constants and must be called from `setUpModule` for every module that runs script code
in-process.

That check exists twice, in `_testsupport.py` and in `scripts/test_validate.py`, because a
plugin file must not read a repository file and the reverse would be as wrong. Copies drift:
if you change one, change the other, and keep **all three** redirects in both. A module that
redirects only `tempfile.tempdir` looks clean while its subprocesses write to the real
`/tmp`; one that redirects neither `HOME` looks clean while it edits the contributor's home
directory. Both copies carry the missing-piece argument in a comment — a module that starts
no subprocess and reads no `HOME` today is exactly the one where an omission goes unnoticed
until the first code that needs it escapes silently.

The tests inside a plugin never read a file outside it: `claude plugin install` copies the
plugin directory alone, so anything a shipped test reaches for must be shipped with it.

### What is not scanned

`validate.py` skips two lists of names, so that a `.venv` in your checkout or a review note
beside it does not make the gate slow and red on content nobody publishes:

- **`SKIP_DIRS`** — local directories: `.venv`, `venv`, `node_modules`, `.idea`, `.vscode`,
  `.tox`, `.mypy_cache`, `.ruff_cache`, plus `.git`, `__pycache__` and `.pytest_cache`.
- **`SKIP_FILES`** — review working notes at the **repository root only**: `REVIEW.md` and
  `REFUSE.md`. The root restriction is what makes this safe: a file with one of those names
  *inside a plugin* is shipped by `claude plugin install`, so it is published content and is
  still scanned.

Anything the gate does not read is a blind spot in a check whose whole job is to stop
internal content going public. So **every name in both lists is also in `.gitignore`** —
untracked means never published, which is the property that makes skipping it safe.
`test_validate.py` enforces the pairing for both lists; adding a name to one without the
other fails the suite.

## Releasing

Merging to `main` publishes: `claude plugin marketplace add paysera/skills` resolves the
default branch, so users get the change on their next `claude plugin marketplace update`.
Tag the merge commit `<plugin>-v<version>` (e.g. `paysera-payments-v1.3.2`) so each published
version is traceable.

## Issues

Public issues are enabled for installation and usage problems with the skills. Do **not**
post account numbers, transfer hashes, tokens, or any personal data in an issue. Security
reports go through [SECURITY.md](SECURITY.md), never a public issue.
