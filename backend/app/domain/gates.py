"""评测门禁策略校验与判定（M7 §12）。"""

import math
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain import audit
from app.domain.etag import make_etag
from app.domain.models import Benchmark, Evaluation, GatePolicy, MetricValue, Run

SUPPORTED_GATE_METRICS = {
    "cer",
    "der",
    "sa_der",
    "naming_acc",
    "speaker_eer",
    "punct_f1",
    "itn_acc",
    "throughput",
    "ttft_ms",
    "finalize_ms",
    "rtf_dev",
    "rtf_e2e",
    "rtf_realtime",
    "overhead_ratio",
    "coverage",
}
SUPPORTED_OPERATORS = {"<", "<=", ">", ">=", "=="}
SUPPORTED_SCOPES = {"absolute", "delta_vs_baseline"}


def validate_rules(rules: list[dict[str, Any]]) -> list[dict[str, object]]:
    """校验规则并剔除未定义字段，避免存入无法执行的策略。"""
    errors: list[dict[str, object]] = []
    normalized: list[dict[str, object]] = []
    for index, rule in enumerate(rules):
        rule_errors: list[dict[str, object]] = []
        metric = rule.get("metric")
        subset = rule.get("subset", "all")
        operator = rule.get("op")
        value = rule.get("value")
        scope = rule.get("scope", "absolute")
        label = rule.get("label")
        path = f"rules.{index}"
        threshold: float | None = None
        if not isinstance(metric, str) or metric not in SUPPORTED_GATE_METRICS:
            rule_errors.append({"path": f"{path}.metric", "reason": "unknown_metric"})
        if not isinstance(subset, str) or not subset or len(subset) > 32:
            rule_errors.append({"path": f"{path}.subset", "reason": "invalid_subset"})
        if not isinstance(operator, str) or operator not in SUPPORTED_OPERATORS:
            rule_errors.append({"path": f"{path}.op", "reason": "unsupported_operator"})
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            rule_errors.append({"path": f"{path}.value", "reason": "invalid_threshold"})
        else:
            threshold = float(value)
        if not isinstance(scope, str) or scope not in SUPPORTED_SCOPES:
            rule_errors.append({"path": f"{path}.scope", "reason": "unsupported_scope"})
        if label is not None and (not isinstance(label, str) or len(label) > 128):
            rule_errors.append({"path": f"{path}.label", "reason": "invalid_label"})
        errors.extend(rule_errors)
        if not rule_errors:
            normalized_rule: dict[str, object] = {
                "metric": metric,
                "subset": subset,
                "op": operator,
                "value": threshold,
                "scope": scope,
            }
            if label is not None:
                normalized_rule["label"] = label
            normalized.append(normalized_rule)
    if errors:
        raise ApiError(
            "E_GATE_POLICY_INVALID",
            "门禁规则非法",
            status_code=422,
            detail={"errors": errors},
        )
    return normalized


def create_gate_policy(
    db: Session,
    *,
    name: str,
    benchmark_id: uuid.UUID | None,
    enabled: bool,
    rules: list[dict[str, Any]],
    on_missing: str,
    actor: str,
) -> GatePolicy:
    """创建门禁策略；业务数据与审计日志在同一事务写入。"""
    normalized_rules = validate_rules(rules)
    if enabled and not normalized_rules:
        raise ApiError(
            "E_GATE_POLICY_INVALID",
            "启用门禁策略至少需要一条规则",
            status_code=422,
            detail={"errors": [{"path": "rules", "reason": "empty_enabled_policy"}]},
        )
    if benchmark_id is not None and db.get(Benchmark, benchmark_id) is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    policy = GatePolicy(
        name=name,
        benchmark_id=benchmark_id,
        enabled=enabled,
        rules=normalized_rules,
        on_missing=on_missing,
        created_by=actor,
        updated_by=actor,
    )
    db.add(policy)
    db.flush()
    audit.record(
        db,
        "gate_policy.created",
        actor=actor,
        resource=str(policy.id),
        detail={
            "name": policy.name,
            "benchmark_id": str(policy.benchmark_id) if policy.benchmark_id else None,
            "enabled": policy.enabled,
            "rules_count": len(policy.rules),
        },
    )
    db.commit()
    db.refresh(policy)
    return policy


def serialize_gate_policy(policy: GatePolicy) -> dict[str, Any]:
    """序列化策略配置及并发更新所需的版本信息。"""
    return {
        "id": str(policy.id),
        "name": policy.name,
        "benchmark_id": str(policy.benchmark_id) if policy.benchmark_id else None,
        "enabled": policy.enabled,
        "rules": policy.rules or [],
        "on_missing": policy.on_missing,
        "created_by": policy.created_by,
        "created_at": policy.created_at.isoformat() if policy.created_at else None,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
        "updated_by": policy.updated_by,
        "version": policy.version,
        "etag": gate_policy_etag(policy),
    }


def gate_policy_configuration(policy: GatePolicy) -> dict[str, Any]:
    """提供参与强 ETag 的稳定策略字段。"""
    return {
        "name": policy.name,
        "benchmark_id": str(policy.benchmark_id) if policy.benchmark_id else None,
        "enabled": policy.enabled,
        "rules": policy.rules or [],
        "on_missing": policy.on_missing,
    }


def gate_policy_etag(policy: GatePolicy) -> str:
    """按策略版本和配置生成强 ETag。"""
    return make_etag("gp", policy.version, gate_policy_configuration(policy))


def _metric_value(
    db: Session, evaluation: Evaluation, metric: str, subset: str
) -> tuple[float | None, str | None]:
    """读取 evaluation 级聚合值；门禁不从逐样本数据重新聚合。"""
    if metric == "coverage":
        if subset == "all":
            scored = evaluation.coverage.get("scored")
            planned = evaluation.coverage.get("planned")
        else:
            row = db.scalar(
                select(MetricValue).where(
                    MetricValue.evaluation_id == evaluation.id,
                    MetricValue.metric == "cer",
                    MetricValue.subset == subset,
                    MetricValue.scope == "micro",
                    MetricValue.run_target_id.is_(None),
                    MetricValue.bucket == {},
                )
            )
            detail = row.detail if row is not None else {}
            scored = detail.get("scoreable_cases", row.n if row is not None else None)
            planned = detail.get("planned_cases")
        if (
            isinstance(scored, bool)
            or not isinstance(scored, int | float)
            or isinstance(planned, bool)
            or not isinstance(planned, int)
            or planned <= 0
        ):
            return None, "metric_unavailable"
        return float(scored) / planned, None

    row = db.scalar(
        select(MetricValue).where(
            MetricValue.evaluation_id == evaluation.id,
            MetricValue.metric == metric,
            MetricValue.subset == subset,
            MetricValue.scope == "micro",
            MetricValue.run_target_id.is_(None),
            MetricValue.bucket == {},
        )
    )
    if row is None:
        return None, "metric_unavailable"
    if row.value is None or row.status == "na":
        return None, row.reason or "metric_unavailable"
    return float(row.value), None


def _compare(observed: float, operator: str, threshold: float) -> bool:
    """按规则中声明的比较符执行判定。"""
    if operator == "<":
        return observed < threshold
    if operator == "<=":
        return observed <= threshold
    if operator == ">":
        return observed > threshold
    if operator == ">=":
        return observed >= threshold
    return math.isclose(observed, threshold, rel_tol=0.0, abs_tol=1e-12)


def _select_policy(
    db: Session, benchmark_id: uuid.UUID, policy_id: uuid.UUID | None
) -> GatePolicy | None:
    """优先选 Benchmark 专属策略；没有时再用全局策略。"""
    if policy_id is not None:
        policy = db.get(GatePolicy, policy_id)
        if policy is None:
            raise ApiError("E_NOT_FOUND", "门禁策略不存在", status_code=404)
        if policy.benchmark_id not in {None, benchmark_id}:
            raise ApiError("E_SEMANTIC", "门禁策略不适用于当前 Benchmark", status_code=422)
        return policy
    return db.scalar(
        select(GatePolicy)
        .where(
            GatePolicy.enabled.is_(True),
            (GatePolicy.benchmark_id == benchmark_id) | GatePolicy.benchmark_id.is_(None),
        )
        .order_by(GatePolicy.benchmark_id.is_(None), GatePolicy.updated_at.desc())
        .limit(1)
    )


def evaluate_gate(
    db: Session,
    *,
    evaluation_id: uuid.UUID,
    policy_id: uuid.UUID | None = None,
    baseline_evaluation_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """按策略规则计算 pass/fail；门禁结果不会修改 Evaluation 状态。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    run = db.get(Run, evaluation.run_id)
    if run is None:
        raise ApiError("E_NOT_FOUND", "Run 不存在", status_code=404)
    benchmark = db.get(Benchmark, run.benchmark_id)
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)

    policy = _select_policy(db, benchmark.id, policy_id)
    if policy is None or not policy.enabled:
        return {
            "status": "not_configured",
            "policy": (
                {"id": str(policy.id), "name": policy.name, "enabled": policy.enabled}
                if policy is not None
                else None
            ),
            "checks": [],
            "failed_count": 0,
            "skipped_count": 0,
        }

    baseline: Evaluation | None = None
    if baseline_evaluation_id is not None:
        baseline = db.get(Evaluation, baseline_evaluation_id)
        if baseline is None:
            raise ApiError("E_NOT_FOUND", "基线 Evaluation 不存在", status_code=404)
        baseline_run = db.get(Run, baseline.run_id)
        if baseline_run is None or baseline_run.benchmark_id != benchmark.id:
            raise ApiError("E_SEMANTIC", "基线必须属于同一 Benchmark", status_code=422)

    checks: list[dict[str, Any]] = []
    for rule in policy.rules or []:
        metric = str(rule["metric"])
        subset = str(rule.get("subset", "all"))
        operator = str(rule["op"])
        raw_threshold = rule["value"]
        if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int | float):
            raise ApiError("E_GATE_POLICY_INVALID", "存储的门禁阈值非法", status_code=422)
        threshold = float(raw_threshold)
        scope = str(rule.get("scope", "absolute"))
        observed, reason = _metric_value(db, evaluation, metric, subset)
        if reason is None and observed is None:
            reason = "metric_unavailable"
        baseline_info: dict[str, Any] | None = None
        if reason is None and scope == "delta_vs_baseline":
            if baseline is None:
                reason = "baseline_not_provided"
            else:
                baseline_value, baseline_reason = _metric_value(db, baseline, metric, subset)
                if baseline_reason is not None or baseline_value is None:
                    reason = "baseline_metric_unavailable"
                else:
                    baseline_info = {
                        "evaluation_id": str(baseline.id),
                        "value": baseline_value,
                    }
                    if observed is None:
                        reason = "metric_unavailable"
                    else:
                        observed -= baseline_value

        result = "skip"
        check: dict[str, Any] = {
            "metric": metric,
            "subset": subset,
            "op": operator,
            "threshold": threshold,
            "observed": observed,
            "result": result,
        }
        if rule.get("label") is not None:
            check["label"] = rule["label"]
        if scope == "delta_vs_baseline" and baseline_info is not None:
            check["delta"] = observed
            check["baseline"] = baseline_info
        if reason is not None:
            check["reason"] = reason
            if policy.on_missing == "fail":
                check["result"] = "fail"
        elif observed is None:
            check["reason"] = "metric_unavailable"
            if policy.on_missing == "fail":
                check["result"] = "fail"
        else:
            check["result"] = "pass" if _compare(observed, operator, threshold) else "fail"
        checks.append(check)

    failed_count = sum(check["result"] == "fail" for check in checks)
    skipped_count = sum(check["result"] == "skip" for check in checks)
    return {
        "status": "fail" if failed_count else "pass",
        "policy": {"id": str(policy.id), "name": policy.name, "enabled": policy.enabled},
        "checks": checks,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
    }
