"""M7 §12 门禁策略的 API、阈值判定与并发保护验收。"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.metric_profiles import profile_hash
from app.domain.models import (
    Benchmark,
    Dataset,
    DatasetVersion,
    Evaluation,
    MetricProfile,
    MetricValue,
    Run,
)

pytestmark = pytest.mark.integration


def _admin_headers(client: TestClient) -> dict[str, str]:
    """登录管理员并返回 API 鉴权头。"""
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _evaluation_pair(
    client: TestClient, *, current_cer: float = 0.0682, baseline_cer: float = 0.0641
) -> tuple[str, str]:
    """创建同一 Benchmark 下的当前与基线 Evaluation 聚合数据。"""
    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:10]
    with factory() as db:
        dataset = Dataset(name=f"门禁数据集-{suffix}", source_type="aishell1")
        db.add(dataset)
        db.flush()
        version = DatasetVersion(
            dataset_id=dataset.id,
            version="v1",
            status="ready",
            item_count=10,
            capabilities={},
        )
        profile_params: dict[str, Any] = {}
        profile = MetricProfile(
            name=f"门禁口径-{suffix}",
            params=profile_params,
            profile_hash=profile_hash(profile_params),
        )
        db.add_all([version, profile])
        db.flush()
        benchmark = Benchmark(
            name=f"门禁基准-{suffix}",
            dataset_version_id=version.id,
            metric_profile_id=profile.id,
            subsets=["near"],
            version_hash=f"gate-{suffix}",
        )
        db.add(benchmark)
        db.flush()

        evaluation_ids: list[str] = []
        for label, cer in (("基线", baseline_cer), ("当前", current_cer)):
            run = Run(
                benchmark_id=benchmark.id,
                name=f"门禁Run-{label}-{suffix}",
                status="completed",
            )
            db.add(run)
            db.flush()
            evaluation = Evaluation(
                run_id=run.id,
                metric_profile_id=profile.id,
                profile_hash=profile.profile_hash,
                status="completed",
                coverage={"scored": 9, "planned": 10},
                metrics={"cer": cer},
            )
            db.add(evaluation)
            db.flush()
            db.add(
                MetricValue(
                    evaluation_id=evaluation.id,
                    metric="cer",
                    subset="near",
                    scope="micro",
                    bucket={},
                    value=cer,
                    n=9,
                    status="ok",
                    detail={"scoreable_cases": 9, "planned_cases": 10},
                )
            )
            evaluation_ids.append(str(evaluation.id))
        db.commit()
    return evaluation_ids[1], evaluation_ids[0]


def test_gate_policy_defaults_disabled_and_patch_requires_current_etag(
    client: TestClient,
) -> None:
    """未启用策略不阻断；创建幂等、更新审计与旧 ETag 冲突均符合契约。"""
    headers = _admin_headers(client)
    evaluation_id, _ = _evaluation_pair(client)
    not_configured = client.get(f"/api/v1/evaluations/{evaluation_id}/gate", headers=headers)
    assert not_configured.status_code == 200
    assert not_configured.json() == {
        "status": "not_configured",
        "policy": None,
        "checks": [],
        "failed_count": 0,
        "skipped_count": 0,
    }

    key = f"gate-policy-{uuid.uuid4()}"
    payload = {"name": f"未启用策略-{uuid.uuid4().hex[:8]}"}
    created = client.post(
        "/api/v1/gate-policies",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert created.status_code == 201
    assert created.json()["enabled"] is False
    assert created.json()["rules"] == []
    disabled_gate = client.get(
        f"/api/v1/evaluations/{evaluation_id}/gate",
        headers=headers,
        params={"policy_id": created.json()["id"]},
    )
    assert disabled_gate.status_code == 200
    assert disabled_gate.json()["status"] == "not_configured"
    replay = client.post(
        "/api/v1/gate-policies",
        headers={**headers, "Idempotency-Key": key},
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["id"] == created.json()["id"]

    policy_id = created.json()["id"]
    missing = client.patch(
        f"/api/v1/gate-policies/{policy_id}",
        headers=headers,
        json={"name": "缺少版本"},
    )
    assert missing.status_code == 428

    etag = created.json()["etag"]
    changed = client.patch(
        f"/api/v1/gate-policies/{policy_id}",
        headers={**headers, "If-Match": etag},
        json={"name": "已更新策略"},
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.headers["ETag"] == changed.json()["etag"]
    stale = client.patch(
        f"/api/v1/gate-policies/{policy_id}",
        headers={**headers, "If-Match": etag},
        json={"name": "旧版本覆盖"},
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["current"]["name"] == "已更新策略"
    assert stale.json()["detail"]["changed_fields"] == ["name"]

    audit_rows = client.get("/api/v1/audit-logs", headers=headers, params={"limit": 100})
    assert audit_rows.status_code == 200
    policy_audits = [
        row
        for row in audit_rows.json()["items"]
        if row["target"] == policy_id and row["action"] == "gate_policy.updated"
    ]
    assert len(policy_audits) == 1
    assert policy_audits[0]["detail"]["force"] is False


def test_gate_evaluates_absolute_delta_coverage_and_missing_modes(client: TestClient) -> None:
    """策略同时支持绝对值、相对基线、覆盖率与 skip/fail 缺失口径。"""
    headers = _admin_headers(client)
    evaluation_id, baseline_id = _evaluation_pair(client)
    payload = {
        "name": f"回归门禁-{uuid.uuid4().hex[:8]}",
        "enabled": True,
        "rules": [
            {
                "metric": "cer",
                "subset": "near",
                "op": "<=",
                "value": 0.07,
                "scope": "absolute",
                "label": "CER 上限",
            },
            {
                "metric": "cer",
                "subset": "near",
                "op": "<=",
                "value": 0.003,
                "scope": "delta_vs_baseline",
            },
            {
                "metric": "coverage",
                "subset": "near",
                "op": ">=",
                "value": 0.98,
                "scope": "absolute",
            },
            {
                "metric": "der",
                "subset": "near",
                "op": "<=",
                "value": 0.2,
                "scope": "absolute",
            },
        ],
    }
    created = client.post("/api/v1/gate-policies", headers=headers, json=payload)
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    result = client.get(
        f"/api/v1/evaluations/{evaluation_id}/gate",
        headers=headers,
        params={"policy_id": policy_id, "baseline_evaluation_id": baseline_id},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "fail"
    assert body["failed_count"] == 2
    assert body["skipped_count"] == 1
    assert [check["result"] for check in body["checks"]] == [
        "pass",
        "fail",
        "fail",
        "skip",
    ]
    assert body["checks"][1]["delta"] == pytest.approx(0.0041)
    assert body["checks"][1]["baseline"]["evaluation_id"] == baseline_id
    assert body["checks"][2]["observed"] == pytest.approx(0.9)
    assert body["checks"][3]["reason"] == "metric_unavailable"

    current_etag = created.json()["etag"]
    changed = client.patch(
        f"/api/v1/gate-policies/{policy_id}",
        headers={**headers, "If-Match": current_etag},
        json={"on_missing": "fail"},
    )
    assert changed.status_code == 200
    missing_fails = client.get(
        f"/api/v1/evaluations/{evaluation_id}/gate",
        headers=headers,
        params={"policy_id": policy_id, "baseline_evaluation_id": baseline_id},
    )
    assert missing_fails.status_code == 200
    assert missing_fails.json()["status"] == "fail"
    assert missing_fails.json()["skipped_count"] == 0
    assert missing_fails.json()["checks"][3]["result"] == "fail"


def test_gate_rejects_unknown_metrics_and_cross_benchmark_baseline(client: TestClient) -> None:
    """未知指标使用门禁错误码，基线 Evaluation 必须来自同一 Benchmark。"""
    headers = _admin_headers(client)
    _evaluation_id, baseline_id = _evaluation_pair(client)
    invalid = client.post(
        "/api/v1/gate-policies",
        headers=headers,
        json={
            "name": "非法指标",
            "enabled": True,
            "rules": [
                {
                    "metric": "made_up_metric",
                    "op": "<=",
                    "value": 1,
                    "scope": "absolute",
                }
            ],
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "E_GATE_POLICY_INVALID"

    other_evaluation_id, _ = _evaluation_pair(client)
    created = client.post(
        "/api/v1/gate-policies",
        headers=headers,
        json={
            "name": f"跨基线策略-{uuid.uuid4().hex[:8]}",
            "enabled": True,
            "rules": [
                {
                    "metric": "cer",
                    "subset": "near",
                    "op": "<=",
                    "value": 0.07,
                    "scope": "delta_vs_baseline",
                }
            ],
        },
    )
    assert created.status_code == 201
    result = client.get(
        f"/api/v1/evaluations/{other_evaluation_id}/gate",
        headers=headers,
        params={
            "policy_id": created.json()["id"],
            "baseline_evaluation_id": baseline_id,
        },
    )
    assert result.status_code == 422
    assert result.json()["code"] == "E_SEMANTIC"
