"""Evaluation 取消与续跑状态转换。"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain import audit
from app.domain.models import Evaluation, Job, Report


def cancel_evaluation(db: Session, evaluation_id: uuid.UUID) -> Evaluation:
    """queued 任务立即取消；运行中任务在当前评分页完成后停止。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    if evaluation.status == "queued":
        evaluation.status = "cancelled"
        evaluation.finished_at = datetime.now(UTC)
        evaluation.progress = {**evaluation.progress, "phase": "cancelled"}
    elif evaluation.status == "scoring":
        evaluation.status = "cancelling"
    db.commit()
    db.refresh(evaluation)
    return evaluation


def delete_evaluation(db: Session, evaluation_id: uuid.UUID, *, actor: str) -> None:
    """删除终态评分；活动评分或被报告/报告任务引用时拒绝（M5 §9/M6 §5.8）。"""
    evaluation = db.scalar(
        select(Evaluation).where(Evaluation.id == evaluation_id).with_for_update()
    )
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    if evaluation.status in {"queued", "scoring", "cancelling"}:
        raise ApiError(
            "E_EVALUATION_ACTIVE",
            "评分仍在排队或执行，请先取消并等待结束后再删除",
            status_code=409,
        )
    evaluation_id_text = str(evaluation.id)
    report_id = db.scalar(
        select(Report.id).where(Report.evaluation_ids.contains([evaluation_id_text])).limit(1)
    )
    active_report_job_id = db.scalar(
        select(Job.id)
        .where(
            Job.kind == "report",
            Job.status.in_(("queued", "running")),
            or_(
                Job.ref_id == evaluation_id_text,
                Job.progress.contains({"evaluation_ids": [evaluation_id_text]}),
            ),
        )
        .limit(1)
    )
    if report_id is not None or active_report_job_id is not None:
        raise ApiError(
            "E_EVALUATION_REFERENCED",
            "评分已被报告引用或正在生成报告，请先删除或等待报告后再删除",
            status_code=409,
            detail={
                "report_id": str(report_id) if report_id else None,
                "job_id": str(active_report_job_id) if active_report_job_id else None,
            },
        )
    audit.record(
        db,
        "evaluation.delete",
        actor=actor,
        resource=f"evaluation:{evaluation.id}",
        detail={
            "run_id": str(evaluation.run_id),
            "status": evaluation.status,
            "scorer_version": evaluation.scorer_version,
        },
    )
    db.delete(evaluation)
    db.commit()


def resume_evaluation(db: Session, evaluation_id: uuid.UUID) -> Evaluation:
    """恢复 failed/cancelled 任务，保留最后已提交的 case_result 游标。"""
    evaluation = db.get(Evaluation, evaluation_id)
    if evaluation is None:
        raise ApiError("E_NOT_FOUND", "Evaluation 不存在", status_code=404)
    if evaluation.status not in {"failed", "cancelled"}:
        raise ApiError(
            "E_CONFLICT",
            "只有 failed 或 cancelled 的 Evaluation 可以续跑",
            status_code=409,
        )
    evaluation.status = "queued"
    evaluation.finished_at = None
    evaluation.progress = {
        **evaluation.progress,
        "phase": "queued",
    }
    metrics = dict(evaluation.metrics)
    metrics.pop("error", None)
    evaluation.metrics = metrics
    db.commit()
    db.refresh(evaluation)
    return evaluation
