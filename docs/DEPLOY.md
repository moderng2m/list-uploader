# Deploying and operating List Uploader (dev)

This deploys the **dev** environment to the personal build account
(`brandonkeithfarris`). It runs on synthetic data and fake integrations only
(`INTEGRATIONS=fake`, `send_to_prod=false`); see CLAUDE.md, "Hard rule". The
TriNet deployment will be done later from TriNet's own account with the same CDK
app (see "Not covered here").

## Before the first deploy

1. **Tools:** Node 20+, [uv](https://docs.astral.sh/uv/), and AWS credentials for
   the personal account in your shell (`aws sts get-caller-identity` shows it).
2. **Install:** `make install`
3. **Bootstrap CDK once per account and region:**
   `npx -y aws-cdk@2 bootstrap aws://<account-id>/us-east-1`
4. **Check what will be created:** `make diff CDK_ARGS="-c alert_emails=you@example.org"`

## Deploy

```
make lint test synth
make deploy CDK_ARGS="-c alert_emails=you@example.org"
```

`make deploy` refuses any environment other than dev. It builds the SPA in live
mode (real API and Cognito sign-in) and deploys these stacks in order:

| Stack | What it holds |
|---|---|
| Web | Site bucket, CloudFront, security headers (CSP, HSTS) |
| Storage | Buckets, DynamoDB tables, KMS key, audit archive pipeline |
| AuditQuery | Glue table, Athena workgroup, `mops-audit-reader-dev`, CloudTrail for the archive |
| Auth | Cognito user pool, hosted sign-in domain, web client (code + PKCE) |
| Workflows | Analyze, Enrich, Send state machines and their task Lambdas |
| Api | HTTP API (JWT, throttled, access-logged), BFF and parse Lambdas |
| Monitoring | Alert topic, alarms, operations dashboard, monitor and canary Lambdas |
| Site | The built SPA and its `config.json` |

The site URL is the `SiteUrl` output of the Site stack.

## After the first deploy

1. **Confirm the alert email.** SNS sends a "Subscription Confirmation" email to
   each address in `alert_emails`. Click the link, or no alarm will reach you.
2. **Create yourself a user and make it an admin** (there is no self sign-up):

   ```
   POOL=$(aws cognito-idp list-user-pools --max-results 20 \
     --query "UserPools[?Name=='list-uploader-dev'].Id" --output text)
   aws cognito-idp admin-create-user --user-pool-id $POOL \
     --username you@example.org \
     --user-attributes Name=email,Value=you@example.org Name=email_verified,Value=true
   aws cognito-idp admin-add-user-to-group --user-pool-id $POOL \
     --username you@example.org --group-name admin
   ```

   Cognito emails a temporary password; you set a new one at first sign-in.
   Leave out the second command for an uploader who isn't an admin.
3. **Sign in** at the site URL and run one of the synthetic files from
   `backend/tests/fixtures/synthetic/` through upload, mapping, analysis, and send.
4. **Check the archive and Athena** with that upload's job ID (from the URL):
   `make verify-archive JOB=j_...`. It checks Firehose delivered to
   `audit/dt=<today>/` with nothing under `audit-errors/`, then queries Athena *as*
   `mops-audit-reader-dev` and compares the event count with DynamoDB.
   Add `DENIED=arn:aws:iam::<account>:role/<another-role>` to confirm, with IAM's
   policy simulator, that a non-MOps principal can't query the audit workgroup.
5. **Fire every alarm once:** `make fire-alarms` shows the plan;
   `make fire-alarms EXECUTE=1` fires them. You should get one email per alarm
   (the hourly AI alarm and the daily canary alarm evaluate at the end of their
   period). See the script's header for how each one is triggered.

## Alarms

All go to the `list-uploader-alerts-dev` topic. Each alarm's description starts
with its severity.

| Alarm | Fires when | First thing to check |
|---|---|---|
| audit-write-failure (critical) | an audit event couldn't be written, 1 min | BFF/task logs for "audit write failed"; DynamoDB throttling or KMS access. Sends are blocked until fixed. |
| send-workflow-failed | a SendWorkflow execution failed, timed out or was aborted | The execution's failed step in Step Functions; the job is FAILED and its owner can retry failed rows. |
| rows-stuck-sending | a row posted to Eloqua has had no answer for 15+ min | The job's timeline; the row may or may not have landed. It is never retried automatically. |
| workato-error-rate | over 5% of Workato calls failed in 15 min | Workato status and the callable's job history. |
| gate-rejected-after-ui | the server refused a send the browser showed as passing | A bug or tampering: the job's GATE_EVALUATED events show both results. |
| enrichment-batch-errors | over 20% of a job's ZoomInfo batches failed | The ZoomInfo callable in Workato. Rows go ahead without enrichment. |
| bedrock-parse-failures | over 20% of AI calls returned unusable output in an hour | AI_INVOCATION events (outcome); the model ID and prompt versions. |
| lambda-errors | 5+ Lambda errors in 5 min | The dashboard's per-function errors, then that function's logs. |
| lambda-throttles | any Lambda throttling | Account concurrency limits. |
| job-stuck-running | a job has been parsing/analyzing/enriching/sending for 60+ min | The monitor Lambda's log names the job IDs; check its workflow execution. |
| canary-failed (dev) | the daily end-to-end canary failed or didn't run | The canary Lambda's log says which step failed. |

The **operations dashboard** is `list-uploader-dev-operations` in CloudWatch.

## Audit archive (MOps)

- Database `list_uploader_dev_audit`, table `events`, workgroup
  `list-uploader-audit-dev`. Saved queries in the workgroup: job history, person
  lookup (by email hash), leads per week by campaign, time from upload to send, top
  issue codes, enrichment yield, AI accept rate, template adoption.
- `subject`, `before`, `after` and `details` are JSON text: use
  `json_extract_scalar(details, '$.field')`.
- Access is through the role `mops-audit-reader-dev`. Its trust is the account; a
  person also needs permission to assume it. In TriNet's account that is the SSO
  permission set that identifies MOps (D14). Being an app admin grants nothing here.
- Every read of `audit/` in the archive bucket is logged by the
  `list-uploader-dev-audit-archive-access` trail (S3 data events), and Athena keeps
  the workgroup's query history.

## Costs (rough estimate, idle dev)

My estimate, not a quote: a few US dollars a month while idle. The fixed items are
two KMS keys (about $1 each per month), CloudWatch alarms (about $0.10 each) and
the dashboard (free within the first three dashboards). DynamoDB, Lambda, Step
Functions, Firehose, Athena and CloudTrail data events are usage-based and
negligible at dev volume. Check AWS Cost Explorer after the first week.

## Tearing down

`npx -y aws-cdk@2 destroy -c env=dev --all` removes most resources. These are kept
on purpose and must be deleted by hand if you want them gone: the KMS data key
(scheduled deletion), the uploads and processed buckets, the audit archive bucket
(Object Lock, governance mode), the archive-access trail bucket, the DynamoDB
tables (the audit table has deletion protection), and the Cognito user pool.

## Not covered here

- **UAT with real Workato and a prod deployment** (BUILD_PLAN P7) happen in
  TriNet's account, not this one: no real Workato endpoints or tokens and no real
  lead data belong in the build account. The prod config already expects a
  Workato token in Secrets Manager (`list-uploader/prod/workato-api-token`), but the
  real Workato client itself isn't written yet; only the fake runs.
- **Corporate SSO:** pass the IdP's SAML metadata URL at deploy time,
  `CDK_ARGS="-c saml_metadata_url=https://... -c saml_idp_name=Okta"`, and register
  the Cognito domain's SAML endpoint with the IdP (OQ-7).
- **The first 3 prod uploads spot-checked in Eloqua/SFDC** and the UAT sign-off.
