"""报告快照的落盘与查询（M6 §2/§5.8）。"""

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain import audit
from app.domain.compare import compare_evaluations, significance_vs_baseline
from app.domain.models import Evaluation, Job, Report
from app.reports.builder import (
    TEMPLATE_VERSION,
    build_payload,
    payload_hash,
    render_html,
    render_xlsx,
)


def _report_dir(report_id: uuid.UUID) -> Path:
    return get_settings().data_dir / "reports" / str(report_id)


def ensure_report_owner(report: Report, *, actor: str, role: str) -> None:
    """报告仅允许创建者或管理员删除（M7 D-M7-14）。"""
    if role == "admin" or report.created_by == actor:
        return
    raise ApiError("E_NOT_OWNER", "只能删除自己创建的报告", status_code=403)


def validate_report_request(
    db: Session,
    *,
    kind: str,
    evaluation_ids: list[uuid.UUID],
    level: str = "summary",
) -> list[Evaluation]:
    """在创建报告 Job 前校验来源、报告类型和级别。"""
    if kind not in {"single", "matrix", "compare", "regression"}:
        raise ApiError("E_SEMANTIC", f"未知快照类型 {kind}", status_code=422)
    if len(set(evaluation_ids)) != len(evaluation_ids):
        raise ApiError("E_SEMANTIC", "Evaluation 不能重复", status_code=422)
    required_count = 1 if kind in {"single", "matrix"} else 2
    if not required_count <= len(evaluation_ids) <= 4:
        raise ApiError(
            "E_SEMANTIC", f"{kind} 快照需要 {required_count}～4 个 Evaluation", status_code=422
        )
    evaluations = [db.get(Evaluation, evaluation_id) for evaluation_id in evaluation_ids]
    missing = [
        str(evaluation_ids[index])
        for index, evaluation in enumerate(evaluations)
        if evaluation is None
    ]
    if missing:
        raise ApiError(
            "E_NOT_FOUND", "Evaluation 不存在", status_code=404, detail={"evaluation_ids": missing}
        )
    complete = [evaluation for evaluation in evaluations if evaluation is not None]
    incomplete = [
        str(evaluation.id)
        for evaluation in complete
        if evaluation.status not in {"completed", "degraded"}
    ]
    if incomplete:
        raise ApiError(
            "E_SEMANTIC",
            "评分未完成，不能生成报告",
            status_code=422,
            detail={"evaluation_ids": incomplete},
        )
    if level not in {"summary", "evidence"}:
        raise ApiError("E_SEMANTIC", f"未知报告级别 {level}", status_code=422)
    return complete


def _check_report_cancellation(db: Session, job: Job | None) -> None:
    """在报告构建阶段检查统一 Job 的取消请求。"""
    if job is None:
        return
    db.refresh(job, attribute_names=["status", "cancel_requested_at"])
    if job.status == "cancelled" or job.cancel_requested_at is not None:
        raise ApiError("E_JOB_CANCELLED", "报告生成任务已取消", status_code=409)


def _set_report_phase(db: Session, job: Job | None, phase: str) -> None:
    """提交报告阶段进度，供 Job 查询和 SSE 使用。"""
    if job is None:
        return
    _check_report_cancellation(db, job)
    job.status = "running"
    job.progress = {**(job.progress or {}), "phase": phase}
    db.commit()


def create_report(
    db: Session,
    *,
    kind: str,
    evaluation_ids: list[uuid.UUID],
    query: dict[str, Any] | None = None,
    level: str = "summary",
    created_by: str | None,
    job: Job | None = None,
) -> Report:
    """生成不可变快照：payload/html/xlsx 落对象目录后落行（M6 §5.8）。

    快照内容不可变：同数据重复生成会产生新快照（input_hash 相同便于识别）。
    """
    complete_evaluations = validate_report_request(
        db, kind=kind, evaluation_ids=evaluation_ids, level=level
    )

    report_query = query or {}
    _set_report_phase(db, job, "building")
    payloads: list[dict[str, Any]] = []
    for evaluation in complete_evaluations:
        _check_report_cancellation(db, job)
        payloads.append(build_payload(db, evaluation, level=level))
    if kind == "single":
        payload = payloads[0]
    else:
        comparison = None
        significance = None
        pair_by = report_query.get("pair_by", "content_uid")
        if pair_by not in {"content_uid", "pair_key", "none"}:
            raise ApiError("E_VALIDATION", "不支持的配对方式", status_code=422)
        if len(evaluation_ids) > 1:
            comparison = compare_evaluations(db, evaluation_ids, pair_by=pair_by)
            if not comparison["comparable"]:
                raise ApiError(
                    "E_SEMANTIC",
                    "Evaluation 不可比，不能生成对比快照",
                    status_code=422,
                    detail={"blockers": comparison["blockers"]},
                )
            if kind == "regression":
                target_sets = [
                    {(target["device_id"], target["subset"]) for target in entry["targets"]}
                    for entry in comparison["evaluations"]
                ]
                if any(target_set != target_sets[0] for target_set in target_sets[1:]):
                    raise ApiError(
                        "E_SEMANTIC",
                        "回归报告要求各 Evaluation 的目标与 subset 一致",
                        status_code=422,
                    )
            if kind in {"compare", "regression"}:
                try:
                    baseline = uuid.UUID(str(report_query.get("baseline", evaluation_ids[0])))
                except ValueError as exc:
                    raise ApiError(
                        "E_VALIDATION", "基线 Evaluation ID 无效", status_code=422
                    ) from exc
                if baseline not in evaluation_ids:
                    raise ApiError("E_VALIDATION", "基线必须包含在报告来源中", status_code=422)
                raw_pairing_subsets = report_query.get("pairing_subsets", {})
                if not isinstance(raw_pairing_subsets, dict):
                    raise ApiError(
                        "E_VALIDATION",
                        "配对子集必须为 Evaluation 到 subset 的映射",
                        status_code=422,
                    )
                try:
                    pairing_subsets = {
                        uuid.UUID(str(key)): value
                        for key, value in raw_pairing_subsets.items()
                        if isinstance(value, str)
                    }
                except ValueError as exc:
                    raise ApiError(
                        "E_VALIDATION", "配对子集中的 Evaluation ID 无效", status_code=422
                    ) from exc
                if len(pairing_subsets) != len(raw_pairing_subsets):
                    raise ApiError("E_VALIDATION", "配对子集名称必须为文本", status_code=422)
                try:
                    n_resample = int(report_query.get("n_resample", 1000))
                except (TypeError, ValueError) as exc:
                    raise ApiError(
                        "E_VALIDATION", "bootstrap 次数必须为整数", status_code=422
                    ) from exc
                if not 100 <= n_resample <= 5000:
                    raise ApiError(
                        "E_VALIDATION", "bootstrap 次数须在 100～5000 之间", status_code=422
                    )
                significance = significance_vs_baseline(
                    db,
                    baseline,
                    [
                        evaluation_id
                        for evaluation_id in evaluation_ids
                        if evaluation_id != baseline
                    ],
                    n_resample=n_resample,
                    pair_by=pair_by,
                    pairing_subsets=pairing_subsets,
                )
        elif kind == "matrix":
            metric_rows = payloads[0]["metrics"]
            comparison = {
                "comparable": True,
                "blockers": [],
                "warnings": [],
                "evaluations": [
                    {
                        "id": str(complete_evaluations[0].id),
                        "run_name": payloads[0]["run"]["name"],
                        "profile_hash": complete_evaluations[0].profile_hash,
                        "scorer_version": complete_evaluations[0].scorer_version,
                        "dataset_version_id": payloads[0]["benchmark"]["dataset"],
                        "benchmark_version_hash": payloads[0]["benchmark"]["version_hash"],
                    }
                ],
                "rows": [
                    {
                        "target": metric.get("target") or "Evaluation 总计",
                        "target_id": metric.get("target_id"),
                        "subset": metric["subset"],
                        "metric": metric["metric"],
                        "scope": metric["scope"],
                        "bucket": metric["bucket"],
                        "values": [
                            {
                                "evaluation_id": str(complete_evaluations[0].id),
                                "value": metric["value"],
                                "n": metric["n"],
                                "ci_low": metric["ci_low"],
                                "ci_high": metric["ci_high"],
                                "status": metric["status"],
                                "reason": metric["reason"],
                            }
                        ],
                    }
                    for metric in metric_rows
                ],
            }
        evidence_cases = []
        case_totals: dict[str, int] = {}
        for evaluation, entry_payload in zip(complete_evaluations, payloads, strict=True):
            for status, count in entry_payload["cases"].items():
                case_totals[status] = case_totals.get(status, 0) + int(count)
            for case_row in entry_payload["case_rows"]:
                evidence_cases.append({"evaluation_id": str(evaluation.id), **case_row})
        payload = {
            "kind": kind,
            "template_version": TEMPLATE_VERSION,
            "level": level,
            "generated_at": payloads[0]["generated_at"],
            "query": report_query,
            "evaluations": payloads,
            "comparison": comparison,
            "significance": significance,
            "cases": case_totals,
            "case_rows": evidence_cases,
        }

    _check_report_cancellation(db, job)
    _set_report_phase(db, job, "rendering")
    report_id = uuid.uuid4()
    report_dir = _report_dir(report_id)
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _check_report_cancellation(db, job)
        html = render_html(payload)
        _check_report_cancellation(db, job)
        (report_dir / "report.html").write_text(html, encoding="utf-8")
        _check_report_cancellation(db, job)
        xlsx = render_xlsx(payload)
        _check_report_cancellation(db, job)
        (report_dir / "report.xlsx").write_bytes(xlsx)
        _set_report_phase(db, job, "persisting")

        report = Report(
            id=report_id,
            kind=kind,
            evaluation_ids=[str(evaluation.id) for evaluation in complete_evaluations],
            query=report_query,
            input_hash=payload_hash(payload),
            template_version=TEMPLATE_VERSION,
            level=level,
            payload_uri=str(report_dir / "payload.json"),
            html_uri=str(report_dir / "report.html"),
            xlsx_uri=str(report_dir / "report.xlsx"),
            created_by=created_by,
        )
        db.add(report)
        if job is not None:
            current_job = db.scalar(
                select(Job)
                .where(Job.id == job.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current_job is None:
                raise ApiError("E_NOT_FOUND", "报告生成任务不存在", status_code=404)
            if current_job.status == "cancelled" or current_job.cancel_requested_at is not None:
                raise ApiError("E_JOB_CANCELLED", "报告生成任务已取消", status_code=409)
            current_job.ref_type = "report"
            current_job.ref_id = str(report.id)
            current_job.status = "succeeded"
            current_job.progress = {**(current_job.progress or {}), "phase": "done"}
            current_job.result = {
                "resource_id": str(report.id),
                "url": f"/api/v1/reports/{report.id}",
            }
            current_job.finished_at = datetime.now(UTC)
        audit.record(
            db,
            "report.create",
            actor=created_by,
            resource=f"report:{report.id}",
            detail={
                "kind": kind,
                "evaluation_ids": [str(evaluation.id) for evaluation in complete_evaluations],
                "level": level,
                "input_hash": report.input_hash,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        shutil.rmtree(report_dir, ignore_errors=True)
        raise
    db.refresh(report)
    return report


def report_dict(
    db: Session,
    report: Report,
    *,
    available_evaluation_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    evaluation_ids = [uuid.UUID(str(value)) for value in report.evaluation_ids]
    if available_evaluation_ids is None:
        available_evaluation_ids = set(
            db.scalars(select(Evaluation.id).where(Evaluation.id.in_(evaluation_ids))).all()
        )
    source_deleted = not set(evaluation_ids).issubset(available_evaluation_ids)
    size_bytes = 0
    for uri in (report.payload_uri, report.html_uri, report.xlsx_uri):
        if not uri:
            continue
        try:
            size_bytes += Path(uri).stat().st_size
        except OSError:
            continue
    return {
        "id": str(report.id),
        "kind": report.kind,
        "evaluation_ids": report.evaluation_ids,
        "query": report.query,
        "input_hash": report.input_hash,
        "template_version": report.template_version,
        "level": report.level,
        "source_deleted": source_deleted,
        "created_by": report.created_by,
        "created_at": report.created_at.isoformat(),
        "size_bytes": size_bytes,
    }


def list_reports(db: Session, limit: int = 50) -> list[Report]:
    return list(db.scalars(select(Report).order_by(Report.created_at.desc()).limit(limit)).all())
