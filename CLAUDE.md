# List Uploader

Self-service lead-list upload: upload → map columns → analyze and fix → (optional)
enrich → send to Eloqua → confirmation. The spec is `docs/SPEC.md`; the phased plan
is `docs/BUILD_PLAN.md`.

## Hard rule: mocks and synthetic data only

This repo and the AWS account it deploys to (`brandonkeithfarris`, a personal
account) are a **build environment**, not TriNet's. See SPEC decision D15.

- No real lead data anywhere: not in fixtures, tests, the frontend mocks, S3, or
  DynamoDB. Use obviously fake values (`example.com`, "Demo Conference 2026").
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
    messages.py          all user-facing text (SPEC §20)
    config_defaults.py   thresholds and limits (SPEC §14.4)
    lead_normalizer/     normalizer v5 goes here unmodified (P3)
  bff/                   API Gateway router (Powertools APIGatewayHttpResolver)
  audit_archiver/        AuditEvents stream -> Firehose -> S3 archive
  tests/                 pytest + moto
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

## Phase status

- P0 scaffold: done.
- Next: P1 (upload and parse). Needs synthetic sample files, not real lists.
