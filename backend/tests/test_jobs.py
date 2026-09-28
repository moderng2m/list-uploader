from __future__ import annotations

import pytest

from shared.audit import Actor, ActorType, AuditWriteError, AuditWriter, StateConflict
from shared.jobs import ALLOWED, InvalidTransition, JobRepo, JobState
from tests.conftest import JOBS_TABLE, Env

USER = Actor(type=ActorType.USER, email="uploader@example.com", sub="s")


def _new(env: Env) -> str:
    job = env.jobs.create(
        actor=USER,
        owner_email="uploader@example.com",
        owner_sub="s",
        filename="a.csv",
        enrich=True,
        upload_key_for=lambda jid: f"{jid}/source.csv",
    )
    return str(job["job_id"])


def test_transition_updates_state_and_audits_atomically(env: Env) -> None:
    job_id = _new(env)
    env.jobs.transition(
        job_id,
        JobState.AWAITING_UPLOAD,
        JobState.UPLOADED,
        actor=USER,
        set_fields={"file": {"size": 10, "ratio": 0.5}},
    )
    job = env.jobs.get(job_id)
    assert job is not None
    assert job["state"] == "UPLOADED"
    assert job["file"] == {"size": 10, "ratio": 0.5}
    assert env.audit_types(job_id).count("JOB_STATE_CHANGED") == 2


def test_stale_transition_conflicts_and_writes_nothing(env: Env) -> None:
    job_id = _new(env)
    before = env.audit_types(job_id)
    with pytest.raises(StateConflict):
        env.jobs.transition(job_id, JobState.UPLOADED, JobState.MAPPING_REVIEW, actor=USER)
    assert env.audit_types(job_id) == before
    job = env.jobs.get(job_id)
    assert job is not None and job["state"] == "AWAITING_UPLOAD"


def test_audit_failure_leaves_job_unchanged(env: Env) -> None:
    job_id = _new(env)
    broken = JobRepo(AuditWriter("missing-table", env="dev", app_version="t"), JOBS_TABLE)
    with pytest.raises(AuditWriteError):
        broken.transition(job_id, JobState.AWAITING_UPLOAD, JobState.UPLOADED, actor=USER)
    job = env.jobs.get(job_id)
    assert job is not None and job["state"] == "AWAITING_UPLOAD"


def test_disallowed_transition_is_rejected_before_writing(env: Env) -> None:
    job_id = _new(env)
    with pytest.raises(InvalidTransition):
        env.jobs.transition(job_id, JobState.AWAITING_UPLOAD, JobState.SENDING, actor=USER)


def test_state_machine_matches_spec() -> None:
    assert JobState.MAPPING_REVIEW in ALLOWED[JobState.UPLOADED]
    assert JobState.PARSE_FAILED in ALLOWED[JobState.UPLOADED]
    assert JobState.FAILED in ALLOWED[JobState.SENDING]
    assert JobState.EXPIRED in ALLOWED[JobState.ANALYSIS_REVIEW]
    assert JobState.CANCELLED not in ALLOWED[JobState.SENDING]
    assert not ALLOWED.get(JobState.COMPLETED)
