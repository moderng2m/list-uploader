# List Uploader

A guided web app for uploading event and campaign lead lists: map columns, fix
data problems, optionally enrich with ZoomInfo, and submit to Eloqua through
Workato. See [`docs/SPEC.md`](docs/SPEC.md) and [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md).

> **Build environment only.** This repo deploys to a personal AWS account with
> mocked integrations and synthetic data. No real lead data or production
> credentials belong here. See `CLAUDE.md`.

## Quick start

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
make install
make lint test synth
make web-dev          # http://localhost:5173, served by the in-browser mock API
```

## Deploying to the dev account

```bash
export AWS_PROFILE=<your personal profile>
npx aws-cdk@2 bootstrap -c env=dev     # once per account/region
make deploy
```

After the deploy, the `ListUploader-dev-Web` stack outputs the site URL. In this
phase the site runs in demo mode (mock API in the browser), so it needs no login
and reads no data. Wiring the site to the real API and Cognito login is a later
phase.

Data stores are retained on `cdk destroy` (`RemovalPolicy.RETAIN`), and the upload
and audit buckets are Object-Locked. Delete them by hand if you tear the
environment down.
