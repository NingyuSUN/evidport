# EvidPort — project index

## CI resource cleanup — 2026-11-10 (UTC)

EvidPort's first published revision `ed5dd1826becca992a7e9f0bf5474f90b9c1df4d`
passed [its remote checks](https://github.com/NingyuSUN/evidport/actions/runs/42782858570)
with 256 tests. The validator's first integration run also passed, but inspection
of its Python 3.13/3.14 logs exposed unclosed acquisition SQLite connections.
SQLite's transaction context commits or rolls back; it does not close a connection.

The follow-up explicitly closes each acquisition connection after transaction exit,
including error paths, and tests closure and rollback using real connections.
The existing Store context already closed its connections. No original document,
conversion, identity rule or review decision is changed by this lifecycle fix.
CI now checks Python 3.12 and 3.14, and treats resource leaks and unhandled test
thread/destructor exceptions as errors. Warnings are not suppressed.

Local Python 3.12 validation passed all 265 tests (the original 256 plus nine
connection-lifecycle cases) with those three warning classes treated as errors.
The targeted subset passed 140 tests; Ruff and the source/candidate hash comparison
passed. Remote Python 3.14 verification remains a separate check of the new commit.

The implementation and regression tests are committed together. Inspect the current
[CI results](https://github.com/NingyuSUN/evidport/actions) for this revision; prior
successful runs are historical evidence, not a claim that a later revision was tested.
Private logs and exact receipt hashes remain in the development archive.

## Publication round — 2026-11-10 (UTC)

The owner selected **EvidPort** and authorized publication followed by CI monitoring
and fixes. Tagline: **Traceable evidence for AI agents.** The target repository is
[NingyuSUN/evidport](https://github.com/NingyuSUN/evidport).
The package `local-evidence-database`, module `evidence_database`, and `evidence-db`
CLI retain their existing names for compatibility. Local data paths remain user-selected.

This round starts from local publication commit `d7c93cf8fb6c2dd1d44ea931908caec8fb3817f8`.
Only public naming and publication metadata are updated before the first push;
the engineering results below are the completed pre-publication checks. Remote CI
is not yet verified at this record. Actual commit and synchronization are queried
from Git and [Actions](https://github.com/NingyuSUN/evidport/actions), not inferred
from a configured remote. Publish this repository before the validator integration.

The earlier preparation record is preserved below. Detailed local release receipts
remain outside this public checkout; cloud delivery is not applicable. Runtime
originals, extraction versions and human review records are unchanged by naming.

Updated: 2026-11-10T07:47:06.073273+00:00 (UTC). Validation evidence was finalized on 2026-11-10.

## Current objective and code provenance

Prepare a portable, independently installable evidence layer for paper acquisition,
versioned extraction, original-page location and quotation-specific review.
The execution location is this checkout. Runtime databases are user-selected local
folders; no personal workstation or server path is required. Cloud delivery is not
applicable to this round.

- Development baseline: `a17a39ceb83c647d8911c1e02e793d0c754877b9`.
- Inputs: a frozen, reviewed working-tree snapshot above that baseline, including
  source, tests, package metadata, public workflow documentation and a synthetic
  project configuration. The private receipt records each exact input/output hash.
- Final development source commit after the frozen tests: `dea178e7acf4fa9fa1e181c6d77242bc8656c75e`.
  The earlier baseline and private population hashes above are preserved as provenance.
- Publication branch: `main`, in a separate repository with fresh publication history.
  The original development repository and its history remain intact.
- Planned repository URL (updated for selected name): https://github.com/NingyuSUN/evidport . This is the
  intended publication target, not evidence that a remote repository or commit exists.
- Public entry points: [README](README.md), [workflow guide](docs/PROJECT_WORKFLOW.md),
  [project example](examples/project.json), `src/evidence_database/` and `tests/`.

## Verified engineering results

| Scope | Result |
|---|---|
| Independent evidence library, complete local test suite | 256 passed; 0 failed, 0 errors, 0 skipped |
| New validator integration tests with synthetic originals and reviews | 44 passed; 0 failed, 0 errors, 0 skipped |
| Selected existing validator core and repository checks | 220 passed; 0 failed, 0 errors, 0 skipped |

The test processes explicitly prioritized the two candidate source directories.
Both candidates passed Ruff; the validator passed mypy across 16 source files.
Both built a wheel and source archive offline. Separate wheel installation directories
passed import-location assertions and four CLI checks, using the existing validator
dependency environment. The checks covered library help, validator profiles, one
accepted record and one rejected record. No historical large benchmark was replayed.

These are engineering checks. They do not establish transcription accuracy across
papers, scientific support for a claim, hallucination reduction, or clinical validity.
Synthetic review approvals are test fixtures, not human approval of real evidence.

## Limits and publication state

- A fresh environment with the complete frozen production dependency set was **not
  verified**: required versions were unavailable in the offline cache. No network
  installation was used to bypass that limitation.
- CI YAML structure, the explicit dependency-import guard and locked PDF dependency
  export were checked locally. GitHub workflows and the remote platform matrix have
  **not run** in this round. No remote CI success or remote synchronization is claimed.
- Builds and wheel checks preceded this final index-only update. Code and CI are
  unchanged by this update. Rebuild source archives before distributing artifacts
  that must include the final index and pinned CI configuration.
- No push or remote publication was performed in this preparation/validation round.
  Commit and publish the evidence repository first; then pin the validator CI to that
  exact evidence commit before committing and publishing the integration branch.
- Detailed test logs, package hashes, source/candidate checksums and preparation
  receipts are private records outside these public checkouts. Runtime originals,
  local stores and parser output belong in ignored directories.

## Git status and next action

At this record, the validated publication candidate is awaiting its scoped local
commit; no candidate commit SHA is invented here. Query the actual checkout after
commit with `git log -1 --format=%H`, and query this index's own revision with
`git log -1 --format=%H -- PROJECT_INDEX.md`. Verify any remote relationship separately.
The next action is local review/commit, followed by the explicitly ordered publication
steps above. Do not infer scientific admission from an installation or test result.

## Preparation history — 2026-11-10

The initial candidate was prepared from the development baseline above with an
explicit public-file allowlist. Internal machine indexes, planning documents, tools,
private papers and runtime stores were excluded. That preparation step had not yet
run tests; the completed validation results are recorded above. The final index-only
update is retained separately in a private document-change receipt.
