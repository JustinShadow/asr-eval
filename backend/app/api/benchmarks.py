"""口径与 Benchmark 接口。"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain import audit
from app.domain.benchmarks import (
    _resolve_subsets,
    availability,
    benchmark_dict,
    compute_version_hash,
    create_benchmark,
    create_profile,
    create_profile_version,
    delete_benchmark,
    delete_metric_profile,
    preview_benchmark,
    profile_dict,
    sampled_item_ids,
    validate_benchmark_definition,
)
from app.domain.etag import check_if_match, if_none_match_matches, make_etag, require_if_match
from app.domain.metric_profiles import (
    BENCHMARK_PRESETS,
    EVALUATORS,
    TEMPLATE_PARAMS,
    is_locked,
    profile_hash,
    validate_params,
)
from app.domain.models import Benchmark, DatasetVersion, Item, MetricProfile, Run
from app.domain.presence import active_editors

router = APIRouter(prefix="/api/v1", tags=["benchmarks"])


class ProfileCreate(BaseModel):
    """口径创建。"""

    name: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class ProfilePatch(BaseModel):
    """MetricProfile 的局部配置更新请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    params: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_fields(self) -> "ProfilePatch":
        """拒绝空更新及不可空字段的 null。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        return self


class ProfileVersionCreate(BaseModel):
    """从已有口径创建不可变的新版本。"""

    params: dict[str, Any]


class BenchmarkCreate(BaseModel):
    """Benchmark 创建。"""

    name: str = Field(min_length=1, max_length=128)
    dataset_version_id: uuid.UUID
    metric_profile_id: uuid.UUID
    # 多 subset（M3 §5.2 步骤 2）；缺省取 primary
    subsets: list[str] = Field(default_factory=list)
    sampling: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    bucket_rules: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=512)


class BenchmarkPreviewRequest(BaseModel):
    """保存前的 Benchmark 规模、分桶和设备能力预览。"""

    dataset_version_id: uuid.UUID
    metric_profile_id: uuid.UUID
    subsets: list[str] = Field(default_factory=list)
    sampling: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    bucket_rules: dict[str, Any] = Field(default_factory=dict)
    deployment_ids: list[uuid.UUID] = Field(default_factory=list)


class BenchmarkPatch(BaseModel):
    """Benchmark 草稿的局部配置更新请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    dataset_version_id: uuid.UUID | None = None
    metric_profile_id: uuid.UUID | None = None
    subsets: list[str] | None = None
    sampling: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    bucket_rules: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def check_fields(self) -> "BenchmarkPatch":
        """拒绝空更新及不可空字段的 null。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        for field in self.model_fields_set - {"note"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        return self


def _profile_configuration(profile: MetricProfile) -> dict[str, Any]:
    return {"name": profile.name, "params": profile.params, "profile_hash": profile.profile_hash}


def _profile_etag(profile: MetricProfile) -> str:
    return make_etag("m", profile.version, _profile_configuration(profile))


def _benchmark_configuration(benchmark: Benchmark) -> dict[str, Any]:
    return {
        "name": benchmark.name,
        "dataset_version_id": str(benchmark.dataset_version_id),
        "metric_profile_id": str(benchmark.metric_profile_id),
        "subsets": benchmark.subsets or [],
        "sampling": benchmark.sampling or {},
        "params": benchmark.params or {},
        "bucket_rules": benchmark.bucket_rules or {},
        "version_hash": benchmark.version_hash,
        "note": benchmark.note,
        "archived": bool(benchmark.archived),
    }


def _benchmark_etag(benchmark: Benchmark) -> str:
    return make_etag("b", benchmark.version, _benchmark_configuration(benchmark))


@router.get("/metric-profiles/templates")
def list_templates(
    _: dict[str, Any] = Depends(require("metric_profile:read")),
) -> dict[str, Any]:
    """预置口径模板与 Benchmark 预设（M3 §6/§6.1）。"""
    return {"templates": TEMPLATE_PARAMS, "presets": BENCHMARK_PRESETS}


@router.get("/metric-profiles/evaluators")
def list_evaluators(
    _: dict[str, Any] = Depends(require("metric_profile:read")),
) -> dict[str, Any]:
    """返回受版本约束的评分实现及其锁定状态/固定参数。"""
    return {
        "items": [
            {
                "id": spec.id,
                "name": spec.name,
                "locked": spec.locked,
                "description": spec.description,
                "params": spec.params,
            }
            for spec in EVALUATORS.values()
        ],
        "custom": {
            "id": "custom",
            "name": "自定义",
            "locked": False,
            "description": "可编辑的兼容口径，仅用于内部实验。",
        },
    }


@router.post("/metric-profiles/validate")
def validate_profile_params(
    body: dict[str, Any],
    _: dict[str, Any] = Depends(require("metric_profile:read")),
) -> dict[str, Any]:
    """口径语义校验（M3 §3.2/§7）：返回定位到字段路径的错误列表。"""
    errors = validate_params(body)
    return {"valid": not errors, "errors": errors}


@router.get("/metric-profiles")
def list_profiles(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("metric_profile:read")),
) -> dict[str, Any]:
    rows = db.scalars(
        select(MetricProfile).order_by(MetricProfile.name, MetricProfile.version.desc())
    ).all()
    counts = dict(
        db.execute(
            select(Benchmark.metric_profile_id, func.count(Benchmark.id)).group_by(
                Benchmark.metric_profile_id
            )
        ).all()
    )
    items = []
    for item in rows:
        payload = profile_dict(item)
        payload["benchmark_count"] = int(counts.get(item.id, 0))
        items.append(payload)
    return {"items": items}


@router.get(
    "/metric-profiles/{profile_id}",
    response_model=dict[str, Any],
    responses={304: {"description": "配置未变化"}},
)
def get_profile(
    profile_id: uuid.UUID,
    response: Response,
    if_none_match: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("metric_profile:read")),
) -> dict[str, Any] | Response:
    """返回单个口径及其配置版本。"""
    profile = db.get(MetricProfile, profile_id)
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
    etag = _profile_etag(profile)
    response.headers["ETag"] = etag
    if if_none_match_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    payload = profile_dict(profile)
    payload["benchmark_count"] = int(
        db.scalar(
            select(func.count())
            .select_from(Benchmark)
            .where(Benchmark.metric_profile_id == profile.id)
        )
        or 0
    )
    return payload


@router.delete("/metric-profiles/{profile_id}", status_code=204)
def delete_metric_profile_endpoint(
    profile_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("metric_profile:delete")),
) -> Response:
    """删除未被 Benchmark 引用的口径版本（M3 §9）。"""
    delete_metric_profile(db, profile_id, actor=str(claims.get("sub", "")))
    return Response(status_code=204)


@router.post("/metric-profiles", status_code=201)
def create_profile_endpoint(
    body: ProfileCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("metric_profile:write")),
) -> dict[str, Any]:
    profile = create_profile(db, name=body.name, params=body.params)
    profile.updated_by = str(claims.get("sub", ""))
    db.commit()
    db.refresh(profile)
    response.headers["ETag"] = _profile_etag(profile)
    return profile_dict(profile)


@router.patch(
    "/metric-profiles/{profile_id}",
    responses={
        409: {"description": "口径已被 Benchmark 引用或名称冲突"},
        412: {"description": "ETag 已过期，响应 detail 携带当前对象"},
        428: {"description": "缺少 If-Match"},
    },
)
def patch_profile(
    profile_id: uuid.UUID,
    body: ProfilePatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    force: bool = Query(default=False),
    reason: str | None = Query(default=None, min_length=3, max_length=500),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("metric_profile:write")),
) -> dict[str, Any]:
    """带 ETag 更新尚未被 Benchmark 引用的口径。"""
    supplied_etag = require_if_match(if_match)
    profile = db.scalar(
        select(MetricProfile).where(MetricProfile.id == profile_id).with_for_update()
    )
    if profile is None:
        raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
    if is_locked(profile.params):
        raise ApiError(
            "E_EVALUATOR_LOCKED",
            "官方/预设口径不可修改；请新建自定义口径",
            status_code=409,
        )

    current = profile_dict(profile)
    current_etag = _profile_etag(profile)
    updates = body.model_dump(exclude_unset=True)
    forced = check_if_match(
        supplied_etag,
        current_etag,
        current=current,
        attempted=updates,
        force=force,
        reason=reason,
    )
    changed_fields = [key for key, value in updates.items() if getattr(profile, key) != value]
    if changed_fields:
        if "name" in updates:
            duplicate = db.scalar(
                select(MetricProfile.id).where(
                    MetricProfile.name == updates["name"],
                    MetricProfile.version == profile.version,
                    MetricProfile.id != profile.id,
                )
            )
            if duplicate is not None:
                raise ApiError("E_CONFLICT", "口径名称已存在", status_code=409)
        if "params" in updates:
            errors = validate_params(updates["params"])
            if errors:
                raise ApiError(
                    "E_VALIDATION", "口径参数非法", status_code=422, detail={"errors": errors}
                )
        referenced = db.scalar(
            select(Benchmark.id).where(Benchmark.metric_profile_id == profile.id).limit(1)
        )
        if referenced is not None:
            raise ApiError(
                "E_VERSION_IN_USE",
                "口径已被 Benchmark 引用；请创建新版本以保留历史定义",
                status_code=409,
            )

        for key, value in updates.items():
            setattr(profile, key, value)
        profile.profile_hash = profile_hash(profile.params)
        profile.version += 1
        profile.updated_by = str(claims.get("sub", ""))
        profile.updated_at = datetime.now(UTC)
        audit.record(
            db,
            "metric_profile.config.updated",
            actor=profile.updated_by,
            resource=f"metric_profile:{profile.id}",
            detail={
                "changed_fields": changed_fields,
                "version": profile.version,
                "force": forced,
                **({"reason": reason.strip()} if forced and reason else {}),
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(profile)

    response.headers["ETag"] = _profile_etag(profile)
    return profile_dict(profile)


@router.post("/metric-profiles/{profile_id}/versions", status_code=201)
def create_profile_version_endpoint(
    profile_id: uuid.UUID,
    body: ProfileVersionCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("metric_profile:write")),
) -> dict[str, Any]:
    """创建口径新版本；原版本与引用它的 Benchmark 保持不变。"""
    profile = create_profile_version(
        db,
        profile_id,
        params=body.params,
        actor=str(claims.get("sub", "")),
    )
    response.headers["ETag"] = _profile_etag(profile)
    return profile_dict(profile)


@router.get("/benchmarks")
def list_benchmarks(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("benchmark:read")),
) -> dict[str, Any]:
    rows = db.scalars(select(Benchmark).order_by(Benchmark.created_at.desc())).all()
    items = []
    for benchmark in rows:
        payload = benchmark_dict(db, benchmark)
        sample_ids = sampled_item_ids(db, benchmark)
        selected_duration = (
            db.scalar(
                select(func.coalesce(func.sum(Item.duration_ms), 0)).where(Item.id.in_(sample_ids))
            )
            if sample_ids
            else 0
        )
        payload["selected_count"] = len(sample_ids)
        payload["selected_duration_ms"] = int(selected_duration or 0)
        items.append(payload)
    return {"items": items}


@router.post("/benchmarks", status_code=201)
def create_benchmark_endpoint(
    body: BenchmarkCreate,
    response: Response,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("benchmark:write")),
) -> dict[str, Any]:
    benchmark = create_benchmark(
        db,
        name=body.name,
        dataset_version_id=body.dataset_version_id,
        metric_profile_id=body.metric_profile_id,
        subsets=body.subsets,
        sampling=body.sampling,
        params=body.params,
        bucket_rules=body.bucket_rules,
        note=body.note,
    )
    response.headers["ETag"] = _benchmark_etag(benchmark)
    return benchmark_dict(db, benchmark)


@router.post("/benchmarks/preview")
def preview_benchmark_endpoint(
    body: BenchmarkPreviewRequest,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("benchmark:read")),
) -> dict[str, Any]:
    """预览规模、分桶和每个目标部署的指标可用性，不写数据库。"""
    return preview_benchmark(
        db,
        dataset_version_id=body.dataset_version_id,
        metric_profile_id=body.metric_profile_id,
        subsets=body.subsets,
        sampling=body.sampling,
        bucket_rules=body.bucket_rules,
        params=body.params,
        deployment_ids=body.deployment_ids,
    )


@router.delete("/benchmarks/{benchmark_id}", status_code=204)
def delete_benchmark_endpoint(
    benchmark_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("benchmark:delete")),
) -> Response:
    """删除未被 Run 引用的基准（M3 §9）。"""
    delete_benchmark(db, benchmark_id, actor=str(claims.get("sub", "")))
    return Response(status_code=204)


@router.post("/benchmarks/{benchmark_id}/archive")
def archive_benchmark(
    benchmark_id: uuid.UUID,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("benchmark:write")),
) -> dict[str, Any]:
    """归档 Benchmark；历史 Run 保留对原版本的读取能力。"""
    benchmark = db.scalar(select(Benchmark).where(Benchmark.id == benchmark_id).with_for_update())
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    if not benchmark.archived:
        benchmark.archived = True
        benchmark.version += 1
        benchmark.updated_by = str(claims.get("sub", ""))
        benchmark.updated_at = datetime.now(UTC)
        audit.record(
            db,
            "benchmark.archived",
            actor=benchmark.updated_by,
            resource=f"benchmark:{benchmark.id}",
            detail={"version": benchmark.version},
            ip=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(benchmark)
    response.headers["ETag"] = _benchmark_etag(benchmark)
    return benchmark_dict(db, benchmark)


@router.get("/benchmarks/{benchmark_id}/availability")
def benchmark_availability(
    benchmark_id: uuid.UUID,
    deployment_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("benchmark:read")),
) -> dict[str, Any]:
    """给定部署返回各指标可用性。"""
    benchmark = db.get(Benchmark, benchmark_id)
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    return availability(db, benchmark, deployment_id)


@router.get(
    "/benchmarks/{benchmark_id}",
    response_model=dict[str, Any],
)
def get_benchmark(
    benchmark_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("benchmark:read")),
) -> dict[str, Any]:
    """返回配置详情和 presence；每次读取刷新在线编辑者列表。"""
    benchmark = db.get(Benchmark, benchmark_id)
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    etag = _benchmark_etag(benchmark)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
    payload = benchmark_dict(db, benchmark)
    payload["editors"] = active_editors(
        db,
        resource_type="benchmark",
        resource_id=str(benchmark.id),
    )
    return payload


@router.patch(
    "/benchmarks/{benchmark_id}",
    responses={
        409: {"description": "Benchmark 已被 Run 使用或名称冲突"},
        412: {"description": "ETag 已过期，响应 detail 携带当前对象"},
        428: {"description": "缺少 If-Match"},
    },
)
def patch_benchmark(
    benchmark_id: uuid.UUID,
    body: BenchmarkPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    force: bool = Query(default=False),
    reason: str | None = Query(default=None, min_length=3, max_length=500),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("benchmark:write")),
) -> dict[str, Any]:
    """更新未被 Run 使用的 Benchmark 草稿并重算版本 hash。"""
    supplied_etag = require_if_match(if_match)
    updates = body.model_dump(exclude_unset=True)
    attempted = {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in updates.items()
    }
    initial_profile_id = db.scalar(
        select(Benchmark.metric_profile_id).where(Benchmark.id == benchmark_id)
    )
    if initial_profile_id is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)
    profile_ids = {initial_profile_id}
    if "metric_profile_id" in updates:
        profile_ids.add(updates["metric_profile_id"])
    locked_profiles: dict[uuid.UUID, MetricProfile] = {}
    for profile_id in sorted(profile_ids, key=str):
        locked_profile = db.scalar(
            select(MetricProfile).where(MetricProfile.id == profile_id).with_for_update()
        )
        if locked_profile is not None:
            locked_profiles[profile_id] = locked_profile

    benchmark = db.scalar(select(Benchmark).where(Benchmark.id == benchmark_id).with_for_update())
    if benchmark is None:
        raise ApiError("E_NOT_FOUND", "Benchmark 不存在", status_code=404)

    current = benchmark_dict(db, benchmark)
    current_etag = _benchmark_etag(benchmark)
    forced = check_if_match(
        supplied_etag,
        current_etag,
        current=current,
        attempted=attempted,
        force=force,
        reason=reason,
    )
    if benchmark.metric_profile_id not in locked_profiles:
        raise ApiError(
            "E_PRECONDITION_FAILED",
            "Benchmark 口径已并发变更，请重新读取后再更新",
            status_code=412,
            detail={
                "current": current,
                "current_etag": current_etag,
                "changed_fields": ["metric_profile_id"],
                "updated_by": current.get("updated_by"),
                "updated_at": current.get("updated_at"),
            },
        )
    changed_fields = [key for key, value in updates.items() if getattr(benchmark, key) != value]

    if changed_fields:
        if benchmark.archived:
            raise ApiError(
                "E_CONFLICT",
                "已归档基准仅供历史查看，不能修改定义",
                status_code=409,
            )
        if db.scalar(select(Run.id).where(Run.benchmark_id == benchmark.id).limit(1)):
            raise ApiError(
                "E_VERSION_IN_USE",
                "已有 Run 使用该 Benchmark，不能修改其评测定义",
                status_code=409,
            )
        dataset_version_id = updates.get("dataset_version_id", benchmark.dataset_version_id)
        dataset_version = db.get(DatasetVersion, dataset_version_id)
        if dataset_version is None:
            raise ApiError("E_NOT_FOUND", "数据集版本不存在", status_code=404)
        if dataset_version.status not in {"ready", "ready_with_errors"}:
            raise ApiError("E_CONFLICT", "数据集版本未就绪", status_code=409)
        metric_profile_id = updates.get("metric_profile_id", benchmark.metric_profile_id)
        profile = locked_profiles.get(metric_profile_id)
        if profile is None:
            raise ApiError("E_NOT_FOUND", "口径不存在", status_code=404)
        target_name = updates.get("name", benchmark.name)
        duplicate = db.scalar(
            select(Benchmark.id).where(
                Benchmark.dataset_version_id == dataset_version_id,
                Benchmark.name == target_name,
                Benchmark.id != benchmark.id,
            )
        )
        if duplicate is not None:
            raise ApiError("E_CONFLICT", "该版本下已存在同名 Benchmark", status_code=409)
        target_subsets = _resolve_subsets(
            db,
            dataset_version.id,
            updates.get("subsets", benchmark.subsets or []),
        )
        target_sampling = updates.get("sampling", benchmark.sampling or {})
        target_bucket_rules = updates.get("bucket_rules", benchmark.bucket_rules or {})
        errors = validate_benchmark_definition(target_sampling, target_bucket_rules)
        if errors:
            raise ApiError(
                "E_VALIDATION", "基准参数非法", status_code=422, detail={"errors": errors}
            )
        for key, value in updates.items():
            setattr(benchmark, key, value)
        benchmark.subsets = target_subsets
        benchmark.version_hash = compute_version_hash(
            db,
            dataset_version_id=benchmark.dataset_version_id,
            subsets=benchmark.subsets or [],
            profile_hash=profile.profile_hash,
            sampling=benchmark.sampling or {},
            bucket_rules=benchmark.bucket_rules or {},
            params=benchmark.params or {},
        )
        benchmark.version += 1
        benchmark.updated_by = str(claims.get("sub", ""))
        benchmark.updated_at = datetime.now(UTC)
        audit.record(
            db,
            "benchmark.config.updated",
            actor=benchmark.updated_by,
            resource=f"benchmark:{benchmark.id}",
            detail={
                "changed_fields": changed_fields,
                "version": benchmark.version,
                "force": forced,
                **({"reason": reason.strip()} if forced and reason else {}),
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(benchmark)

    response.headers["ETag"] = _benchmark_etag(benchmark)
    return benchmark_dict(db, benchmark)
