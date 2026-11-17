"""Evaluation 对比接口。"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain.compare import PairBy, compare_evaluations, significance_vs_baseline

router = APIRouter(prefix="/api/v1", tags=["compare"])


class CompareRequest(BaseModel):
    """2～4 个 Evaluation；可选择基线、配对方式与对应 subset。"""

    evaluation_ids: list[uuid.UUID] = Field(min_length=2, max_length=4)
    baseline: uuid.UUID | None = None
    pair_by: PairBy = "content_uid"
    pairing_subsets: dict[uuid.UUID, str] = Field(default_factory=dict)
    # 配对 bootstrap 次数（M6 §5.5；测试可调小）
    n_resample: int = Field(default=1000, ge=100, le=5000)


@router.post("/compare")
def compare(
    body: CompareRequest,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """可比性校验、目标指标矩阵与配对 bootstrap 显著性（M6 §5）。"""
    if body.baseline is not None and body.baseline not in body.evaluation_ids:
        raise ApiError("E_VALIDATION", "基线必须包含在本次对比中", status_code=422)
    result = compare_evaluations(db, body.evaluation_ids, pair_by=body.pair_by)
    if result["comparable"]:
        baseline = body.baseline or body.evaluation_ids[0]
        others = [
            evaluation_id for evaluation_id in body.evaluation_ids if evaluation_id != baseline
        ]
        result["significance"] = significance_vs_baseline(
            db,
            baseline,
            others,
            n_resample=body.n_resample,
            pair_by=body.pair_by,
            pairing_subsets=body.pairing_subsets,
        )
    else:
        result["significance"] = None
    return result


@router.get("/compare/available")
def compare_available(
    evaluation_ids: list[uuid.UUID] = Query(default=[]),
    pair_by: PairBy = Query(default="content_uid"),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("evaluation:read")),
) -> dict[str, Any]:
    """预检：只返回可比性与阻断原因。"""
    result = compare_evaluations(db, evaluation_ids, pair_by=pair_by)
    return {
        "comparable": result["comparable"],
        "blockers": result["blockers"],
        "warnings": result["warnings"],
        "evaluations": result["evaluations"],
    }
