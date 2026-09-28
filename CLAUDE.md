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
    enrichment.py        EnrichmentProvider protocol, ZoomInfoProvider, eligibility
    fake_zoominfo.py     synthetic ZoomInfo answers for the fake Workato
    sending.py           pre-send gate (SPEC §17) and Post to Eloqua payload (§16.2)
    processed_file.py    downloadable processed CSV (§7.4), formula-injection safe
    jobs.py              JobState machine; JobRepo (state change + audit in one txn)
    rows.py              Rows table access
    messages.py          all user-facing text (SPEC §20)
    config_defaults.py   thresholds and limits (SPEC §14.4)
    lead_normalizer/     normalizer v5 goes here unmodified (P3)
  bff/                   API Gateway router (Powertools APIGatewayHttpResolver)
  tasks/                 async task Lambdas (parse_file; analyze, enrich, send = workflow steps)
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

## Enrichment (P4)

`POST /jobs/{id}/enrich` (ANALYSIS_REVIEW, job.enrich true; or retry from FAILED
when the last error was enrichment) starts EnrichWorkflow: Prepare (eligible rows,
SPEC §15.2, in batches of 25) -> Map (2 in parallel) of provider calls -> Finalize
(re-evaluate every row, write rows and audit events, -> ENRICHMENT_REVIEW).
`ZoomInfoProvider` retries a failing batch 3 times, then marks its rows `error`
and the job carries on (§15.5). `tasks.enrich.run_all` mirrors the state machine.

The provider only stores what ZoomInfo returned (`row.enrichment`). The merge
lives in `evaluate_row`: accepted results, and review results the user chose to
apply, fill blank inputs before normalization (never email), so precedence,
normalization and re-validation are the same code as everywhere else. Do-not-call
numbers are dropped by the provider. `POST /enrichment-decisions` (apply/skip or
`skip_all`) goes through the same row-change path as row edits.

Decisions made while building P4:
- Rows can be edited in ENRICHMENT_REVIEW as well as ANALYSIS_REVIEW (the spec's
  state diagram has no way back, but rows that stayed blank need fixing somewhere).
- A row whose enrichment match is still awaiting apply/skip keeps its blank
  company/name as pending, not blocking.
- LINKEDIN_MULTIPLE_PROFILES (warning) is a new issue code for SPEC §15.4's
  "more than one LinkedIn profile" warning.
- ENRICHMENT_RESULT is written once per row at finalize: match details plus the
  values filled. ENRICHMENT_REQUESTED is per batch.
- `selected_candidate_json`'s key names aren't documented; the parser accepts
  zi_best_x, snake_case and camelCase. Confirm against a real sample response.

## Gate and send (P5)

`evaluate_gate` (shared/sending.py) runs on the server three times: `GET /gate`
(Review & Send loads), `POST /send`, and SendPrepare in the workflow. Each writes
GATE_EVALUATED with `where` = ui / server / workflow. `POST /send` takes the
confirmation the user saw (`rows_to_send` + rows per campaign and status) and
refuses with 409 if it no longer matches. It then moves review -> READY_TO_SEND ->
SENDING (SEND_CONFIRMED), so a second click gets 409 and only one workflow starts.

SendWorkflow: SendPrepare (gate again; 25-row batches) -> Map SendBatches
(`send_max_concurrency`, 5) -> SendFinalize (COMPLETED or COMPLETED_WITH_ERRORS).
Per row: build payload -> `assert_send_to_prod_allowed` -> conditional claim
(`send.status` not_sent/failed -> sending) -> post -> ROW_SUBMITTED or
ROW_SEND_FAILED written in one transaction with the row's send result. The claim
makes a retried batch or a second execution skip rows already taken.
`tasks.send.run_all` mirrors the state machine.

`POST /retry-failed` (COMPLETED_WITH_ERRORS, or FAILED at the send stage) re-sends
only `failed` rows. `GET /result` lists submitted, failed and unconfirmed rows.
`GET /download` writes PROCESSED_FILE_DOWNLOADED, then puts the CSV in the
processed bucket and returns a 5-minute presigned GET.

Decisions made while building P5:
- **OQ-1 interim rule, taken literally:** only fields with a confirmed callable
  parameter (`catalog.CALLABLE_PARAMS`) go in the payload. Company, Last Name and
  List Name are therefore NOT sent until OQ-1 is answered, although the gate still
  requires them. The Send screen lists sent and not-sent fields.
- No answer from Post to Eloqua (timeout, connection error): the row stays
  `sending` with ROW_SEND_FAILED outcome `no_response`, shows as "not confirmed",
  and is never retried automatically (it may have landed).
- A retry doesn't re-run the gate: the rows already passed it and were confirmed.
- Map items carry 25 rows (not one) to keep 5,000-row jobs well under the Step
  Functions history limit; 5 items in flight = 5 concurrent posts.
- `EnvConfig` refuses `send_to_prod=True` outside `prod` at synth time.
- Processed-file cells starting with `=`, `@`, tab, CR, or a non-numeric `+`/`-`
  get a leading apostrophe (CSV formula injection).

## Phase status

- P0 scaffold: done.
- P1 upload and parse: done.
- P2 column mapping: done.
- P3 analysis: done except the golden-output test (needs normalizer v5).
- P4 enrichment: done (fake ZoomInfo).
- P5 gate and send: done (fake Post to Eloqua).
- Next: P6 (history, admin, audit).
