"""总览页聚合读数（M8 §9.2）。"""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.db import get_db
from app.domain.models import Benchmark, Evaluation, Run
from app.domain.readout import run_readout

router = APIRouter(prefix="/api/v1/overview", tags=["overview"])


def _latest_delta(db: Session, run: Run) -> dict[str, Any] | None:
    """读取 Run 最近一次已完成评分的配对 CER 变化。"""
    evaluation = db.scalar(
        select(Evaluation)
        .where(Evaluation.run_id == run.id, Evaluation.status == "completed")
        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        .limit(1)
    )
    if evaluation is None:
        return None
    readout = run_readout(db, run, evaluation_id=evaluation.id)
    delta = readout.get("delta")
    if not isinstance(delta, dict) or not isinstance(delta.get("delta"), (int, float)):
        return None
    return {"evaluation_id": str(evaluation.id), **delta}


@router.get("/summary")
def overview_summary(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """返回近七天 Run、失败、运行中和已评分 Run 的平均 CER Δ。"""
    now = datetime.now(UTC)
    since = now - timedelta(days=7)
    run_filter = Run.created_at >= since
    run_count = int(db.scalar(select(func.count()).select_from(Run).where(run_filter)) or 0)
    active = int(
        db.scalar(
            select(func.count())
            .select_from(Run)
            .where(run_filter, Run.status.in_({"queued", "running"}))
        )
        or 0
    )
    failed = int(
        db.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                run_filter,
                Run.status.in_({"failed", "partial", "aborted_condition_changed"}),
            )
        )
        or 0
    )
    # Bootstrap 配对读数较重；总览均值基于最近 20 个已评分 Run，并返回实际 n。
    scored_runs = db.scalars(
        select(Run)
        .where(run_filter, Run.status.in_({"completed", "partial"}))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(20)
    ).all()
    deltas = [
        float(delta["delta"])
        for run in scored_runs
        if (delta := _latest_delta(db, run)) is not None
    ]
    return {
        "days": 7,
        "runs": run_count,
        "failed_runs": failed,
        "running_runs": active,
        "average_cer_delta_pp": sum(deltas) / len(deltas) * 100 if deltas else None,
        "cer_delta_n": len(deltas),
        "as_of": now.isoformat(),
    }


@router.get("/regressions")
def overview_regressions(
    days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("run:read")),
) -> dict[str, Any]:
    """列出近 N 天配对 CER 显著变差的 Run，最多返回 10 条。"""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(
        select(Run, Benchmark.name)
        .join(Benchmark, Benchmark.id == Run.benchmark_id)
        .where(
            Run.created_at >= since,
            Run.status.in_({"completed", "partial"}),
        )
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(30)
    ).all()
    regressions = []
    for run, benchmark_name in rows:
        delta = _latest_delta(db, run)
        if not delta or not delta.get("significant") or float(delta["delta"]) <= 0:
            continue
        regressions.append(
            {
                "run_id": str(run.id),
                "run_name": run.name,
                "benchmark": benchmark_name,
                "created_at": run.created_at.isoformat(),
                "evaluation_id": delta["evaluation_id"],
                "delta_pp": float(delta["delta"]) * 100,
                "ci_low_pp": float(delta["ci_low"]) * 100
                if delta.get("ci_low") is not None
                else None,
                "ci_high_pp": float(delta["ci_high"]) * 100
                if delta.get("ci_high") is not None
                else None,
                "n": int(delta.get("n", 0)),
            }
        )
        if len(regressions) == 10:
            break
    return {"days": days, "items": regressions}
