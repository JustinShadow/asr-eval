"""报告快照接口（M6 §3.1、M7 D-M7-12）。"""

import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import ApiError
from app.core.rate_limit import rate_limiter, require_limit
from app.domain import audit
from app.domain.idempotency import execute_idempotently
from app.domain.imports import job_dict
from app.domain.models import Evaluation, Report
from app.reports.service import ensure_report_owner, list_reports, report_dict
from app.reports.tasks import start_report

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])

# 报告 HTML 只用于下载与 sandbox 预览：禁止脚本/外链，主域不内联渲染（D-M7-12）
_HTML_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src 'none'"


class ReportCreate(BaseModel):
    """生成快照（M6 §5.8）。"""

    kind: Literal["single", "matrix", "compare", "regression"] = "single"
    evaluation_ids: list[uuid.UUID] = Field(min_length=1, max_length=4)
    query: dict[str, Any] = Field(default_factory=dict)
    level: str = Field(default="summary", max_length=16)


@router.post("", status_code=202)
def create_report_endpoint(
    body: ReportCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("report:create")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """异步生成不可变报告快照，返回统一 Job 资源。"""

    def create() -> dict[str, Any]:
        require_limit(
            rate_limiter.consume(f"report:{claims['sub']}", 10, 3600),
            message="报告生成次数已达上限",
        )
        job = start_report(
            db,
            kind=body.kind,
            evaluation_ids=body.evaluation_ids,
            query=body.query,
            level=body.level,
            created_by=str(claims.get("sub")) or None,
        )
        return job_dict(job)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("")
def list_reports_endpoint(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("report:read")),
) -> dict[str, Any]:
    """报告快照列表，最新在前。"""
    reports = list_reports(db, limit)
    evaluation_ids = {
        uuid.UUID(str(evaluation_id))
        for report in reports
        for evaluation_id in report.evaluation_ids
    }
    available_evaluation_ids = (
        set(db.scalars(select(Evaluation.id).where(Evaluation.id.in_(evaluation_ids))).all())
        if evaluation_ids
        else set()
    )
    return {
        "items": [
            report_dict(db, report, available_evaluation_ids=available_evaluation_ids)
            for report in reports
        ]
    }


@router.get("/{report_id}")
def get_report(
    report_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("report:read")),
) -> dict[str, Any]:
    report = db.get(Report, report_id)
    if report is None:
        raise ApiError("E_NOT_FOUND", "报告不存在", status_code=404)
    return report_dict(db, report)


@router.get("/{report_id}/html")
def get_report_html(
    report_id: uuid.UUID,
    inline: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("report:read")),
) -> FileResponse:
    """下载/沙箱预览 HTML（自带 CSP sandbox，不在主域内联执行脚本）。"""
    report = db.get(Report, report_id)
    if report is None or not report.html_uri or not Path(report.html_uri).is_file():
        raise ApiError("E_NOT_FOUND", "报告文件不存在", status_code=404)
    return FileResponse(
        Path(report.html_uri),
        media_type="text/html",
        filename=None if inline else f"report-{report.id}.html",
        headers={
            "Content-Security-Policy": _HTML_CSP,
            "X-Content-Type-Options": "nosniff",
            **(
                {}
                if inline
                else {"Content-Disposition": f'attachment; filename="report-{report.id}.html"'}
            ),
        },
    )


@router.get("/{report_id}/xlsx")
def get_report_xlsx(
    report_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("report:read")),
) -> FileResponse:
    """下载 XLSX（Summary / Buckets / Cases 三个 sheet）。"""
    report = db.get(Report, report_id)
    if report is None or not report.xlsx_uri or not Path(report.xlsx_uri).is_file():
        raise ApiError("E_NOT_FOUND", "报告文件不存在", status_code=404)
    return FileResponse(
        Path(report.xlsx_uri),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"report-{report.id}.xlsx",
    )


@router.delete("/{report_id}", status_code=204)
def delete_report(
    report_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("report:delete")),
) -> None:
    """删除快照（内容不可变，删除仅用于清理；写审计）。"""
    report = db.get(Report, report_id)
    if report is None:
        raise ApiError("E_NOT_FOUND", "报告不存在", status_code=404)
    ensure_report_owner(
        report,
        actor=str(claims.get("sub", "")),
        role=str(claims.get("role", "")),
    )
    audit.record(
        db,
        "report.delete",
        actor=str(claims.get("sub")) or None,
        resource=f"report:{report.id}",
        detail={"evaluation_ids": report.evaluation_ids},
    )
    db.delete(report)
    db.commit()
    report_dir = get_settings().data_dir / "reports" / str(report_id)
    if report_dir.is_dir():
        shutil.rmtree(report_dir)
