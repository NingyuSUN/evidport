# EvidPort

Traceable evidence for AI agents.

A local evidence layer that connects paper acquisition, versioned extraction, original-page
locations and quote-specific review. Missing full text stays visible as a DOI download task;
bibliographic metadata never becomes a fabricated quotation.

For compatibility, the distribution `local-evidence-database`, Python module `evidence_database`,
and `evidence-db` CLI keep their existing names.

## Workflow

```text
DOIs / bounded literature search
  -> free PDF candidates / manual-download list
  -> original bytes + identity checks + SHA256
  -> pinned extraction versions + page locations + warnings
  -> quote-specific original-page review
  -> BioAI Evidence Validator or another consumer
```

New originals and conversions preserve older versions. A citation pins one conversion and block;
new versions do not inherit old approvals. Main articles and supplements have separate identities.
The library uses Python and a CLI; MCP packaging is not implemented.

## Install and continue a project

Python 3.11+. Work in a local directory outside cloud-sync folders.

```sh
python -m pip install -e '.[pdf,dev]'
evidence-db project --config examples/project.json sync --fetch
evidence-db project --config examples/project.json status
```

Edit [examples/project.json](examples/project.json) first. Paths resolve relative to that file.
Each run creates a receipt and a pending CSV/TSV/JSON list. Save acquired PDFs in the configured
inbox as `paper_<id>.pdf`, using the exact suggested filename, then run `sync` again without
`--fetch`. Unknown files stay untouched. Existing selected conversions are reused.

See [Project workflow](docs/PROJECT_WORKFLOW.md) for configuration, manual intake and validation.
[PROJECT_INDEX.md](PROJECT_INDEX.md) is the project entry point.

## Locate and check evidence

```sh
evidence-db --db data/evidence search 'sample size'
evidence-db --db data/evidence show --citation 'evidence:CONVERSION_ID#BLOCK_ID'
evidence-db --db data/evidence packet --citation 'evidence:CONVERSION_ID#BLOCK_ID' --out artifacts/review-001
evidence-db --db data/evidence check --citation 'evidence:CONVERSION_ID#BLOCK_ID' --quote 'Exact quotation'
```

The review packet compares extracted text with physical PDF pages, including repository covers.
Uncertain geometry is marked page-only/partial. A person must inspect the original before
recording a human review; `check --inspect` tests text presence and never admits evidence.

## Validator integration

Install both local packages or their wheels in the same environment, then opt in:

```sh
bioevidence validate record.json --profile literature-claim --evidence-library data/evidence
```

The adapter binds the PDF hash, publication identifier, exact citation and quotation, opens the
store read-only and preserves the strict review gate. Without the new flag, the validator's
established workflow and reports remain unchanged. Historical benchmarks are not rerun.

## Verification and limits

```sh
python -m pytest
python -m ruff check src tests
evidence-db --db data/evidence audit
```

Tests exercise synthetic identity mistakes, corrupted artifacts, stale versions, review/revoke
behavior and workflow resumption. Private papers, databases and model weights are not included.
The core needs no model API or GPU; the PDF extra supplies acquisition and baseline extraction.
Other extraction tools run separately or use explicitly configured endpoints/import adapters.

This local single-user system does not guarantee exhaustive literature coverage, faithful
transcription, scientific validity or a measured reduction in hallucinations. Reviewer identity
is locally self-declared. Hashes do not protect against an administrator rewriting the whole store.
Read-only access supports SQLite DELETE mode; WAL mode is refused to avoid sidecar-file writes.

Code: [Apache-2.0](LICENSE). Dependencies, models and papers retain their own licenses.
