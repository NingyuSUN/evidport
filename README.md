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
The Python API and CLI remain available. An optional local MCP server exposes this same
workflow to AI clients, preserving the source checks and human-review gate.

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

## Local MCP for AI clients

Install the optional MCP and PDF dependencies in the server environment (Python MCP SDK 2.3.0):

```sh
python -m pip install -e '.[mcp,pdf]'
evidport-mcp --project-config examples/project.json
```

Run the example from this repository after editing its project configuration. Activate the
environment containing EvidPort before launching a client that uses the supplied `python` command.

The client starts this command as a local stdio subprocess. The default is offline; an operator
can add `--allow-network` at startup to enable the existing metadata and free-PDF providers.
Thirteen tools cover paper discovery, DOI pending lists, inbox imports, extraction, evidence
retrieval and quote checks; four resource templates provide blocks, continuation chunks,
original page images and review packets. Long operations return a job ID to poll.

Follow [MCP usage](docs/MCP_USAGE.md) for configuration and the complete manual-download-to-
Validator workflow, and [agent reading rules](docs/AGENT_USAGE.md) for evidence use.
The server has no human-approval tool. Successful protocol calls do not
establish scientific validity or prove a Desktop client has loaded its configuration.

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
established workflow and reports remain unchanged. Enabling the adapter does not change
historical benchmark records.

The optional Validator MCP consumer retrieves exact citations through the server and independently
checks them against this same local library. It rejects protocol failures and disagreements instead
of trusting a server's admission claim. See [MCP usage](docs/MCP_USAGE.md#validate-a-proposed-record).

## Verification and limits

```sh
python -m pip install -e '.[pdf,mcp,dev]'
python -m pytest
python -m ruff check src tests
evidence-db --db data/evidence audit
```

Tests exercise synthetic identity mistakes, corrupted artifacts, stale versions, review/revoke
behavior and workflow resumption. Private papers, databases and model weights are not included.
The [MCP acceptance summary](evaluation/mcp-local-20261111.json) records the separate real-paper
check: nine distinct papers, including three repeated through manual intake. This is engineering
validation of retrieval and quotation checks, not a scientific accuracy benchmark.
The core needs no model API or GPU; the PDF extra supplies acquisition and baseline extraction.
Other extraction tools run separately or use explicitly configured endpoints/import adapters.

This local single-user system does not guarantee exhaustive literature coverage, faithful
transcription, scientific validity or a measured reduction in hallucinations. Reviewer identity
is locally self-declared. Hashes do not protect against an administrator rewriting the whole store.
Read-only access supports SQLite DELETE mode; WAL mode is refused to avoid sidecar-file writes.

Code: [Apache-2.0](LICENSE). Dependencies, models and papers retain their own licenses.
