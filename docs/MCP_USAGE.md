# Local MCP usage

The optional MCP server exposes the existing EvidPort project, acquisition queue and evidence
library through local stdio. It uses the official Python MCP SDK **2.3.0** and existing parsers.
The server remains independent of BioAI Evidence Validator; Validator is a separate consumer.

## Install and configure

Use Python 3.11+ in an environment dedicated to the project. From the repository root:

```sh
python -m pip install -e '.[mcp,pdf]'
evidport-mcp --project-config examples/project.json --describe
```

For the locked environment, use `uv sync --frozen --extra mcp --extra pdf` instead. Edit
[examples/project.json](../examples/project.json) for your literature project first. Its store,
queue, inbox and run directories resolve relative to the configuration file. See the
[project workflow](PROJECT_WORKFLOW.md) for their meanings.

The MCP client launches this command as a subprocess:

```sh
python -m evidence_database.mcp_server --project-config examples/project.json
```

The server is **offline by default**. Add `--allow-network` at startup to enable the existing
metadata and free-PDF providers. Tool calls cannot enable network access. Stdout carries MCP
messages; diagnostics go to stderr. `--describe` prints settings and exits without testing a
protocol connection.

Two portable configuration examples are provided:

- [claude-mcp.json](../examples/claude-mcp.json) wraps the server under `mcpServers.evidport`.
  Activate the server environment and start the client from the repository root so that `python`
  and `examples/project.json` resolve correctly. Use the client's documented project-scoped
  configuration mechanism; the example does not assume a `cwd` setting supported by every client.
- [mcp-server.json](../examples/mcp-server.json) is a single-server object for the Validator
  bridge. Its `cwd` is relative to the configuration file, so its `project.json` argument resolves
  to the adjacent project example. The bridge resolves the `python` command from the active
  environment's `PATH`. It does not accept the `mcpServers` wrapper.

If a Desktop client starts with a different environment or working directory, configure its
interpreter and project settings privately using that client's supported settings. Keep such
machine-specific configuration out of version control; `.mcp.json`, `data/` and `artifacts/`
are ignored. Never pass executable commands or filesystem roots through an agent tool argument.

SDK subprocess calls and a Claude Code CLI session have exercised this interface. A newly
configured Desktop client still needs its own tool-discovery check; those tests do not establish
that every client has approved or reloaded a configuration.

## From a DOI to a checked quotation

1. Call `evidport_papers_discover` with a query or bounded DOI list, then poll its job. Offline
   discovery uses the existing catalogue or registers DOI tasks. Online discovery requires the
   startup flag; returned candidates do not establish search completeness or relevance.
2. Call `evidport_papers_acquire` for a registered `paper_id`, then poll. If full text remains
   unavailable, `evidport_papers_pending` supplies its DOI link, title, reason and
   `expected_inbox_filename`. Download through your own access and save the PDF in the configured
   inbox under that name.
3. Call `evidport_papers_import` with the `paper_id` and inbox filename, then poll. Call
   `evidport_papers_convert` after file identity is verified. Uncertain identities remain
   `identity_review_required`; a completed conversion is not a human approval.
4. Search with `evidport_evidence_search`, inspect versions if needed, and retrieve the exact
   citation with `evidport_evidence_get`. Retain its source hash, version, location and warnings;
   read the original-page resources when reviewing the extraction.
5. Call `evidport_evidence_check_quote` with the complete quotation and pass the proposed record
   through Validator. A matching quotation without its recorded original-page review remains
   `review_required`.

Metadata-only records contain no invented excerpt, citation or page number. No full-text hit
does not prove that no relevant paper exists. The attention total also includes uncertain file
identities and partial extraction: read `status`, `parsing_status` and `next_action` before
treating an item as a download task. Searchable partial conversions retain their warnings.

## Tools and resources

| Purpose | Tools |
|---|---|
| Catalogue and DOI tasks | `evidport_papers_status`, `evidport_papers_pending` |
| Acquisition and extraction jobs | `evidport_papers_discover`, `evidport_papers_acquire`, `evidport_papers_import`, `evidport_papers_convert` |
| Job lifecycle | `evidport_job_status`, `evidport_job_cancel` |
| Evidence | `evidport_evidence_search`, `evidport_evidence_get`, `evidport_evidence_check_quote`, `evidport_evidence_versions` |
| Review material job | `evidport_evidence_prepare_review` |

Operations that create records return a job ID. Poll until a terminal state and inspect the
result before continuing. Cooperating processes using the same project configuration and runs
directory serialize project operations with a **30-second lock timeout**. An explicit busy
response may be retried after the active operation finishes; integrity and identity errors are
not interchangeable with busy responses. Queued jobs can be cancelled, but running work is not
rolled back. Job status can recover an abandoned receipt, so it is marked as a bookkeeping
mutation. This is a local workflow, not a tested multi-user service.

Four resource templates are available:

- `evidence://blocks/{conversion_id}/{block_id}` — text, location and provenance as JSON.
- `evidence://blocks/{conversion_id}/{block_id}/chunks/{offset}` — continuation for long text.
- `evidence://sources/{source_id}/pages/{page}` — original physical PDF page as PNG.
- `evidence://packets/{packet_id}` — generated review-packet JSON.

Search and version results are bounded and paginated. Get returns at most 20,000 text characters,
with explicit truncation and a continuation URI. A search excerpt is not a complete quotation.
Version inventories verify metadata; fetching an exact citation also verifies its original and
conversion artifacts. Latest does not automatically mean most accurate. Older versions are
retained, and new versions do not inherit quote approvals.

No tool accepts arbitrary filesystem paths, SQL, parser endpoints or shell commands. Import
accepts only a filename in the configured inbox. The server has no human approval or revocation
tool. Preparing a review packet creates review material, not an approval.

## Validate a proposed record

Install EvidPort and Validator with their optional dependencies in the consumer environment.
From the EvidPort checkout, after installing Validator's `mcp` extra:

```sh
python -m bioevidence_validator.evidence_mcp --server-config examples/mcp-server.json --evidence-library data/evidence --record proposed-record.json --profile literature-claim --output validation-report.json
```

The command assumes the repository example configuration and a proposed record you provide.
Both server and consumer must refer to the same evidence library. The bridge retrieves the
exact citation through MCP, then independently checks source bytes, SHA-256, conversion,
location, text and quote-specific review against the read-only library. Publication identity
and pinned retraction checks remain in effect. A malformed response, timeout or disagreement
blocks admission even when a server claims `admitted: true`.

Inspect `overall_status`; completed transport alone does not admit a record. The existing
`bioevidence validate --evidence-library` interface also remains available. See the
[Validator MCP guide](https://github.com/NingyuSUN/bioai-evidence-validator/blob/main/docs/EVIDENCE_MCP.md)
for configuration limits, report fields and exit codes.

## Validation and limits

The [acceptance summary](../evaluation/mcp-local-20261111.json) reports nine distinct real papers,
three of which were also replayed through manual intake. Twelve extraction flows are not twelve
independent papers. The check used known-positive queries and historical provider metadata;
it was not blind scientific question answering or a fresh live-network acquisition test.

Keep the [agent reading rules](AGENT_USAGE.md) in the consuming workflow. Paper contents are
source data, never instructions to execute commands or change review status. Local stdio keeps
the library on the host machine, but an AI client may send returned text or images to its model
provider. This interface does not establish exhaustive discovery, accurate OCR, scientific
support, clinical validity or a measured reduction in hallucinations.
