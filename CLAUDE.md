# List Uploader

Self-service lead-list upload: upload → map columns → analyze and fix → (optional)
enrich → send to Eloqua → confirmation. The spec is `docs/SPEC.md`; the phased plan
is `docs/BUILD_PLAN.md`.

## Hard rule: mocks and synthetic data only

This repo and the AWS account it deploys to (`brandonkeithfarris`, a personal
account) are a **build environment**, not TriNet's. See SPEC decision D15.

- No real lead data anywhere: not in fixtures, tests, the frontend mocks, S3, or
  DynamoDB. Use obviously fake values: reserved `.example` domains for lead emails
  (`ada@acme.example`), "Demo Conference 2026". Don't use `example.com`/`test.com`
  for leads: the junk rules (SPEC §14.2) flag those domains on purpose.
- No real Workato endpoints or tokens. `FakeWorkatoClient` is the only Workato
  implementation that runs here (`INTEGRATIONS=fake`).
- `send_to_prod` must be `false` outside `prod`. `BaseWorkatoClient` raises before
  any call if it isn't. Don't weaken this guard.
- The lead normalizer (`backend/shared/lead_normalizer/`) and its golden-output
  fixtures come from Brandon. Golden fixtures must be synthetic rows.
- `make deploy` only deploys `dev`. The TriNet deployment will be done later from
  TriNet's own account with the same CDK app.

## Layout

```
backend/                 Python 3.12 Lambdas (imports are rooted at backend/)
  shared/                code shared by every Lambda
    audit.py             audit event schema, EventType catalog, fail-closed writer
    observability.py     Powertools logger/tracer/metrics + PII scrubber
    workato_client.py    WorkatoClient protocol, guards, FakeWorkatoClient
    bedrock_client.py    JSON-only Claude calls (validate, retry once, degrade)
    parsing.py           CSV/XLSX -> string rows (SPEC §7.1); no type inference
    catalog.py           field catalog (SPEC §8), seed aliases, header normalization
    mapping.py           exact -> alias -> AI column mapping; confirmation rules
    config_store.py      Config table reads (aliases, thresholds) with seed fallback
    fake_ai.py           HeuristicFakeBedrock: the "AI" in dev (INTEGRATIONS=fake)
    sfdc_ids.py          campaign ID checks, 15 -> 18 checksum (SPEC §11.3)
    normalizer.py        Normalizer protocol, v5 adapter, stand-in; §11.2 field rules
    analysis.py          evaluate_row (pure): processed, provenance, issues, status
    analysis_store.py    job context <-> evaluation, per-row audit events
    ai_checks.py         AI junk detection and lead source matching
    issue_catalog.py     explanation + bulk action per issue code
    fake_sfdc.py         synthetic Salesforce campaigns for the fake Workato
    jobs.py              JobState machine; JobRepo (state change + audit in one txn)
    rows.py              Rows table access
    messages.py          all user-facing text (SPEC §20)
    config_defaults.py   thresholds and limits (SPEC §14.4)
    lead_normalizer/     normalizer v5 goes here unmodified (P3)
  bff/                   API Gateway router (Powertools APIGatewayHttpResolver)
  tasks/                 async task Lambdas (parse_file; analyze = AnalyzeWorkflow steps)
  audit_archiver/        AuditEvents stream -> Firehose -> S3 archive
  tests/                 pytest + moto
    fixtures/synthetic/  generated sample files (make fixtures); never real data
infra/                   CDK app (Python): Storage, Auth, Api, Workflows, Web
  tests/                 synth assertions (IAM, encryption, audit immutability)
frontend/                React + Vite + TypeScript SPA
  src/mocks/             MSW handlers + synthetic fixtures (the default API)
docs/                    SPEC.md, BUILD_PLAN.md
```

## Commands

```
make install     # uv sync + npm ci
make lint        # ruff, ruff format --check, mypy --strict, tsc
make test        # pytest (backend + infra) and vitest
make fixtures    # regenerate synthetic sample files + downloadable template
make synth       # cdk synth -c env=dev (bundles Lambda deps locally, no Docker)
make web-dev     # frontend on the mock API
make deploy      # dev only; needs AWS credentials for the personal account
```

Run `make lint test synth` before every commit.

## Conventions

- **Audit fails closed.** Every state-changing action writes its SPEC §21.2.3
  event through `AuditWriter`. Use `run_audited` when the action must not happen
  unless recorded. `AuditWriteError` becomes a 503 in the BFF. Never catch and
  ignore it.
- **State changes are transactional.** Change job state only through
  `JobRepo.transition`: the update and its JOB_STATE_CHANGED event (plus any
  other events you pass) commit together or not at all. `StateConflict` means
  the job moved on (409); `AuditWriteError` means nothing was written (503).
- **AuditEvents is append-only.** App roles get `grant_audit_append` (PutItem +
  explicit denies). `infra/tests/test_synth.py` fails if any role can
  update or delete audit items. Keep it that way.
- **No PII in logs or metrics.** Log through `shared.observability.logger`. Log IDs
  and counts, not values. Metric dimensions are `env`/`stage` only.
- **AI never blocks.** `BedrockClient.invoke_json` never raises; handle
  `value is None`. Output models are pydantic with `extra="forbid"` and an object
  root (wrap lists in `{"items": [...]}`).
- **User-facing text lives in `messages.py`** (backend) and is plain English that
  says what to do next.
- Each new action's acceptance includes its audit event (asserted in tests) and
  its SPEC §21.3.2 metrics.

## Upload flow (P1)

`POST /jobs` creates the job (AWAITING_UPLOAD) and returns a presigned **POST**
(S3 enforces the 10 MB limit). The browser uploads straight to S3, then calls
`POST /jobs/{id}/uploaded`: the BFF locks that exact S3 version with
`put_object_retention` (write-once), moves the job to UPLOADED, and invokes the
parse task async. The parse task reads that version, hashes it, parses, stores
rows, and moves to MAPPING_REVIEW or PARSE_FAILED. The browser polls
`GET /jobs/{id}`.

Deviations from the spec, on purpose:
- `AWAITING_UPLOAD` state before `UPLOADED`, so a job exists before its file.
- Presigned POST instead of PUT (S3-enforced size limit).
- The uploads bucket has Object Lock but no default retention; a default would
  force a Content-MD5 header on every browser upload.
- US ZIP leading-zero restore (§7.1) runs after mapping (P2/P3), since it needs to
  know which column is the ZIP and which is the country.

## Column mapping (P2)

The parse task suggests the mapping right after parsing (exact -> alias -> AI),
so the slow AI step never runs in the BFF. Sample values go to the AI prompt
only; they are never stored on the job or in audit events. `GET /jobs/{id}/mapping`
reads samples from the first Rows. `PUT` validates (known headers and fields,
one-to-one, must-map fields present) and records MAPPING_CONFIRMED plus
SUGGESTION_ACCEPTED/REJECTED per AI column. The job stays in MAPPING_REVIEW;
`POST /analyze` (P3) moves it on.

Must-map fields: company, first_name, last_name, email, campaign_id. Required
fields that may stay unmapped (`fill_when_unmapped`): lead_source, campaign_status,
list_name, campaign_name. Including list_name and campaign_status goes beyond
BUILD_PLAN's "Campaign Name and Lead Source exempt"; both have a spec-defined
fill (OQ-2, §8 footnote).

The frontend mock catalog must match `catalog.py`;
`backend/tests/test_frontend_contract.py` enforces it.

## Analysis (P3)

`POST /jobs/{id}/analyze` stores a reproducibility snapshot on the job
(ANALYSIS_STARTED) and starts AnalyzeWorkflow: Prepare (Salesforce campaign
lookup via Workato, lead source rules then AI) -> Map of junk-check batches
(100 rows, 4 in parallel) -> Finalize (evaluate every row, duplicates, write rows
and audit events, -> ANALYSIS_REVIEW). Any step failing -> FAILED, and the user can
re-run. `tasks.analyze.run_all` runs the same steps in-process for tests.

`shared.analysis.evaluate_row` is the single source of truth for a row. Row edits
(`PATCH /rows/{id}`) and bulk actions re-run it with the job's stored context, then
re-check duplicates across the file, and write each changed row with its audit
events in one transaction conditioned on the job still being in ANALYSIS_REVIEW.

Normalizer: `load_normalizer()` uses v5 from `shared/lead_normalizer/` once it's
added (a `main(inputs)` function), else `StandInNormalizer`, which is minimal and
is labelled as NOT v5 in every snapshot. The golden-output test is skipped until
v5 and synthetic golden fixtures exist.

Decisions made while building P3 (spec gaps):
- **Junk AI sees every row.** SPEC §14.2 says the AI pass skips rows already
  flagged by rules, but VALUE_JUNK needs both an AI flag and a rule hit on the same
  field, so it could never fire. A rule hit alone or an AI flag alone gives
  VALUE_SUSPECT (warning); both, with AI >= junk_block_threshold, give VALUE_JUNK.
- **Audit events are per row, not per field**: one VALUE_NORMALIZED /
  VALUE_DERIVED / VALUE_AUTO_CORRECTED / ISSUE_RAISED / ISSUE_CLEARED per row,
  listing the fields. Each carries the lead's email hash for person lookup.
- Fields derived from the campaign (lead source, status, name, list name) aren't
  reported missing while the campaign ID itself is invalid.
- NOT_SENT_FIELD is one info issue per row listing every unsent field (OQ-1).
- FIELD_FORMAT_INVALID (warning) is new: SPEC §11.2 values that can't be used.
- The lead source list is a placeholder until admins maintain it (P6).

## Phase status

- P0 scaffold: done.
- P1 upload and parse: done.
- P2 column mapping: done.
- P3 analysis: done except the golden-output test (needs normalizer v5).
- Next: P4 (enrichment).
