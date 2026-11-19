# EvidPort — project index

## File identity rule version 3 — 2026-11-19 (Asia/Tokyo)

Objective: correct one wrong refusal and one wrong acceptance in the check that a PDF is the paper
it was fetched or attached for. Both were found on real files during a private agent run on
2026-11-18. Merged to main on 2026-11-19 with [PR #5](https://github.com/NingyuSUN/evidport/pull/5).

| Field | Scope and evidence |
|---|---|
| Wrong refusal | Two official open-access PDFs were refused with `front_page_doi_mismatch`. Their text layer held the paper's DOI a second time, cut at a line-end hyphen in one and run into the following word in the other, and the rule read that string as another paper's identifier. A string of exactly one of these two shapes is now tolerated, and only when the exact DOI is on the same page; it is recorded in `doi_layout_variants_tolerated`. Any other DOI on the page still refuses the file. |
| Wrong acceptance | A two-page degree record (thesis summary and examination sheet, in Japanese) carrying the paper's title and DOI was accepted as the paper. A file offered as a paper whose first page has degree-record wording (`学位論文`, `博士論文`, `論文審査`, `学位论文`, `博士论文`) now stays pending with `degree_record_identity_requires_review`. |
| Pending list | `export_pending` adds `pmcid` and `free_locations_tried` (the addresses the queue already tried), so the person downloading does not repeat them. |
| Code and tests | `src/evidence_database/acquisition.py`, `tests/test_acquisition.py` (7 new cases). `identity_rule_version` is now 3. 412 tests passed locally on Python 3.12; Ruff and the public-tree check passed. CI passed on Python 3.12 and 3.14 for `fa5ef4b`. |
| Check on the real files | The three files kept from the run were inspected again under the new rule on a second machine: both refused PDFs are accepted with the tolerated string recorded, and the degree record goes to identity review. |
| Consumers | Bio-AI Evidence Validator confirms a publication's identity only for rule versions it lists. A Validator that lists version 2 only reports papers checked under this revision as having no confirmed identity. Its main branch accepts versions 2 and 3 since [its PR #50](https://github.com/NingyuSUN/bioai-evidence-validator/pull/50), merged the same day, and pins `fa5ef4b` of this repository. Papers already in a library keep the identity record they were accepted with; nothing is re-checked. |
| Limits | Each change rests on one or two real files. Degree records in other languages, including English thesis covers, are not recognised. The supplement rule is unchanged: a paper whose first page has a line beginning "Supplementary information" is still refused as a supplement, which was a wrong refusal for one journal PDF in the same run. Whether existing libraries hold a degree record accepted under version 2 was not checked. |

## Quotations taken from stored text — 2026-11-18 (Asia/Tokyo)

Objective: stop losing real quotations because whoever typed them changed them. Work is on the
local branch `quote-by-position`; see Git for whether it has been committed or published.

| Field | Scope and evidence |
|---|---|
| Finding that prompted it | In a private study of 92 model-written claims on three papers, 50 of 149 typed quotations were not found in their cited block by the exact check: about 17 only joined a word broken at a line end or changed a dash or quote style, at least 7 wrote out a symbol the text layer had stored as a control character, the rest were reworded or stitched with an ellipsis. |
| Change | `check` stays exact. New: a block's sentences can be numbered and a quotation taken by position (`Store.sentences`, `Store.sentence_quote`; `show --sentences`, `check --sentences 2-3`; `sentences` on `evidport_evidence_get`, `first_sentence`/`last_sentence` on `evidport_evidence_check_quote`). A typed quotation that is not found returns `suggested_quotes`: the stored text of the one place it corresponds to, with what differed. Letters and digits are never substituted; an ambiguous place or a reworded quotation gets no suggestion; the typed quotation itself is never admitted. The tool count is unchanged. |
| Code | New `src/evidence_database/quoting.py` and `tests/test_quoting.py`; changes in `store.py`, `mcp_evidence.py`, `mcp_server.py`, `cli.py`; guides updated. |
| Measured effect | Re-running the same 149 stored quotations: 28 of the 50 unfound ones resolve to stored text (32 stored quotations, since stitched ones split), 22 do not. Of the 28, 15 are then admitted by independent reading; the others are found but held for a person, mostly because the stored text contains a control character. Claims needing no person at all rise from 27 to 31 of 92 under the stored semantic review, with no known problem claim among them. |
| Limits | The effect of quoting by position cannot be measured on stored model output; it needs new generation. The 22 unresolved quotations are reworded and only position quoting avoids them. The study's reference labels were made by AI reviewers, not experts, and three papers is a small sample. A resolved quotation was not re-read by the semantic reviewer. |

The historical entries below retain the evidence for earlier revisions.

## Machine verification of quotations — 2026-11-17 (Asia/Tokyo)

Objective: let the system settle the transcription check that a person otherwise does for every
quotation, and leave a person only what it cannot settle. Work is on the local branch
`independent-reading-verification`; nothing is committed, pushed or published at this record.

| Field | Scope and evidence |
|---|---|
| Change | The baseline conversion can record an OCR reading of every paragraph's rendered page region, the position of superscripts and letters set in symbol fonts. `check` reports `machine_verification` and, when the project sets `verification.admission` to `independent_reading`, admits a quotation that both readings agree on and that touches no known text-layer defect. The default remains human review only. See [machine verification](docs/MACHINE_VERIFICATION.md). |
| Code | New `src/evidence_database/verification.py`; changes in `adapters.py` (adapter version 2), `store.py`, `workflow.py`, `acquisition.py`, `mcp_papers.py`, `mcp_evidence.py`, `mcp_server.py`, `cli.py`; new `tests/test_verification.py`. Block text, citations, the review ledger and human approval rules are unchanged. Machine verification writes no review record. |
| Tests | 405 passed locally on Python 3.12 (375 existing, 30 new) with resource warnings as errors; Ruff and the public-tree check passed. Python 3.14 and remote CI have not run on this branch. |
| Measurement before the change | 11 text-layer papers, 3,036 paragraph blocks, 127,254 words. Text layer and Tesseract 5.5 agreed on 95.2% of words. A fixed-seed sample of 59 short disagreements was judged against the page by an AI assistant: the text layer had wrong characters in 9, giving an estimated word accuracy near 99.5% (roughly 99.3% to 99.7%). The errors were systematic: minus signs stored as control characters, a ligature stored as another letter, detached accents, zero-width characters, lost word spaces, flattened superscripts. |
| End-to-end run after the change | 12 library papers re-extracted into a separate scratch store, 3,052 regions read by Tesseract 4.1.1 on a second machine, no failed readings. Of 4,553 sentences of eight or more words, quoted as the block gives them, 3,144 (69.1%) were admitted without a person under the first rules. After the typography fix and the symbol-font rule below, re-evaluated without new OCR: 3,287 (72.2%), 87.9% of sentences without digits and 58.0% with digits. The rest are held back by reading disagreement (724), superscripts (368), symbol-font letters (73) and detected defects. Tesseract 5.5 readings of the same regions gave nearly the same rate as 4.1.1 (70.1% against 69.7% under the first rules). |
| Accuracy of what was admitted | All 3,144 sentences admitted under the first rules were read a third time with RapidOCR and compared line by line: 2,277 agreed in all three readings, 457 were not fully read by the third engine, 410 differed. Of 200 differing places chosen for a look (all 177 that were not look-alike glyphs or punctuation, plus 23 look-alike ones), 198 were located on the page: the text layer was right in 197. The one error was a Greek alpha extracted as `a` from a symbol font and read the same by Tesseract. A font scan then found the same class elsewhere in the library (alpha, beta, micro as `m`, registered sign, small capitals). The symbol-font rule now holds these back; both known alpha errors are held, with 14 other sentences as collateral. |
| Recorded acceptance quotations | Of the 12 real quotations from the 2026-11-11 run, 4 were admitted by agreement and 8 stayed pending; all 12 fabricated quotations were rejected. Several of those 12 are figure labels or journal footers rather than body sentences. |
| Limits | Agreement is not proof; the check above covers 12 papers, and 157 look-alike differences and 53 punctuation-only ones were not compared with the page. All page comparisons were made by an AI assistant, not a human reviewer. Sentence-level rates describe whole sentences; an agent can often quote a span that avoids a citation mark. Scanned pages, tables and rotated pages are not machine-verified. The Validator support is on a separate local branch of that repository and is not merged. |
| Records | Measurement scripts, crops, readings, the scratch store and reports are local, under ignored `artifacts/`. The original library was opened read-only and its file listing and index hash were unchanged afterwards. |
| Next | Owner review of this branch and of the Validator branch `independent-reading-route`; commit both; enable `independent_reading` in a project and re-convert its papers. |

## Local MCP publication — 2026-11-11 (Asia/Tokyo)

This revision merges the local stdio MCP interface over the existing acquisition,
extraction and review workflow. The reviewed public candidate starts from `a6f26ff` and contains portable
code, tests, configuration examples and documentation. Papers, databases, machine-specific
settings and detailed private receipts are excluded.

| Field | Scope and evidence |
|---|---|
| Inputs and implementation | Existing evidence-store and acquisition APIs; official Python MCP SDK 2.3.0 as an optional dependency. The MCP server provides 13 tools and 4 resource templates. It does not import Validator or create human approvals. |
| Recorded local acceptance | Nine distinct searchable papers were checked; three were replayed through isolated DOI/manual-intake flows. The 12 extraction flows produced 12 matching real quotations and rejected 12 fabricated quotations, with zero automatic admissions. Two other candidate identities remained pending. See the [acceptance summary](evaluation/mcp-local-20261111.json). |
| Consumer checks | Three actual Validator MCP cases produced the expected outcomes: a real unreviewed quotation required review; a fabricated quotation and incorrect source hash were rejected. Source files and existing review records remained unchanged. A Claude Code CLI session also completed status/search/get/check calls. |
| Validation status | The public candidate passed 372 tests before the final three publication-guard cases were added; all 19 guard cases then passed, covering 375 distinct tests in total. Ruff, the public-tree check and package build passed. The development-only corpus harness is excluded. Both Python 3.12 and 3.14 passed all 375 tests, with no failures, skips or test warnings, in [PR CI run 42982805563](https://github.com/NingyuSUN/evidport/actions/runs/42982805563) for `63ce5dbef394f38abea13c7ef51499ae75feb1fa`. The merged revision has separate [main checks](https://github.com/NingyuSUN/evidport/actions/workflows/ci.yml?query=branch%3Amain). |
| Public entry points | [README](README.md), [MCP usage](docs/MCP_USAGE.md), [agent reading rules](docs/AGENT_USAGE.md), [project workflow](docs/PROJECT_WORKFLOW.md), [project example](examples/project.json), source and tests. All configuration examples use repository-relative inputs. |
| Limits | Known-positive retrieval and historical provider metadata were used. This round did not measure literature-search completeness, live-network acquisition, general transcription accuracy, scientific support or hallucination reduction. Partial extraction and uncertain identities retain their warnings. Desktop configuration approval/reload and multi-user deployment remain separate validation tasks. |
| Git and publication | Regular two-parent merge of [PR #1](https://github.com/NingyuSUN/evidport/pull/1), preserving the reviewed commit and existing public history. Runtime files are unchanged from the tested PR; this index records the merge. Post-merge main CI must be checked for its exact revision. Query the exact index revision with `git log -1 -- PROJECT_INDEX.md`. Existing public history is retained; private development history is not published. |
| Next | Verify the merged main revision, then pin Validator to this public commit and check its CI. Cloud delivery is not applicable. |

## Historical records

The records below describe earlier revisions and preparation stages. Their pending states,
counts and next actions are historical; the current objective and scope are recorded above.

### CI resource cleanup — 2026-11-10 (UTC)

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

### Publication round — 2026-11-10 (UTC)

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

### Current objective and code provenance

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

### Verified engineering results

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

### Limits and publication state

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

### Git status and next action

At this record, the validated publication candidate is awaiting its scoped local
commit; no candidate commit SHA is invented here. Query the actual checkout after
commit with `git log -1 --format=%H`, and query this index's own revision with
`git log -1 --format=%H -- PROJECT_INDEX.md`. Verify any remote relationship separately.
The next action is local review/commit, followed by the explicitly ordered publication
steps above. Do not infer scientific admission from an installation or test result.

### Preparation history — 2026-11-10

The initial candidate was prepared from the development baseline above with an
explicit public-file allowlist. Internal machine indexes, planning documents, tools,
private papers and runtime stores were excluded. That preparation step had not yet
run tests; the completed validation results are recorded above. The final index-only
update is retained separately in a private document-change receipt.
