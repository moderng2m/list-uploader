"""P7: operational checks (monitor, canary) and the metrics the alarms read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shared.enrichment import ZoomInfoProvider
from shared.observability import metrics
from shared.workato_client import FakeWorkatoClient, PostResult, SendToProdViolation
from tasks import canary
from tasks.analyze import run_all as run_analysis
from tasks.monitor import check
from tasks.parse_file import run as run_parse
from tasks.send import run_all as run_send
from tests.conftest import Env
from tests.test_enrichment_flow import _enriched
from tests.test_send_flow import _analyzed, _csv, _send


def _metric(name: str) -> list[float]:
    entry = metrics.metric_set.get(name)
    return [float(v) for v in entry["Value"]] if entry else []


@pytest.fixture(autouse=True)
def clear_metrics() -> Any:
    metrics.clear_metrics()
    yield
    metrics.clear_metrics()


class TestWorkatoMetrics:
    def test_every_call_counts_and_failures_are_errors(self) -> None:
        workato = FakeWorkatoClient(env="dev", post_status={"j:3": 500})
        workato.lookup_campaigns(["701000000000001AAA"], caller_job_id="j")
        for row in (2, 3):
            payload = {"source_record_id": f"j:{row}", "send_to_prod": False}
            assert isinstance(workato.post_to_eloqua(payload), PostResult)
        assert sum(_metric("WorkatoCalls")) == 3
        assert sum(_metric("WorkatoErrors")) == 1
        assert len(_metric("WorkatoLatencyMs")) == 3

    def test_exceptions_are_errors_and_the_prod_guard_is_not_a_call(self) -> None:
        workato = FakeWorkatoClient(env="dev", fail_enrich=True)
        with pytest.raises(Exception, match="ZoomInfo"):
            workato.enrich_contacts([{"source_record_id": "j:2"}], caller_job_id="j")
        with pytest.raises(SendToProdViolation):
            workato.post_to_eloqua({"source_record_id": "j:2", "send_to_prod": True})
        assert sum(_metric("WorkatoCalls")) == 1
        assert sum(_metric("WorkatoErrors")) == 1


class TestMonitor:
    def test_quiet_when_nothing_is_stuck(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(2))
        assert _send(job_id)[0] == 202
        run_send(job_id, app_env.send_deps())
        assert check(app_env.jobs, app_env.rows, datetime.now(UTC)) == {
            "stuck_jobs": [],
            "stuck_sending_rows": 0,
        }

    def test_finds_stuck_jobs_and_rows(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(2))
        assert _send(job_id)[0] == 202  # SENDING, never finished
        rows = app_env.rows.list(job_id)
        app_env.rows.claim_send(
            job_id,
            rows[0]["row_id"],
            attempt=1,
            payload_sha256="x",
            at=datetime.now(UTC).isoformat(),
        )
        # 20 minutes later: the row is stuck, the job isn't yet.
        later = datetime.now(UTC) + timedelta(minutes=20)
        assert check(app_env.jobs, app_env.rows, later) == {
            "stuck_jobs": [],
            "stuck_sending_rows": 1,
        }
        much_later = datetime.now(UTC) + timedelta(minutes=90)
        result = check(app_env.jobs, app_env.rows, much_later)
        assert result["stuck_jobs"] == [job_id]
        # A day on, the stuck row has been reported and stops counting.
        assert (
            check(app_env.jobs, app_env.rows, datetime.now(UTC) + timedelta(days=2))[
                "stuck_sending_rows"
            ]
            == 0
        )


class TestCanary:
    def test_runs_end_to_end_through_the_api(self, app_env: Env) -> None:
        outcome = canary.run(
            app_env.bff_deps(),
            parse=lambda job_id: run_parse(job_id, app_env.parse_deps()),
            analyze_all=lambda job_id: run_analysis(job_id, app_env.analyze_deps()),
            send_all=lambda job_id: run_send(job_id, app_env.send_deps()),
        )
        assert outcome["submitted"] == 2
        job = app_env.jobs.get(outcome["job_id"])
        assert job is not None
        assert job["state"] == "COMPLETED"
        assert job["owner_email"] == canary.CANARY_USER
        # Every lead in the canary file is synthetic.
        assert all(
            r["processed"]["email"].endswith(".example") for r in app_env.rows.list(job["job_id"])
        )

    def test_reports_a_broken_step(self, app_env: Env) -> None:
        with pytest.raises(canary.CanaryFailed, match="send didn't complete"):
            canary.run(
                app_env.bff_deps(),
                parse=lambda job_id: run_parse(job_id, app_env.parse_deps()),
                analyze_all=lambda job_id: run_analysis(job_id, app_env.analyze_deps()),
                send_all=lambda job_id: "FAILED",
            )


def test_enrichment_error_pct_is_published(app_env: Env) -> None:
    workato = FakeWorkatoClient(env="dev", fail_enrich=True)
    _enriched(app_env, _csv(3), provider=ZoomInfoProvider(workato, sleep=lambda _: None))
    assert _metric("EnrichmentErrorPct") == [100.0]
