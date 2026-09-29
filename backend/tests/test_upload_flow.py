"""P1 end to end: create job -> upload -> confirm -> parse, against moto."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from shared.audit import AuditWriter
from shared.jobs import JobState
from tasks.parse_file import ParseDeps, run
from tests.conftest import UPLOADS_BUCKET, Env
from tests.helpers import ADMIN, call, http_event

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
OWNER = "uploader@example.com"


def _parse_deps(env: Env, **kw: Any) -> ParseDeps:
    return env.parse_deps(**kw)  # type: ignore[no-any-return]


def _create(filename: str = "leads.xlsx", enrich: bool = True) -> dict[str, Any]:
    status, body = call(
        http_event("POST", "/jobs", email=OWNER, body={"filename": filename, "enrich": enrich})
    )
    assert status == 201, body
    return body  # type: ignore[no-any-return]


def _upload(env: Env, created: dict[str, Any], data: bytes) -> None:
    # Stands in for the browser's presigned POST.
    env.s3.put_object(Bucket=UPLOADS_BUCKET, Key=created["upload"]["fields"]["key"], Body=data)


def _full_upload(env: Env, fixture: str, filename: str | None = None) -> str:
    created = _create(filename or fixture)
    _upload(env, created, (FIXTURES / fixture).read_bytes())
    job_id = created["job"]["job_id"]
    assert call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))[0] == 202
    assert env.parse_requests == [job_id]
    run(job_id, _parse_deps(env))
    return str(job_id)


class TestCreate:
    def test_creates_job_and_presigned_post(self, app_env: Env) -> None:
        body = _create("Demo List.xlsx", enrich=False)
        job = body["job"]
        assert job["state"] == "AWAITING_UPLOAD"
        assert job["filename"] == "Demo List.xlsx"
        assert job["owner_email"] == OWNER
        assert job["enrich"] is False
        assert "upload_key" not in job and "owner_sub" not in job
        fields = body["upload"]["fields"]
        assert fields["key"] == f"{job['job_id']}/source.xlsx"  # user filename never in the key
        policy = json.loads(__import__("base64").b64decode(fields["policy"]))
        assert ["content-length-range", 1, 10 * 1024 * 1024] in policy["conditions"]
        assert app_env.audit_types(job["job_id"]) == ["JOB_CREATED", "JOB_STATE_CHANGED"]

    @pytest.mark.parametrize("name", ["leads.pdf", "leads", "", "../x.csv"])
    def test_rejects_bad_filenames(self, app_env: Env, name: str) -> None:
        status, _ = call(http_event("POST", "/jobs", body={"filename": name}))
        assert status == 400
        assert app_env.jobs_table.scan()["Count"] == 0

    def test_wrong_type_message_is_plain_english(self, app_env: Env) -> None:
        _, body = call(http_event("POST", "/jobs", body={"filename": "leads.pdf"}))
        assert "This file is a .pdf. Upload a .csv or .xlsx file" in body["message"]

    def test_audit_failure_creates_nothing(self, app_env: Env) -> None:
        broken = AuditWriter("missing-table", env="dev", app_version="t")
        app_env.jobs.audit = broken
        assert call(http_event("POST", "/jobs", body={"filename": "a.csv"}))[0] == 503
        assert app_env.jobs_table.scan()["Count"] == 0


class TestUploadAndParse:
    def test_template_reaches_mapping_review(self, app_env: Env) -> None:
        job_id = _full_upload(app_env, "template_filled.xlsx")
        status, job = call(http_event("GET", f"/jobs/{job_id}", email=OWNER))
        assert status == 200
        assert job["state"] == "MAPPING_REVIEW"
        assert job["parse"]["row_count"] == 4
        assert job["parse"]["sheet_name"] == "Sheet1"
        assert len(job["parse"]["headers"]) == 25
        assert [w["code"] for w in job["parse"]["warnings"]] == ["EXCEL_ERROR_VALUE"]
        assert len(job["file"]["sha256"]) == 64
        assert job["file"]["version_id"]

        rows = app_env.rows.list(job_id)
        assert [r["row_id"] for r in rows] == [3, 4, 6, 7]
        assert rows[0]["source"]["Business Phone"] == "5550100100"
        assert rows[0]["expires_at"] > 0

        assert app_env.audit_types(job_id) == [
            "JOB_CREATED",
            "JOB_STATE_CHANGED",  # -> AWAITING_UPLOAD
            "JOB_STATE_CHANGED",  # -> UPLOADED
            "FILE_UPLOADED",
            "FILE_PARSED",
            "MAPPING_SUGGESTED",  # template: all exact, so no AI_INVOCATION
            "JOB_STATE_CHANGED",  # -> MAPPING_REVIEW
        ]

    def test_file_uploaded_event_has_hash_size_and_version(self, app_env: Env) -> None:
        job_id = _full_upload(app_env, "semicolon.csv")
        items = app_env.audit_table.scan()["Items"]
        uploaded = next(i for i in items if i["event_type"] == "FILE_UPLOADED")
        details = json.loads(uploaded["details"])
        import hashlib

        data = (FIXTURES / "semicolon.csv").read_bytes()
        assert details["sha256"] == hashlib.sha256(data).hexdigest()
        assert details["size"] == len(data)
        assert details["version_id"] == app_env.jobs.get(job_id)["file"]["version_id"]

    def test_raw_file_version_is_locked(self, app_env: Env) -> None:
        job_id = _full_upload(app_env, "semicolon.csv")
        job = app_env.jobs.get(job_id)
        assert job is not None
        key = f"{job_id}/source.csv"
        # moto's GetObjectRetention is broken; HEAD reports the same lock.
        head = app_env.s3.head_object(
            Bucket=UPLOADS_BUCKET, Key=key, VersionId=job["file"]["version_id"]
        )
        assert head["ObjectLockMode"] == "GOVERNANCE"
        days = (head["ObjectLockRetainUntilDate"] - datetime.now(UTC)).days
        assert 88 <= days <= 90

    def test_parse_uses_the_confirmed_version(self, app_env: Env) -> None:
        created = _create("leads.csv")
        job_id = created["job"]["job_id"]
        _upload(app_env, created, b"Company\nConfirmed\n")
        call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        _upload(app_env, created, b"Company\nOverwritten later\n")  # a second PUT after confirm
        run(job_id, _parse_deps(app_env))
        assert app_env.rows.list(job_id)[0]["source"] == {"Company": "Confirmed"}

    def test_parse_failure_is_recorded_with_user_message(self, app_env: Env) -> None:
        created = _create("leads.csv")
        job_id = created["job"]["job_id"]
        _upload(app_env, created, b"Company,Email\n")
        call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        assert run(job_id, _parse_deps(app_env)) == JobState.PARSE_FAILED
        job = app_env.jobs.get(job_id)
        assert job is not None
        assert job["state"] == "PARSE_FAILED"
        assert job["parse_error"]["code"] == "NO_DATA_ROWS"
        assert "no data rows" in job["parse_error"]["message"]
        assert app_env.audit_types(job_id)[-3:] == [
            "FILE_UPLOADED",
            "PARSE_FAILED",
            "JOB_STATE_CHANGED",
        ]
        assert app_env.rows.list(job_id) == []

    def test_oversize_file_fails_parse(self, app_env: Env) -> None:
        created = _create("big.csv")
        job_id = created["job"]["job_id"]
        _upload(app_env, created, b"Company\n" + b"x" * 200)
        call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        run(job_id, _parse_deps(app_env, max_bytes=100))
        job = app_env.jobs.get(job_id)
        assert job is not None and job["parse_error"]["code"] == "FILE_TOO_LARGE"

    def test_rerun_is_a_no_op(self, app_env: Env) -> None:
        job_id = _full_upload(app_env, "semicolon.csv")
        before = app_env.audit_types(job_id)
        assert run(job_id, _parse_deps(app_env)) == "skipped"
        assert app_env.audit_types(job_id) == before

    def test_unexpected_error_fails_job_cleanly(self, app_env: Env) -> None:
        created = _create("leads.csv")
        job_id = created["job"]["job_id"]
        _upload(app_env, created, b"Company\nAcme\n")
        call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        deps = _parse_deps(app_env, uploads_bucket="no-such-bucket")
        assert run(job_id, deps) == JobState.PARSE_FAILED
        job = app_env.jobs.get(job_id)
        assert job is not None and job["parse_error"]["code"] == "SYSTEM_ERROR"


class TestConfirmUpload:
    def test_missing_file(self, app_env: Env) -> None:
        job_id = _create()["job"]["job_id"]
        status, body = call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        assert status == 400
        assert body["message"] == "We didn't receive your file. Try uploading it again."
        assert app_env.parse_requests == []

    def test_confirming_twice_conflicts(self, app_env: Env) -> None:
        created = _create("leads.csv")
        _upload(app_env, created, b"Company\nAcme\n")
        job_id = created["job"]["job_id"]
        assert call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))[0] == 202
        assert call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))[0] == 409
        assert app_env.parse_requests == [job_id]


class TestAccess:
    def test_other_users_job_is_denied_and_audited(self, app_env: Env) -> None:
        job_id = _create()["job"]["job_id"]
        for method, path in (("GET", f"/jobs/{job_id}"), ("POST", f"/jobs/{job_id}/uploaded")):
            status, _ = call(http_event(method, path, email="someone.else@example.com"))
            assert status == 403
        assert app_env.audit_types(job_id).count("ACCESS_DENIED") == 2

    def test_admin_can_read_any_job(self, app_env: Env) -> None:
        job_id = _create()["job"]["job_id"]
        status, _ = call(
            http_event("GET", f"/jobs/{job_id}", email="admin@example.com", groups=ADMIN)
        )
        assert status == 200

    def test_unknown_job_is_404(self, app_env: Env) -> None:
        assert call(http_event("GET", "/jobs/j_nope"))[0] == 404

    def test_list_own_jobs_newest_first(self, app_env: Env) -> None:
        first = _create("a.csv")["job"]["job_id"]
        second = _create("b.csv")["job"]["job_id"]
        call(http_event("POST", "/jobs", email="other@example.com", body={"filename": "c.csv"}))
        status, jobs = call(http_event("GET", "/jobs", email=OWNER))
        assert status == 200
        assert [j["job_id"] for j in jobs] == [second, first]

    def test_list_all_requires_admin(self, app_env: Env) -> None:
        _create("a.csv")
        call(http_event("POST", "/jobs", email="other@example.com", body={"filename": "c.csv"}))
        status, _ = call(http_event("GET", "/jobs", query={"all": "true"}))
        assert status == 403
        status, jobs = call(
            http_event(
                "GET", "/jobs", email="admin@example.com", groups=ADMIN, query={"all": "true"}
            )
        )
        assert status == 200 and len(jobs) == 2


def test_presigned_links_use_sigv4(aws: None) -> None:
    """S3 refuses SSE-KMS uploads signed with SigV2 (found on the first dev deploy)."""
    from bff.app import signing_s3_client

    s3 = signing_s3_client()
    post = s3.generate_presigned_post(Bucket="any-bucket", Key="j_1/source.csv")
    assert post["fields"]["x-amz-algorithm"] == "AWS4-HMAC-SHA256"
    assert "AWSAccessKeyId" not in post["fields"] and "signature" not in post["fields"]
    url = s3.generate_presigned_url("get_object", Params={"Bucket": "any-bucket", "Key": "k"})
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
