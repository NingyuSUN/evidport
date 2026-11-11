# EvidPort project workflow and consumer integration

EvidPort joins acquisition, local intake and extraction without duplicating the evidence
store. It works offline unless `sync --fetch` is requested. The optional
[local MCP interface](MCP_USAGE.md) exposes the same project and review boundaries.

## Configure and resume

Start with [examples/project.json](../examples/project.json). Required fields are `schema: 1`,
`project_id`, separate `store`, `queue`, `inbox`, `runs` directories, and a nonempty `papers` list.
Relative paths resolve from the configuration file's directory. Keep private configurations
in ignored `data/` or `artifacts/`, outside the four managed directories.

Each paper has a `metadata` object with a DOI, PMID or provider-specific record identifier.
Use the bibliographic title and retain its provenance. Optional paper fields are:

| Field | Purpose |
|---|---|
| `file` | An explicit local original; no ambiguous filename matching |
| `sha256` | Expected bytes of that file; mismatch prevents intake |
| `conversion_id` | An explicit existing conversion of the verified original |

`metadata.document_id` preserves an existing local document identifier. The `role` distinguishes
main articles and supplements; a supplement needs a distinct `attachment_id`. A selected
conversion must belong to the exact source revision and must not be failed. There is no
automatic "latest is best" selection policy.

```sh
evidence-db project --config data/project.json sync --fetch
evidence-db project --config data/project.json status
```

Sync registers declared candidates, optionally attempts free PDF locations, checks explicit
files, imports matching inbox files and parses acquired originals. `--no-parse` defers new
parsing. No configuration field executes arbitrary commands or starts external services.
Repeated sync retains an existing successful or partial conversion. For deliberate reparsing,
use `convert` and select its returned conversion ID. New versions never inherit prior reviews.

## Manual download loop

Each run creates `result.json` and a fresh `pending/` directory under `runs`. The table includes
DOI/PMID, title, exact suggested filename and reason:

- `need_pdf`: obtain the original and save it to `inbox` as `paper_<id>.pdf`.
- `verify_downloaded_file`: bytes are already retained, but identity needs examination.

Run `sync` without `--fetch` after adding files. Inputs are never moved or deleted. Unknown
inbox files remain untouched. Pending exports include only the configured project's papers,
even when projects share a queue.

Identity checking is conservative: it compares frontmatter titles/identifiers, recognizes
limited repository cover sheets and rejects reference-only or supplement matches. Scans,
short titles and conflicts can remain pending. A general human identity-adjudication interface
is not implemented; a successful older OCR run is not an automatic identity approval.

## Individual acquisition commands

```sh
evidence-db --db data/evidence papers --queue data/acquisition discover --provider europepmc --query 'endometrial receptivity' --max-results 100
evidence-db --db data/evidence papers --queue data/acquisition resolve --doi 10.1126/science.aay6690
evidence-db --db data/evidence papers --queue data/acquisition fetch --all --parse
evidence-db --db data/evidence papers --queue data/acquisition pending --out artifacts/pending-001
evidence-db --db data/evidence papers --queue data/acquisition import-pdf --paper PAPER_ID --pdf paper.pdf --parse
```

Europe PMC and Crossref supply metadata; DOI resolution may add available PDFs from the PMC
cloud dataset. Institution-specific links can be registered with source, version and access
evidence. The implementation does not crawl every repository or guarantee complete retrieval.
Free access is not necessarily an open redistribution license. Missing links and network errors
are not proof of a paywall. Saved query, provider, time, hashes, counts, caps and errors document
the search scope, not relevance or completeness. Provider query syntax may differ.

## Validator contract

Install EvidPort and BioAI Evidence Validator in the same environment, then:

```sh
bioevidence validate record.json --profile literature-claim --evidence-library data/evidence
```

The adapter is optional and separate from the validator's older embedded archive option.
No unpublished package is silently downloaded from an index.

| Validator field | Bound library value |
|---|---|
| `source_artifacts[].sha256` | Exact original PDF SHA256 |
| `source_artifacts[].uri` | DOI/PMID/PMCID confirmed in acquisition receipts |
| `source_artifacts[].title` | Title consistent with frozen metadata |
| `evidence_items[].locator` | `evidence:<conversion_id>#<block_id>` |
| `evidence_items[].extracted_text` | Exact quotation with scientific symbols and precision |

The adapter opens the store read-only. Missing identity, unreviewed or revoked quotations and
corruption produce explicit findings, even when another grounder reports a match. Historical
imports lacking sufficiently bound identity receipts remain review-required. Retraction metadata
is read from pinned responses, not refreshed invisibly during validation.

Quote review concerns extraction, not scientific truth. Domain interpretation and contradictory
evidence remain the consumer's responsibility. Enabling this adapter changes no historical
benchmark result and supplies no measured agent hallucination rate.

## Operations

Exit `0` means the requested operation completed under its contract; `1` is an input/execution
error; `2` means pending work, a current failure or partial parsing. Inspect `result.json` rather
than interpreting `2` as failure of every input. Runs are sequential and single-user, with new
receipts instead of overwritten history.

Read-only stores refuse WAL mode because SQLite can create or modify `-wal`/`-shm` on a nominally
read-only connection. The ordinary store uses DELETE mode. No global agent configuration,
background scheduler, remote deployment or human approval is created by these commands.
