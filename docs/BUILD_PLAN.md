# List Uploader: Build Plan

Build in order. Each phase ends with passing tests and a short demo against mocks. Section refs (§) point to `SPEC.md`.

## Prerequisites outside Claude Code (Brandon)
These gate specific phases.

| Item | Needed by |
|---|---|
| Add lead normalizer v5 to `backend/shared/lead_normalizer/` unmodified, plus ~50 scrubbed real rows for golden-output tests | P3 |
| Sample files: the template, 3–5 real (scrubbed) event lists incl. a messy vendor export | P1 |
| Build `[MOPS] SFDC Campaign Lookup \| Callable` (§18) | P3 (real), mocked before |
| Expose SFDC Lookup, ZI Contact Enrich, and Post to Eloqua on Workato API Platform; capture auth method and timeout (OQ-9) | P4/P5 real calls |
| Confirm the Post to Eloqua field contract (OQ-1) | P5 |
| Sample JSON responses from each callable (success, no-match, error) for fixtures | P3–P5 |
| IdP/Cognito federation + admin group; TriNet IaC standard (OQ-7) | P7 |
| Bedrock model access enabled in the TriNet account/region | P2 real, mocked before |
| Add `workato_job_id` output and optional `caller_job_id` input to the three callables (SPEC §24 item 6) | P4/P5 correlation |
| Confirm audit retention period (OQ-12; interim 2 years) and the SSO group that identifies MOps members (OQ-14) | P7 |
| Brandon's alert email for `alert_emails`; confirm the SNS subscription email after first deploy | P7 |

---

## P0 — Scaffold
- Repo layout per `CLAUDE.md`; Makefile; lint and type config; pytest + moto.
- CDK app with empty stacks: `Storage` (S3, DynamoDB), `Api` (HTTP API, BFF Lambda), `Workflows` (Step Functions), `Web` (S3 + CloudFront), `Auth` (Cognito). `cdk synth` passes for `dev`.
- Frontend shell: routing for Upload / Mapping / Analysis / Enrichment / Send / Result / History / Admin, with a mock API layer (MSW or equivalent).
- `workato_client.py` and `bedrock_client.py` interfaces with fake implementations for tests.
- **Audit and observability foundation** (built first so every later phase uses it):
  - `audit.py`: event schema, event-type enum, fail-closed writer
  - `AuditEvents` table with PutItem-only IAM
  - audit archive bucket with Object Lock + Firehose (IaC only; the archive can be verified in P7)
  - `observability.py`: Powertools Logger/Tracer/Metrics, PII scrubber.

**Accept:**
- `make lint test synth` green; the frontend renders all routes with mock data.
- PII scrubber test: a log call containing an email, phone, and name emits none of them.
- An audit write failure makes the calling action fail.
- IAM synth output shows no Update/Delete on `AuditEvents`.

**From here on, every phase's acceptance includes:** each new action emits its SPEC §21.2.3 event (asserted in tests), and new metrics from SPEC §21.3.2 are emitted.

## P1 — Upload and parse (§6.1, §7.1)
- `POST /jobs` + presigned upload; `POST /jobs/{id}/uploaded` → parse.
- CSV (encoding + delimiter detection) and XLSX parsing: sheet selection, float-to-string, Excel error values, empty-row drop, duplicate headers.
- Persist the job and raw rows; raw file write-once.

**Accept:** unit tests cover the official template (including the stray `#VALUE!`), UTF-8 BOM CSV, Windows-1252 CSV, semicolon CSV, XLSX with phone/ZIP stored as numbers, and an oversize file rejection. Parse output is identical for the same data saved as CSV and XLSX.

## P2 — Column mapping (§6.2, §10)
- Field catalog module (§8) and seed aliases (§10.2) in `Config`.
- Exact → alias → Bedrock suggestion; one-to-one enforcement; required-field check (Campaign Name and Lead Source exempt).
- Mapping UI with method badges; `PUT /mapping`.

**Accept:**
- The template maps 25/25 with zero AI calls.
- A vendor file with headers like "E-mail", "Org", "Job Position" maps via alias/AI.
- With the Bedrock fake returning low confidence, the column is left unmapped.
- With the Bedrock fake returning invalid JSON, mapping still completes with no suggestions.

## P3 — Analysis (§9, §11–§14, §7.3)
- Normalizer wrapper with processed precedence and provenance.
- `sfdc_ids.py` (15→18 + checksum), with tests on known ID pairs.
- SFDC lookup via Workato (fixture-backed), lead source resolution, member status resolution.
- Deterministic junk checks, then batched Bedrock junk pass.
- Duplicate check on (email, campaign_id).
- AnalyzeWorkflow state machine; Analysis UI:
  - summary cards
  - grouped issues with bulk actions
  - editable grid with per-row re-validation
  - campaign panel.

**Accept:**
- Every issue code in §9 has at least one test.
- Normalizer idempotency: `normalize(normalize(x)) == normalize(x)` on fixture rows.
- Golden-output test: the normalizer reproduces recorded v5 output for the fixture rows.
- A file with 3 distinct campaign IDs (one 15-char, one bad checksum, one not found) produces the right per-row issues and messages.
- Blank status → campaign default.
- `Events` → `Marketing: Events` auto-corrected.
- AI failure never blocks.

## P4 — Enrichment (§15)
- `EnrichmentProvider` protocol; ZoomInfo implementation calling the callable in batches of 25 via Workato.
- Fill-blanks-only merge, email never touched, DNC phone rule, LinkedIn normalization and multi-profile warning.
- Review UI with plain-English conflict translations; apply/skip decisions; post-enrichment re-validation.
- Batch-level failure handling.

**Accept:**
- Fixture tests cover accepted, needs-review, no-match, invalid-input, and a whole-batch Workato error.
- A 60-row job makes exactly 3 callable calls.
- A pending-required row becomes ready after enrichment fills Company.
- A non-blank title is never overwritten.

## P5 — Gate and send (§16, §17)
- Server-side gate; Review & Send UI; SendWorkflow with conditional-write idempotency and `send_to_prod` assertion.
- Result page, retry-failed, processed CSV download.

**Accept:**
- Double-clicking Send (two concurrent workflow starts) submits each row once.
- A forced 500 on 2 rows → `COMPLETED_WITH_ERRORS`; retry sends only those 2.
- In `dev`, any payload with `send_to_prod=true` raises before calling Workato.
- Unconfirmed-field handling matches OQ-1's interim rule.

## P6 — History, admin, audit (§6.7–§6.8, §21)
- **Audit UI** (SPEC §21.2.6): job timeline tab, row history drawer with per-field value lineage, admin audit search incl. hashed email lookup, audited CSV export.
- History list and reopen; Admin lead-source CRUD (deactivate, not delete); thresholds and aliases editor; promote-AI-mapping-to-alias; audit records.

**Accept:**
- A non-admin gets 403 on admin routes and on other users' jobs.
- Deactivating a lead source makes it invalid for new jobs without breaking historical jobs.
- For any sent row, the row history explains every processed value back to the source cell.
- Searching an email returns every job that included it.

## P7 — Hardening and deploy
- Cognito federation; IAM least-privilege review; S3/KMS/TLS policies; TTL/lifecycle.
- Observability: CloudWatch operations dashboard, all SPEC §21.3.4 alarms wired to the SNS email topic, X-Ray on, daily synthetic canary in `dev`.
- `mops-audit-reader` role + `list-uploader-audit` Athena workgroup; CloudTrail S3 data events on the audit bucket.
- Audit archive: verify Firehose → S3 Object Lock → Glue/Athena; build the business dashboard queries (SPEC §21.3.3).
- Deploy to `dev`. UAT with real Workato (`send_to_prod=false`) on a test campaign with 3 real lists. Then `prod`.

**Accept:** each alarm fired once in `dev` by fault injection and arrived by email; a MOps member (not an admin of the app) can query the archive in the audit workgroup, and a non-MOps user can't; Athena returns a UAT job's full event history; UAT checklist signed off. The first 3 prod uploads are spot-checked in Eloqua/SFDC by Brandon; this is the evidence that MOps review isn't needed.

---

## Phase 2 backlog (not v1)
- Delivery reconciliation (§16.5).
- `lead-normalizer` standalone batch Lambda (same package, array in / array out) for Workato to call, retiring the inline recipe copy.
- Clay provider (§15.6).
- Fixes to the ZI callable logger (SPEC §24).
