# Changelog

## 1.8.12 (2026-08-14)

Twenty-fourth review round. One finding, and the first in three rounds that is in a payment
script rather than the test scaffolding — an old defect, not drift from a recent change.

- **`--perform-at` had no upper bound.** `--amount` has been bounded since 1.7.0, for the
  stated reason that no legitimate value is that large and the API's rejection is late and
  cryptic; its sibling option took an arbitrary integer. `SKILL.md` and `--help` both offer
  "epoch seconds", and an extra digit is the ordinary way to mistype a pasted timestamp.
  Two of the four spellings then crashed the schedule printout with a `ValueError` or
  `OverflowError` out of `datetime` — a Python traceback where every other bad argument in
  this tool gets one plain sentence — and one digit lower the value was accepted in silence
  as a signing deadline in the year 5138. Nothing was ever created (the crash lands before
  the payload, the `--confirm` test and the write-ahead ledger row), so this was a usability
  and silent-acceptance defect rather than a money one.
  The resolved epoch is now bounded to `MAX_PERFORM_AT_DAYS` (366) beside the existing
  past/near-instant test, in the wording `--amount` already uses. `_safe_date()` formats the
  offending value without raising the very error the bound replaces.
  **Behaviour change:** an explicit `YYYY-MM-DD` more than 366 days ahead is now refused
  too. It was previously bounded only by `strptime`'s year 9999, and `perform_at` is a
  signing deadline — a draft sits unsigned until then and auto-cancels after — so a
  multi-year window is not a use case the tool should accept quietly.
  `--help` said "Only a past or near-instant time is rejected", which the bound made false;
  it and `SKILL.md` now state the rule as applied, and a test fails if the help stops
  quoting the constant.

## 1.8.11 (2026-08-14)

Twenty-third review round. No Critical, High or Medium defect. Neither payment script
changed; all three findings are on the twin-copy agreement test added in 1.8.10.

- **`CONTRIBUTING.md` forbade the direction that test relies on.** It said a plugin file
  must not read a repository file "and the reverse would be as wrong", while the new test
  reads two plugin files from the repository side. The asymmetry is real — `claude plugin
  install` copies the plugin alone, so only the plugin-to-repository direction can break an
  installed copy — but the guide never said so, and it is the document a reviewer is told
  to follow. A contributor obeying it would have deleted the test that enforces the
  copy-drift rule stated in the same paragraph. The guide now states the rule as applied,
  and a test fails if it goes back to forbidding it — added after a mutation showed the
  correction was prose nothing checked, which is how it drifted in the first place.
- **One of the three comparisons raised `IndexError` instead of naming its cause.** Its two
  siblings assert on the match before reading it; this one indexed a `findall` result
  directly, so a restructured copy reported a crash rather than "the copy was
  restructured". All three now go through one guarded helper.
- **The comparison was anchored to fixed paths**, so it covered one plugin and one of that
  plugin's two test modules. `test_cancel_payment.py` has no full-cycle test today, and a
  second plugin does not exist yet — but the check said nothing about either, and this
  repository is a marketplace that gains skills one at a time. The copies are now
  discovered by glob, every test module beside a discovered copy is compared, and the
  search fails if it finds nothing rather than passing on an empty set.

## 1.8.10 (2026-08-14)

Twenty-second review round. No Critical, High or Medium defect. Neither payment script
changed; both findings are gaps in what protects the previous round's fix.

- **Nothing failed if `test_cancel_payment.py` stopped sandboxing its config paths.**
  1.8.9's fix has two parts — `HOME` redirected, and the import-time constants re-pointed
  by `redirect_config_paths()`. Deleting the second call from that module's `setUpModule`
  left the suite green while restoring the defect 1.8.9 closed: four in-process
  `read_token()` calls chmodding the contributor's own directory again. This repository
  treats a check that cannot fail as a defect in itself, and the new sandbox had no such
  guard on the cancel side. Both modules now assert their constants point inside the
  sandbox, driven off `_HOME_DERIVED` rather than a hard-coded name — so a name dropped
  from that tuple fails too — with a count assertion so the loop cannot pass vacuously.
  The cancel module also gains the end-to-end proof the create module already had: a
  config directory outside the sandbox stays at `0755` while the one inside is tightened.
- **The two copies of the leak check disagreed about `HOME`.** Both have a test that one
  setup/teardown cycle leaves the process as it found it; the repository copy checked all
  four variables, the plugin copy three. The behaviour was right — `assert_tempdir_is_empty()`
  does restore `HOME` — but only one copy confirmed it, which is the copy-drift
  `CONTRIBUTING.md` forbids. The plugin copy now checks all four, and asserts the `HOME`
  redirect in its isolation test as the repository copy does.
  The prose rule that the copies must agree is now also a test:
  `TestTheTwoCopiesOfTheLeakCheckAgree` compares what each copy redirects, what each
  captures for restoring, and what each full-cycle test asserts, and fails on any
  divergence. The last of those was added after a mutation survived: comparing only the
  redirects still let the plugin copy's `keys` tuple drift, which is where the drift
  actually was.

## 1.8.9 (2026-08-14)

Twenty-first review round. Both findings are on 1.8.8's own changes.

- **The test suite reached outside its sandbox and chmodded the contributor's own config
  directory.** 1.8.8 gave `cancel-payment.py` the `0700` hardening on every run that reads
  the token. The shared cancel-test fixture never set `HOME`, and neither did any of the
  in-process `read_token()` tests in either module — so running the documented test command
  silently changed the mode of the developer's real `~/.config/paysera-payments/`. The
  temporary-directory leak check could not see it: that check catches what the suite
  *writes*, inside a box it owns, and this was a change made *outside* the box entirely.
  `isolate_tempdir()` now redirects `HOME` alongside `TMPDIR`/`TEMP`/`TMP`, and a new
  `redirect_config_paths()` re-points the scripts' `HOME`-derived constants — which
  `expanduser()` resolves at import time, before any `setUpModule`, so the variable
  redirect alone would not have covered in-process calls. The repository-side copy of the
  leak check carries the same three redirects, under the same twin-copy rule.
- **`SKILL.md` named two of the three ways the live scan can come back incomplete, and
  called the list exhaustive.** The shape-change cause added in 1.8.7 and refined in 1.8.8
  appeared nowhere in the documentation, so a reader meeting that warning met a message
  from a cause the skill did not describe. All three are now documented, including the
  `_metadata.total` rule that separates a quiet account from an API change, and a test
  fails if a fourth warning is added without updating the paragraph that claims the list
  is complete.

## 1.8.8 (2026-08-14)

Twentieth review round. No Critical, High or Medium defect was reported; all four findings
are drift introduced by the previous round's own changes.

- **`cancel-payment.py` never tightened the configuration directory.** 1.8.7 gave
  `create-payment.py` a `_harden_config_dir()` call on every run that reads the token, and
  strengthened `SKILL.md` to say so — but `cancel-payment.py` has its own `read_token()`
  and got no such call. A machine used for cancellations alone kept the umask's `0755` on
  the directory that holds the token and the ledger. It now has the helper too, deliberately
  copied rather than shared (a plugin file must stay self-contained, as `_check_token_file_mode`
  already is), fixed to the default config directory so that `--token-file` cannot make it
  chmod a path it was only asked to read from.
- **A metadata-only empty page counted as an API change.** 1.8.7 treated any 200 body with
  no `items`/`transfers`/`data` list as an unrecognised shape. The API does send a
  `_metadata` block, so if it omits the rows key when there is nothing to list, every scan
  of a quiet account would have warned that the API changed and marked the duplicate check
  incomplete — and a warning that fires on ordinary results stops being read. The answer's
  own `total` now settles it: `0` is an empty result, `> 0` with no rows key stays loud,
  because that is rows existing and not being visible.
- **`SKILL.md` still described the 1.8.6 form of the incomplete-scan note** — the marker
  printed "instead of" the all-clear line. 1.8.7 made it unconditional and printed it
  *above* the rest of the summary, so the case that fix was made for, a truncated scan that
  still found rows, was the one case the documentation did not cover.
- **`CONTRIBUTING.md` overstated the gate's coverage.** It said `validate.py` scans *every*
  `.md`/`.py`/`.json`/`.yml`/`.yaml` file in the repository, and its pairing-rule section
  named `SKIP_DIRS` only. That section is the blind-spot list a reviewer is told to confirm
  by hand, so a skip it does not name cannot be checked. Both mechanisms are now described,
  including the root-only restriction that makes the file skip safe.

## 1.8.7 (2026-08-14)

Nineteenth review round. Four of the six findings are on 1.8.6's own changes.

- **A truncated live scan that still returned rows reported as complete.** 1.8.6 put the
  `LIVE SCAN INCOMPLETE` note in an `elif`, so only the empty case could reach it; a scan
  that lost a page or hit the 50-page cap and still found rows printed the found-list with
  nothing said about the truncation. The note is now unconditional and printed *before*
  the list, so a partial view is labelled as one.
- **That note claimed the ledger had found nothing** — but it is printed directly above the
  `SKIP` block, which is where ledger matches are listed. Two stdout lines could disagree
  about the ledger, and the `SKIP` is the line that stops a second payment. The note now
  says only what it knows: the live list could not be read in full.
- **A 200 response in an unrecognised shape read as a complete, empty scan.**
  `_transfer_items()` returned `[]` both for a page with no rows and for a body with no
  `items`/`transfers`/`data` list, and `list_transfers()` ends its walk on an empty page.
  An API that renamed its container key would have turned the live half of the duplicate
  check into a silent all-clear. It now returns `(rows, recognised)`, and an unrecognised
  shape warns on stderr and marks the scan incomplete. An empty object still counts as an
  honest empty result.
- **`SKILL.md`'s new stderr contract was not true for `create-payment.py`.** 1.8.6 wrote
  "both scripts … every message that makes the run exit non-zero goes to stderr". That
  holds for `cancel-payment.py`; `create-payment.py` puts its *decisions* on stdout — the
  duplicate `SKIP` block behind `exit 3`, including the `--register-only` remedy, and the
  `FAILED (HTTP …)` body behind `exit 1`. A caller that believed the sentence and kept
  stderr alone would lose exactly those. The documentation now describes each script's
  split as it is, and tests pin it in both directions.
- **`update_ledger()`'s result was still discarded after an unanswered POST.** The
  `unknown` row is the only record that a draft may exist on the server, and the message
  promises later runs will refuse because of it. With the row gone that promise was false.
  The failure path now tests the write like the success path does and says so instead.
- **The publication gate failed on untracked working files at the repository root.** The
  1.8.6 argument — untracked means never published, so it need not be scanned — was applied
  to directories only. `SKIP_FILES` extends it to review notes (`REVIEW.md`, `REFUSE.md`),
  paired with `.gitignore` under the same enforced rule, and only at the root: a `REVIEW.md`
  inside a plugin ships with `claude plugin install` and is still scanned.

## 1.8.6 (2026-08-14)

Eighteenth review round.

- **A `--no-register` draft made the next run recommend `--force`, which creates a second
  signable draft.** `find_blocking()` recognised an unregistered draft by testing the
  ledger's `registered` flag for `is False`. That is only one of the three ways a draft can
  be unsignable: `--no-register` writes `None`, and rows written before 1.8.0 have no such
  key. Both fell through to the generic "Use --force to override" — the exact opposite of
  the remedy, and the double-payment condition 1.5.0/1.7.0/1.8.0 exist to remove. The live
  status now decides it (`new` means not signable), with the ledger flag only as a
  fallback, so all three cases point at `--register-only`.
- **An incomplete live duplicate scan still printed an all-clear on stdout.** A failed page
  and the 50-page cap both warn on **stderr** and then return a partial list; the caller
  could not tell, and printed `No prior payments to those accounts in the period.` on
  stdout. `list_transfers()` and `find_blocking()` now return a completeness flag, and the
  summary reads `LIVE SCAN INCOMPLETE` instead. Same defect class as 1.7.1, 1.7.4 and
  1.8.1: a partial check reporting as complete.
- **The `0700` promise for `~/.config/paysera-payments/` was not kept on every run.** Only
  `ledger_lock()` and `_write_ledger()` hardened the directory, and both run only with
  `--invoice-id`; a dry run left it at the umask's `0755` while `SKILL.md` said otherwise.
  `read_token()` now tightens it on every run — and, unlike the old path, never creates it,
  so a dry run still leaves no trace.
- **`--register-only` could exit 1 after the registration had succeeded.** `mark_registered()`
  took the ledger lock, and the lock exits the process when another run holds it. Exit 1
  contradicts the documented "every other non-zero code means nothing was created", and
  invites a wrapper to retry. The ledger write is now non-fatal there and warns instead;
  a run that is about to *create* is still stopped by a held lock.
- **`_main()` reported a ledger record it had not made.** `update_ledger()` returns `False`
  when the write-ahead row is gone (a hand edit, a `rm` mid-run); the return value was
  discarded and `ledger : recorded for invoice …` printed regardless. The ledger is the
  only guard against a second draft, so it now prints `NOT recorded` and warns on stderr.
- **`cancel-payment.py` sent its failures to stdout** while `SKILL.md` promised stderr and
  `create-payment.py` did that. Every message that makes the script exit non-zero now goes
  to stderr; the per-transfer report stays on stdout.
- **The publication gate scanned local directories nobody publishes.** A `.venv` or
  `node_modules` in a contributor's checkout was scanned as published content, making the
  documented local run slow and red on third-party code. `SKIP_DIRS` now covers the usual
  ones — and because a skipped directory is a blind spot in a check that exists to stop
  internal content going public, every name in it must also be in `.gitignore`, which a
  test enforces.
- `CONTRIBUTING.md` now documents `-p no:randomly`, which CI passes and the documented
  command omitted, on a suite the same document says must not be shuffled.
- Recorded the built-in timezone fallback's one known divergence from `zoneinfo` (the
  non-existent 03:00–03:59 wall hour on the spring transition; nothing in this file can
  reach it), and removed two unused imports.

## 1.8.5 (2026-08-13)

Seventeenth review round.

- **The published exit-code contract was missing `2`.** `resolve_payer()` exits 2 for a
  `--payer` outside the token's scoped accounts, and argparse exits 2 for a usage error —
  both ordinary results, neither in the list an agent reads to decide what a run meant.
  `SKILL.md` now documents `2`, documents `cancel-payment.py`'s own codes, and states that
  the list is exhaustive: a code outside it is a bug, not a result to interpret. A test
  reads every `sys.exit(N)` out of the source and fails on any code the documentation does
  not list, so the next one added cannot be forgotten.
- **Every URL built from a transferHash is now shape-checked**, not just the two built from
  a hash somebody typed. `find_blocking()` took one from the ledger — a plain JSON file the
  documentation invites the operator to read — and `register_transfer()` took one from the
  API's answer. Both now go through `_checked_hash()`, which owns the rule. A malformed
  ledger row blocks with a message naming the row instead of fetching whatever path it
  spells out (still fail-safe, and now no request is made at all); an id the API returns
  that cannot go in a URL is reported as a failed registration rather than a crash.

## 1.8.4 (2026-08-13)

Sixteenth review round. Both findings are about the two copies of the leak check drifting
apart in the round that created the second copy.

- **The repository copy redirected only `tempfile.tempdir`, not `TMPDIR`/`TEMP`/`TMP`.**
  Harmless today — `scripts/test_validate.py` starts no subprocess — but its own probe
  asserted only the half it had, so the first subprocess test added there would have
  written to the real `/tmp` while the module reported clean. Confirmed by adding such a
  test: before the fix the module passed, after it the module fails and names the leak.
  Both copies now redirect both halves, and both probes assert both and then prove it with
  a real `mkdtemp()`.
- **A disarmed check no longer reads as a pass.** `assert_tempdir_is_empty()` returned
  quietly when `setUpModule` had not run — the exact "never fired and nothing leaked look
  identical" failure the check exists to prevent, one level up. Both copies now raise
  `setUpModule did not run — the leak check is disarmed`, and both have a test for it.
- Both copies restore `TMPDIR`/`TEMP`/`TMP` and `tempfile.tempdir` at teardown, with a test
  that a full cycle leaves the process as it found it. Under pytest the three test modules
  share one process, so a teardown that skips the restore hands the next module a path that
  no longer exists.
- `CONTRIBUTING.md` says the check exists twice, why, and that both halves belong in both.

## 1.8.3 (2026-08-13)

Fifteenth review round. Both findings are about the previous round's leak check, which is
now built a third way — and the third way is the one that should have been obvious.

- **A shipped test reached outside the plugin.** The leak check ran each test module in a
  subprocess, including `scripts/test_validate.py`, which belongs to the repository.
  `claude plugin install` copies the plugin directory alone, so from an installed copy that
  path does not exist and the check failed — a hard failure naming no real defect.
  Confirmed by running the shipped suite from a copy outside the repository.
- **It also ran the whole suite a second time inside itself**: four executions of every
  module per CI job, this module carrying 63 subprocess call sites.

  Both are gone. Each test module now gets its own temporary directory
  (`tempfile.tempdir` plus `TMPDIR`/`TEMP`/`TMP`, so spawned subprocesses land inside it
  too) and must leave it empty at `tearDownModule`. Same property, no path outside the
  plugin, no second run: the suite goes back from ~7.5s to ~3.4s. `scripts/test_validate.py`
  carries its own six-line copy rather than importing the plugin's — a plugin file must not
  depend on a repository file, and the dependency the other way round would be as wrong.
- The teardown check has tests of its own in both places, because a check that runs at
  teardown cannot be observed from inside the module: "it never fires" and "nothing leaked"
  look identical otherwise.

## 1.8.2 (2026-08-13)

Fourteenth review round. Both findings are about the previous round's own fix.

- **The rename was half done.** 1.8.1 changed the tool's output from "IBAN(s)" to
  "beneficiary account(s)" because calling a national account number an IBAN is how the
  missing key stayed unnoticed — and then left `SKILL.md` saying the check "scans all of
  the beneficiary's IBANs", "prints every payment it finds to those IBANs", and "is only
  as complete as the IBANs you give it". An operator paying an Armenian or Georgian
  account could read that and conclude the duplicate check does not cover them. The
  documentation, the `find_blocking` docstring and the remaining code comments now say
  "account", and a test reads the dedup section of `SKILL.md` and fails on the old wording.
  "IBAN" is kept where it means the flag names or the Paysera IBAN selection rule.
- **The leak check is now behavioural.** It counted `tempfile.mkdtemp(` occurrences against
  `shutil.rmtree` occurrences in each test module — but it read its own module, so both
  strings counted themselves and the balance was accidental (a docstring naming either
  function would have broken it), equal counts never proved pairing anyway, and
  `scripts/test_validate.py` was not covered. Each test module is now run in a subprocess
  with an empty `TMPDIR` of its own, and the directory must be empty afterwards. No
  bookkeeping, covers every module including ones added later, and it caught a deliberately
  reintroduced leak in all six places plus `test_validate.py`.

## 1.8.1 (2026-08-13)

Thirteenth review round.

- **The duplicate check could not see a payment to a non-IBAN account.** The live
  cross-check read the beneficiary account from two keys, `bank_account.iban` and
  `beneficiary.iban`. It never read the third — `bank_account.bank_account_number` — which
  is the key this tool itself writes for a national account number (Armenian, Georgian and
  similar). For every such beneficiary the account was absent from the candidate set, so
  every prior payment was dropped before the amount and invoice-id rules ran, and the run
  printed "No prior payments … in the period". Only the local ledger stood in the way, and
  the ledger cannot see a payment made by hand in the app. All three keys are now read.
- The dup-check output says "beneficiary account(s)", not "IBAN(s)". A national account
  number is not an IBAN, and calling it one is how the missing key stayed unnoticed.
- **The publication gate now treats any scheme as a URL** — `ssh://`, `git://`,
  `jdbc:postgresql://`, or a bare `//`, not only `http`/`https`. An internal host behind
  another scheme fell through to the prose exemption, whose file-path rule then cleared it,
  because the text before the host ends in `/`. A clone command for an internal repository
  is an ordinary line to write in a contributing guide or a workflow `run:` step.
- `--beneficiary-address` help said REQUIRED for any cross-border transfer; the pre-flight
  check demands it only outside the SEPA zone. The help now states the rule that is
  actually applied, and a test pins the two together.
- Gate errors about a skill name a repository-relative path, and name the file once. Two
  of them printed the CI runner's own absolute checkout path, and one printed the file
  name twice.
- The test suite removed none of its temporary directories: one per test method, 84 per
  run, each holding a ledger with test IBANs and amounts, and CI runs the suite twice.
  Every `mkdtemp()` is now paired with a cleanup, and a test fails if one is not.
- `CONTRIBUTING.md` lists `.yaml`, which the gate has always scanned.

## 1.8.0 (2026-08-12)

Twelfth review round.

- **A failed register step reported success.** After creating a transfer the tool calls
  `PUT /transfers/{hash}/register`; without it the transfer stays in `new`, which is shown
  nowhere for signing. When that call failed the run printed the failure and then carried
  on down the success path: the process exited **0**, the ledger row was written as
  `state="created"` — the same value a registered transfer gets, so nothing downstream
  could tell them apart — and the closing line, four lines after saying the transfer was
  invisible, told the operator to open the app and sign it. A wrapper or an agent reading
  the exit code reported the payment as ready.

  Now: exit **4** (distinct from `1`, because `1` invites a retry and this draft must not
  be created again), a closing message that says it is not signable, and `registered` on
  the ledger row.
- **Added `--register-only <hash>`**, so the remedy lives in the tool. Previously the only
  ways out were a hand-written PUT carrying the token, or `--force` — which creates a
  second draft instead of fixing the first, while the duplicate guard correctly refuses a
  plain re-run. A later run for the same invoice now names this command in its `SKIP`
  output, and suppresses the usual "use --force to override" line, which would contradict
  it. `--register-only` creates nothing, so it needs none of the payment arguments; it is
  dry-run by default like every other sending path, and validates the hash shape before
  putting it in a URL.
- `--no-register` now says the transfer is not yet signable, and how to make it signable,
  instead of ending on the same "open the app and sign" line.
- The register step had **no test coverage at all** — the one test reaching that code
  stubbed a curl answering 201 to everything, so the register call succeeded by accident
  of the stub. It now has twelve, covering both outcomes, both exit codes, the ledger
  field, the remedy path, and the argument handling.

## 1.7.4 (2026-08-12)

Eleventh review round.

- **The duplicate scan's page cap truncated the result without saying so.**
  `list_transfers()` reads at most 50 pages of 100. Every other way that walk can end is
  honest — an empty page, a short page and a reached total all mean the end of the data,
  and an HTTP error prints a loud warning — but running out of pages returned a partial
  list the caller could not tell from a complete one, and the run then printed its usual
  "scanned payments … since \<date\>" line over it. Verified with a stubbed 12000-transfer
  window: 5000 read, 7000 dropped, nothing on stderr. Reaching the cap now warns in the
  same terms as the HTTP-error path, naming how many rows were read and pointing at
  `--invoice-date` to narrow the window. Raising the cap would not have fixed it — the
  silence was the defect.

  It needs a busy payer account together with an old `--invoice-date` to bite, and the
  ledger source is unaffected. It is fixed because it is the same shape as the last three
  rounds' findings: a partial check reporting as complete.

## 1.7.3 (2026-08-12)

Tenth review round.

- **The future-date guard added in 1.7.2 refused TODAY for the first 2-3 hours of every
  Vilnius day.** It compared UTC midnight of the given date against the current instant,
  and for the first 2 hours (EET) or 3 hours (EEST) of a Vilnius day that midnight has not
  arrived — so an ordinary run with a correct invoice date stopped with exit 1, in a
  message that then printed today's Vilnius date as the reason it was not today. A
  scheduled job running after midnight hit it every night, and the obvious workaround —
  dropping `--invoice-date` — puts the duplicate scan back on the wide default window that
  1.7.0 narrowed on purpose. "Is this date in the future?" is now a calendar comparison in
  Vilnius, like every other date decision in this file.
- The test that was supposed to cover this could not fail. It called `date.today()` and
  ran the script against the real clock, so on a UTC runner UTC midnight of the UTC date
  is always past. The boundary now has its own tests against a **frozen clock**, at every
  hour of the day, in both EET and EEST, and on both sides of the boundary — reverting the
  fix fails eight of them. The end-to-end test is kept as a companion and now says in the
  file that it cannot see the boundary by itself.

## 1.7.2 (2026-08-12)

Ninth review round.

- **The 1.7.1 separator fix was applied at one call site, and was the wrong one.** It
  stripped separators from `--iban` only, and only *after* `select_beneficiary_iban()`
  had already chosen the payee, leaving `--also-iban` untouched entirely. Two consequences,
  both verified: a Paysera IBAN pasted as `LT60-3500-…` in `--also-iban` lost the payment
  while the run printed "no Paysera IBAN listed" on the line above it; and, worse, the
  duplicate check compared candidates with `_norm_iban()`, which stripped spaces only, so
  a separated account could never match what the API returns and contributed nothing to
  the check — while the run still counted it as scanned. SKILL.md tells the operator to
  pass every IBAN the invoice lists; one of them was silently doing nothing.

  The normalisation now lives in `_norm_iban()`, which is the shared helper behind all
  three decisions that read an IBAN, and every listed account is cleaned before any of
  them is compared or chosen. Unicode dashes are handled too. The narrower rule for the
  value actually **sent** is unchanged: separators are removed only when what remains is
  a well-formed IBAN, so a national account number keeps its punctuation.
- A **future** `--invoice-date` is now refused. The window would start after today, hold
  no transfer, and the run would then report "no prior payments to those IBANs" — an
  all-clear from a check that could not have found anything. A mistyped year is the usual
  way in. Today's date is still accepted, which is the boundary that matters.
- The publication gate now flags a **bare issue-tracker key**, not just a tracker URL.
  CONTRIBUTING.md forbids ticket references in files and commit messages outright, so the
  gate's narrower rule made the documented promise stronger than the check behind it.
  Standards and formats share the exact shape (`ISO-2`, `RFC-1918`, `UTF-8`, `SHA-256`),
  so a prefix allowlist carries those and this repository's placeholders; anything else
  is treated as a possible ticket. Both halves are pinned by samples.

## 1.7.1 (2026-08-12)

Eighth review round.

- **The duplicate-check report could name a window that was not scanned.** A malformed
  `--invoice-date` warned on stderr and fell back to the 90-day window, while stdout said
  "since \<the malformed value\>" — two streams disagreeing about the period a
  money-safety check covered, and for an invoice older than the default window a real
  duplicate could sit outside the scan that stdout claimed had covered it. A day-first
  date is now refused outright: a date the operator typed is one they expect to be used.
  The window and the sentence describing it also come from one helper now, so they cannot
  drift again.
- The lock guard was an `ExitStack` that nothing closed, so its release depended on
  refcounting collecting the stack rather than on the scope the comments claimed. Nothing
  failed in practice — the process exits immediately after and the kernel drops the flock
  — but `main()` now owns the stack explicitly and releases it on every exit path.
- **The publication gate could not see an IP address at all.** Hostname matching requires
  a letters-only final label, so a private-address URL — a usual way to name an internal
  dashboard, database or CI server — passed cleanly, and CONTRIBUTING.md's list of blind
  spots did not mention it, so a reviewer trusting that list would think the class was
  covered. Private and loopback IPv4 ranges are now flagged wherever they appear, and
  `.localhost` was added to the reserved-TLD list. The remaining limits (IPv6, internal
  services on routable addresses) are now written down.
- An IBAN written with separators (`LT12-1000-…`) was rejected as "not an IBAN" with a
  demand for a BIC, sending the operator after the wrong problem. Separators are stripped
  when what remains is a valid IBAN, with a note; a genuine non-IBAN account number is
  still refused as before.

## 1.7.0 (2026-08-12)

Seventh review round — the first since 1.6.0 to change the shipped skill rather than the
publication checker.

**Security**
- **The `0600` token file was documented but never created or checked.** The setup steps
  said only "save the returned token to …", so a plain `>` redirect under the usual
  `umask 022` produced a `0644` file, and `read_token()` never looked at the mode. Every
  local user could read a PAT carrying `transfers:create` and `transfers:cancel`. Unlike
  the argv exposure closed in 1.4.0, which lasted for the length of one request, this was
  permanent. Both scripts now refuse a token file readable by group or other and print the
  `chmod` that fixes it; SKILL.md creates the file under `umask 077` instead.
- The config directory is genuinely `0700` now. `os.makedirs(mode=0o700, exist_ok=True)`
  applies the mode only to a directory it creates — and the user creates this one first,
  for the token — so it silently stayed at the umask's `0755`.

**Double payments**
- **Concurrent runs could lose a ledger row.** The append was a read-modify-write with no
  lock, so of two overlapping runs the second erased the first's row. That row is usually
  the write-ahead `pending` one, and since `GET /transfers` cannot list unsigned drafts it
  is the *only* record of the draft — losing it lets a later run create a second signable
  draft for the same invoice, the exact failure 1.5.0 set out to stop. A sending run now
  holds an exclusive lock from before the duplicate check until the attempt is recorded,
  so two runs cannot both pass their own check. It is non-blocking and fails closed; dry
  runs neither take it nor are blocked by it.
- **Without `--invoice-date` the scan covered the entire account history**, where a match
  on the amount alone blocks. A supplier billed the same sum every month was refused every
  month after the first, under a message naming the wrong invoice — and the only remedy
  offered was `--force`, which disables the whole duplicate check, ledger included. The
  window is now 90 days by default, the `SKIP` output says which rule matched each hit,
  and an amount-only match points at `--invoice-date` rather than at `--force`.

**Cross-border**
- A **non-IBAN account number with no BIC** (the Armenian `2050…` format, which the script
  explicitly supports) yielded no country, and an unknown country read as "domestic, in
  SEPA": `--beneficiary-type`, `--beneficiary-bic`, `--beneficiary-address` and
  `--beneficiary-city` were all skipped and the SEPA-Instant rail was chosen for an
  account it cannot reach. The API then refused the transfer with
  `mapper_beneficiary_country_not_set` — precisely the late, cryptic failure these
  pre-flight checks exist to replace. A BIC is now required for a non-IBAN account, and an
  undeterminable country is refused rather than assumed domestic.
- Added the SEPA members admitted in 2023-2024 (AL, MD, MK, ME), with the date the list
  was last checked. They were being treated as international wires.

**Correctness**
- The payload and the ledger now carry the **validated** amount, not the raw `--amount`
  text. `1e2`, `+12.34` and `" 12.34"` all passed validation and were sent verbatim.
- The built-in Vilnius rule converted the hour 01:00-02:00 UTC on the last Sunday of
  October one hour late, and non-monotonically. The cause was inheriting
  `tzinfo.fromutc()`, which infers the offset by calling `dst()` back on a
  partly-converted value — one boundary constant cannot serve both that convention and
  the wall-clock one `utcoffset()` receives. `fromutc()` is now explicit and marks the
  repeated hour with `fold=1`. Affected hosts without tzdata only, and printed deadlines
  rather than dates. The transitions are now walked minute by minute against `zoneinfo`.

**Checks and tests**
- `validate.py` reports malformed JSON and missing frontmatter as errors naming the file,
  instead of a traceback that tells a contributor nothing. (CRLF turned out **not** to be
  a failure mode — `read_text` translates universal newlines — so that is now pinned as
  working rather than "fixed".)
- `TestWriteAheadLedger` subclassed a `TestCase` to reuse its fixture, so unittest ran all
  15 inherited subprocess-spawning tests a second time, in a suite CI runs twice. The
  fixtures in both test files are plain mixins now.
- Every fix above is pinned by a test that fails when the fix is reverted.

## 1.6.3 (2026-08-11)

Sixth review round — publication checker only; the shipped skill is unchanged.

- **Closed a hole introduced in 1.6.2.** The Python-assignment exemption also matched a
  SHELL assignment (`NAME=value`), which is a realistic way to write a real internal host
  in a bash block or a workflow `run:` step — so those bypassed the gate entirely. The
  exemption is now restricted to Python source, to assignments with whitespace on both
  sides of `=` (shell syntax forbids the spaces), and to lines with no quote before the
  token. Dropping any part of that restriction fails nine tests.

## 1.6.2 (2026-08-11)

Fifth review round — publication checker only; the shipped skill is unchanged.

- The "checker is exempt" test asserted an empty result against an empty temporary
  repository, so it held regardless of the exemption logic and never reached it. It now
  writes real files, including a decoy with the same name in another directory, and pins
  both halves of the rule. Breaking the exemption either way now fails it.
- Removed the residual false positives on dotted code chains whose last label happens to be
  a real TLD (Python module names such as `io`, `app` and `net` all are): in Python source,
  a spaced assignment to a bare dotted module chain is code. A quoted host, a YAML `host:`
  value, and a shell assignment anywhere are all still checked.
- `.test` and `.invalid` are reserved TLDs but also ordinary file suffixes, so they now
  count only inside a URL: `config.test` in prose is a filename, while the same name after
  a scheme is a host. "Inside a URL" is now judged per occurrence rather than per line,
  so the two can appear together and still be told apart.
- Recorded the checker's one deliberate blind spot in the code and in CONTRIBUTING.md: an
  `internal` label under an uncommon gTLD is not detected, because widening the rule brings
  the code false positives back.

## 1.6.1 (2026-08-11)

Fourth review round.

- The published-content scan exempted any hostname wrapped in backticks — which is how
  this repository normally writes hostnames, so the most likely spelling of a leaked
  internal host was the one spelling the gate passed over. The exemption is gone; a dotted
  name is now judged by whether its last label can be a TLD, which covers the code-path
  false positives without creating a hole. CONTRIBUTING.md says so explicitly.
- The publication gate now has its own tests (`scripts/test_validate.py`), table-driven
  over what must and must not be flagged, and CI runs them. Reinstating the backtick
  exemption fails five of them.
- The placeholder-payer-name guard no longer applies to a name the operator passed
  explicitly: a company legitimately called "Example ..." could not pay at all, and the
  error told them to do the thing they had already done.
- The placeholder and cross-border checks now run BEFORE the duplicate check, so a
  configuration error is reported without first spending a round of network requests.
- SKILL.md notes that `printf` in the `pcurl` helper must be the shell builtin: the token
  is an argument to it, so `/usr/bin/printf` would put the token back into a process's argv.

## 1.6.0 (2026-08-11)

Final review round.

**Documentation taught the insecure pattern**
- Every `curl` example in SKILL.md put the token in `-H "Authorization: Bearer ..."` —
  exactly the argv exposure the scripts were fixed to avoid in 1.4.0. Two of them used the
  `bank.paysera.com` session token, which is not scope-limited and is more dangerous than
  the PAT. All now pass the token on stdin, and the slash command tells the agent the same.

**Cross-border transfers**
- Every non-Lithuanian beneficiary was declared to the API as a **natural person**,
  hard-coded. A transfer to a foreign company therefore misdeclared it on a regulated
  payment message. Added `--beneficiary-type natural|legal`, required for cross-border
  transfers and never guessed; the value is printed before the payload.
- Non-SEPA transfers now check for `--beneficiary-bic`, `--beneficiary-address` and
  `--beneficiary-city` before sending, instead of failing afterwards on a `mapper_*` error.
- The beneficiary address is no longer trimmed silently — a dropped city or postcode can
  get an international wire refused.

**Other correctness**
- The tool refuses to send a placeholder account label as the payer name; the label is what
  the beneficiary sees, so an unedited config would have shown "example — replace me".
- The duplicate amount match now compares currency too: 100.00 USD is not a duplicate of
  100.00 EUR.
- `--perform-at +Nh` in the last minutes before midnight can no longer change where the
  transfer is signable without saying so.
- `cancel-payment.py` validates the transferHash shape before putting it in a URL path.

**Checks and tests**
- The published-content scan no longer flags import paths, filenames or file paths that
  merely contain `internal`/`test`; CONTRIBUTING.md describes the real behaviour.
- The validator reports an incomplete manifest as an error instead of a `KeyError`, and
  now catches `tags`/`keywords` drift between the two manifests (which had already drifted).
- Tests added for the write-ahead ledger ORDER (the 1.5.0 fix, previously unpinned —
  removing it now fails 4 tests), `resolve_payer()`'s refusal paths, `list_transfers()`
  direction and pagination, the timezone fallback itself, and address clipping.
- CI runs the suite on Python 3.8 as well as 3.11; 3.8 is the claimed minimum and the only
  configuration where the built-in timezone fallback is exercised.

## 1.5.1 (2026-08-11)
- Added a test suite for both scripts (89 tests): the Vilnius day boundary and every
  scheduling path, beneficiary IBAN selection, invoice-id matching, purpose clipping,
  the ledger state machine including the unconfirmed-attempt block, token handling
  (asserting the token never reaches argv), transport failures, and the cancel gate
  (dry-run never issues a DELETE; a terminal or unreadable transfer is never deleted).
- CI no longer tolerates "no tests collected", and runs the suite a second time without
  pytest, so a green pipeline cannot be reached by losing the tests.

## 1.5.0 (2026-08-11)

Second review round.

**Duplicate payments**
- The ledger row is now written **before** the POST, not after. Previously a request that
  was sent but never answered (timeout, killed process) left a draft on the server that
  nothing recorded — and `GET /transfers` does not list unsigned drafts, so neither dedup
  source could see it. A retry, e.g. an hourly cron, created a second signable draft for
  the same invoice. An unanswered attempt is now kept as `unknown`, blocks further runs,
  and prints instructions to check the Paysera app. A definite HTTP 4xx is recorded as
  `failed` and does not block a legitimate retry.
- `--force` with an invoice id now says on stderr that the duplicate check was skipped
  entirely. It was the only bypass in the script that stayed silent.
- The invoice-id match against a transfer purpose requires token boundaries and a minimum
  id length. `12` or `A1` previously matched unrelated purposes and refused good payments.

**Scheduling**
- The future-dated schedule line printed the day in UTC while every other date decision
  used Vilnius, so a run between midnight and 03:00 printed both the execution day and the
  deadline day one day early — the deadline appearing already past.
- `--perform-at +Nh` is now held inside today like every other same-day path. `+6h` at
  20:00 previously rolled `operation_date` to tomorrow, hiding the transfer from the
  mobile app, which is the opposite of what the help text promised.
- The "no window left" messages said "past 23:00" when the cutoff is 10 minutes earlier.

**Validation**
- `--currency` is normalised and checked once, so `--currency eur` no longer selects the
  instant rail while sending `"eur"` in the payload and the ledger.
- `--charge-type` accepts only `sha` and `our`, the two values the API takes.

**Publication checks**
- `scripts/validate.py` now scans the whole repository, not just `plugins/`. The
  marketplace catalogue duplicates each skill description verbatim, and the README is just
  as public; neither was being checked.
- Added detection of internal hostnames on non-Paysera domains (`intranet`/`internal`
  labels, `.local`/`.lan`/`.corp`/... suffixes) and of issue-tracker/forge URLs. The host
  allowlist alone could only see `paysera.*`.
- CONTRIBUTING.md now states exactly what the automated check covers and what only a human
  can catch, and its test command matches CI.

## 1.4.0 (2026-08-10)

Security and correctness fixes from the pre-publication review.

**Security**
- The PAT is no longer passed to `curl` as a command-line argument, where any local user
  could read it from `ps` or `/proc/<pid>/cmdline` for the duration of every request. It
  now goes to curl on stdin as a config file. A POST body goes through a `0600` temp file.
- The token is rejected if it contains a quote, backslash or newline (config-file quoting).
- The ledger directory is created `0700`, and the ledger temp file is `0600` **before**
  being renamed into place — previously the finished file was briefly world-readable.

**Scheduling**
- Without tzdata the tool used plain UTC in place of Europe/Vilnius, which is 2-3 hours
  off. A run after ~21:00 Vilnius produced a `perform_at` on the **next day**, making the
  transfer web-bank-only — the opposite of what `--today` promises. There is now a
  built-in EET/EEST fallback following the EU DST rule, and a note when it is in use.
- Past 23:00 Vilnius there is no same-day window left; `--today` now falls back to ASAP
  (which keeps `operation_date` on today) instead of returning a next-day timestamp.
- `compute_schedule()` now judges "is this date past?" in Vilnius, as `parse_perform_at()`
  already did. The two disagreed between 21:00 and midnight UTC.

**Reliability**
- Every request now has a 30-second timeout and checks curl's exit status. A missing curl,
  a timeout, or a non-zero exit is reported on stderr instead of being indistinguishable
  from an empty API response.
- An incomplete live duplicate check now warns loudly before falling back to the ledger.
- A transfer whose `purpose.details` is null no longer crashes the duplicate check, and a
  null `amount` no longer crashes `cancel-payment.py`.

**Validation and messaging**
- A purpose over the 140-char SEPA limit now warns and shows exactly what was dropped —
  silent truncation could remove the invoice reference the payee reconciles on.
- `--amount` rejects `Infinity`, `NaN`, more than 2 decimal places, and implausibly large
  values instead of passing them to the API.
- An empty `--iban` now gives a clear error instead of an `IndexError`.
- An unmapped `--buyer-code` used with an explicit `--payer` now prints a NOTE saying the
  wrong-account guard did not run.
- Corrected the `--also-iban` help, which claimed those IBANs are never paid (a Paysera
  IBAN there does become the payee), and the `--perform-at` help, which misstated both the
  default and whether same-day is allowed.

**Removed**
- `phone_utils.py` and its tests: nothing in the plugin referenced them.

## 1.3.2 (2026-08-10)
- First public release on https://github.com/paysera/skills.
- Documented Personal Access Token creation against the public PAT API, replacing the
  previous pointer to an internal-only tool.

## 1.3.1 (2026-07-08)
- Fixed `list_transfers` in create-payment.py: the live dedup cross-check was querying the wrong direction — `debit_account_number` returns INCOMING transfers, so it never saw an executed OUTGOING duplicate. Now uses `credit_account_number` (accounting semantics: the payer is the credit side).
- Replaced the broken server-side cursor pagination (`_metadata.cursors.after` re-returns the same window then reports has_next=false, silently truncating) with `offset` pagination plus id-based dedupe, so a busy payer account is no longer capped at one page.
- SKILL.md: corrected the `GET /transfers` filter documentation to the verified direction semantics (credit = payer/outgoing, debit = beneficiary/incoming).

## 1.3.0 (2026-07-03)
- SEPA-zone detection, purpose-length trimming, exact-match buyer-name fallback, and extra
  non-blocking transfer states in create-payment.py.
