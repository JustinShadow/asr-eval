"""数据集、版本、样本与导入预览接口。"""

import base64
import binascii
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain.benchmarks import bucket_coverage
from app.domain.datasets import (
    create_dataset,
    delete_dataset,
    delete_version,
    preview_import,
    version_diff,
)
from app.domain.idempotency import execute_idempotently
from app.domain.imports import job_dict, start_import
from app.domain.models import (
    AudioAsset,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    RefSegment,
    RefText,
    SpeakerMeta,
)
from app.domain.reanalysis import start_reanalysis

router = APIRouter(prefix="/api/v1", tags=["datasets"])
DatasetSource = Literal[
    "aishell1", "aishell3", "kespeech", "alimeeting", "magicdata", "apy", "datatang", "srt"
]


class DatasetCreate(BaseModel):
    """创建数据集。"""

    name: str = Field(min_length=1, max_length=128)
    source_type: DatasetSource | None = None
    description: str | None = Field(default=None, max_length=1024)


class ImportRequest(BaseModel):
    """导入数据集版本与 M8 向导参数；limit 控制本次扫描条数。"""

    root: str = Field(min_length=1, max_length=1024)
    limit: int = Field(default=1000, ge=1, le=20000)
    version: str | None = Field(default=None, max_length=64)
    source_type: DatasetSource | Literal["auto"] | None = None
    allow_missing_ref: bool = False
    subset_mapping: dict[str, Literal["phone", "mic", "near", "far", "all"]] = Field(
        default_factory=dict
    )
    audio_profile_overrides: dict[str, Literal["as_is", "mono_mix", "mono_ch0"]] = Field(
        default_factory=dict
    )


def _dataset_dict(dataset: Dataset) -> dict[str, Any]:
    return {
        "id": str(dataset.id),
        "name": dataset.name,
        "source_type": dataset.source_type,
        "description": dataset.description,
        "tags": dataset.tags,
        "created_at": dataset.created_at.isoformat(),
    }


def _version_dict(version: DatasetVersion) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "dataset_id": str(version.dataset_id),
        "version": version.version,
        "status": version.status,
        "item_count": version.item_count,
        "total_duration_ms": version.total_duration_ms,
        "capabilities": version.capabilities,
        "stats": version.stats,
        "created_at": version.created_at.isoformat(),
    }


@router.get("/datasets")
def list_datasets(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """数据集列表。"""
    items = db.scalars(select(Dataset).order_by(Dataset.name)).all()
    dataset_ids = [item.id for item in items]
    versions = (
        db.scalars(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id.in_(dataset_ids))
            .order_by(DatasetVersion.created_at.desc())
        ).all()
        if dataset_ids
        else []
    )
    versions_by_dataset: dict[uuid.UUID, list[DatasetVersion]] = {}
    for version in versions:
        versions_by_dataset.setdefault(version.dataset_id, []).append(version)
    benchmark_counts = (
        dict(
            db.execute(
                select(DatasetVersion.dataset_id, func.count(Benchmark.id))
                .join(Benchmark, Benchmark.dataset_version_id == DatasetVersion.id)
                .where(DatasetVersion.dataset_id.in_(dataset_ids))
                .group_by(DatasetVersion.dataset_id)
            ).all()
        )
        if dataset_ids
        else {}
    )
    result = []
    for dataset in items:
        dataset_versions = versions_by_dataset.get(dataset.id, [])
        latest = dataset_versions[0] if dataset_versions else None
        result.append(
            {
                **_dataset_dict(dataset),
                "version_count": len(dataset_versions),
                "reference_count": int(benchmark_counts.get(dataset.id, 0)),
                "latest_version": _version_dict(latest) if latest else None,
            }
        )
    return {"items": result}


@router.post("/datasets", status_code=201)
def create_dataset_endpoint(
    body: DatasetCreate,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:write")),
) -> dict[str, Any]:
    """创建数据集。"""
    dataset = create_dataset(
        db, name=body.name, source_type=body.source_type, description=body.description
    )
    return _dataset_dict(dataset)


@router.get("/datasets/{dataset_id}")
def get_dataset(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """数据集详情。"""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ApiError("E_NOT_FOUND", "数据集不存在", status_code=404)
    return _dataset_dict(dataset)


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset_endpoint(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("dataset:delete")),
) -> Response:
    """整组未被基准或活动任务引用时，删除数据集及其所有版本。"""
    delete_dataset(db, dataset_id, actor=str(claims.get("sub")) or None)
    return Response(status_code=204)


@router.post("/datasets/{dataset_id}/versions", status_code=202)
def import_version(
    dataset_id: uuid.UUID,
    body: ImportRequest,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("dataset:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """按 M1/M7 契约异步创建数据集版本并返回统一 Job。"""

    def create() -> dict[str, Any]:
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise ApiError("E_NOT_FOUND", "数据集不存在", status_code=404)
        task = start_import(
            db,
            dataset,
            root=Path(body.root).expanduser(),
            limit=body.limit,
            version=body.version,
            created_by=str(claims.get("sub")),
            source_type=body.source_type,
            allow_missing_ref=body.allow_missing_ref,
            subset_mapping=body.subset_mapping,
            audio_profile_overrides=body.audio_profile_overrides,
        )
        return job_dict(task)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"dataset_id": str(dataset_id), **body.model_dump(mode="json")},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.post("/datasets/{dataset_id}/preview")
def preview_import_endpoint(
    dataset_id: uuid.UUID,
    body: ImportRequest,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:write")),
) -> dict[str, Any]:
    """导入前预览（M1 §5.2）：只解析前 50 条，不落库。"""
    dataset = db.get(Dataset, dataset_id)
    if dataset is None:
        raise ApiError("E_NOT_FOUND", "数据集不存在", status_code=404)
    root = Path(body.root).expanduser()
    return preview_import(
        db,
        dataset,
        root=root,
        limit=50,
        source_type=body.source_type,
        allow_missing_ref=body.allow_missing_ref,
        subset_mapping=body.subset_mapping,
        audio_profile_overrides=body.audio_profile_overrides,
    )


@router.get("/dataset-versions/{version_id}/subsets")
def list_subsets(
    version_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """版本内 subset 列表（M1 §3.3）。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    rows = db.scalars(
        select(DatasetSubset).where(DatasetSubset.dataset_version_id == version_id)
    ).all()
    return {
        "items": [
            {
                "id": str(subset.id),
                "name": subset.name,
                "audio_profile": subset.audio_profile,
                "is_primary": subset.is_primary,
                "item_count": subset.item_count,
                "total_duration_ms": subset.total_duration_ms,
                "capabilities": subset.capabilities,
            }
            for subset in rows
        ]
    }


@router.get("/dataset-versions/{version_id}/diff")
def diff_version(
    version_id: uuid.UUID,
    against: uuid.UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """版本 diff（M1 §5.8、M8 §9.8）：完整计数并按不透明游标分页。"""
    version = db.get(DatasetVersion, version_id)
    other = db.get(DatasetVersion, against)
    if version is None or other is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    if version.dataset_id != other.dataset_id:
        raise ApiError("E_SEMANTIC", "只能对比同一数据集的版本", status_code=422)
    result = version_diff(
        db,
        other,
        version,
        limit=limit,
        offset=_decode_diff_cursor(cursor) if cursor else 0,
    )
    next_offset = result.pop("next_offset")
    previous_offset = result.pop("previous_offset")
    result["next_cursor"] = _encode_diff_cursor(next_offset) if next_offset is not None else None
    result["previous_cursor"] = (
        _encode_diff_cursor(previous_offset) if previous_offset is not None else None
    )
    return result


@router.get("/datasets/{dataset_id}/versions")
def list_versions(
    dataset_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """版本列表。"""
    rows = db.scalars(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.created_at.desc())
    ).all()
    return {"items": [_version_dict(item) for item in rows]}


@router.get("/dataset-versions/{version_id}")
def get_version(
    version_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """版本详情。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    return _version_dict(version)


@router.get("/dataset-versions/{version_id}/capabilities")
def get_version_capabilities(
    version_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """返回版本汇总与 subset 能力；指标门控必须使用 subset 级结果（M1 §5.5/§5.6）。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    subsets = db.scalars(
        select(DatasetSubset)
        .where(DatasetSubset.dataset_version_id == version_id)
        .order_by(DatasetSubset.name)
    ).all()
    return {
        "dataset_version_id": str(version.id),
        "capabilities": version.capabilities,
        "subsets": [
            {
                "name": subset.name,
                "is_primary": subset.is_primary,
                "capabilities": subset.capabilities,
            }
            for subset in subsets
        ],
    }


@router.get("/dataset-versions/{version_id}/bucket-coverage")
def get_bucket_coverage(
    version_id: uuid.UUID,
    subsets: str | None = Query(default=None, max_length=256),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """返回所选子集下各分桶维度的有值覆盖率，供基准编辑器自动勾选。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    names = [value.strip() for value in (subsets or "").split(",") if value.strip()] or ["all"]
    return bucket_coverage(db, dataset_version_id=version_id, subsets=names)


@router.delete("/dataset-versions/{version_id}", status_code=204)
def delete_dataset_version(
    version_id: uuid.UUID,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("dataset:delete")),
) -> None:
    """删除未被评测定义或运行任务引用的版本，并写审计日志。"""
    delete_version(db, version_id, actor=str(claims.get("sub")) or None)


@router.post("/dataset-versions/{version_id}/reanalyze", status_code=202)
def reanalyze_dataset_version(
    version_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("dataset:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """提交异步重新分析，返回统一 Job 并支持幂等键（M1 §3.3/M7 §6）。"""

    def create() -> dict[str, Any]:
        return job_dict(
            start_reanalysis(
                db,
                version_id,
                created_by=str(claims.get("sub")) or None,
            )
        )

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"dataset_version_id": str(version_id)},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("/dataset-versions/{version_id}/items")
def list_items(
    version_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    split: str | None = Query(default=None, max_length=32),
    bucket: list[str] | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """按不透明游标分页，并支持 split、bucket 与文本/键检索（M1 §3.3）。"""
    if db.get(DatasetVersion, version_id) is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    statement = (
        select(Item, RefText.text)
        .join(RefText, and_(RefText.item_id == Item.id, RefText.kind == "verbatim"))
        .where(Item.dataset_version_id == version_id)
    )
    if split:
        statement = statement.where(Item.split == split)
    if q:
        pattern = f"%{q}%"
        statement = statement.where(or_(Item.item_key.ilike(pattern), RefText.text.ilike(pattern)))
    for filter_value in bucket or []:
        dimension, separator, value = filter_value.partition(":")
        if not separator or not dimension or not value:
            raise ApiError("E_VALIDATION", "bucket 格式应为 dimension:value", status_code=400)
        statement = statement.where(Item.buckets[dimension].astext == value)
    if cursor:
        statement = statement.where(Item.id > _decode_item_cursor(cursor))
    rows = db.execute(statement.order_by(Item.id).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": item.id,
            "item_key": item.item_key,
            "split": item.split,
            "duration_ms": item.duration_ms,
            "speakers": item.speakers,
            "buckets": item.buckets,
            "ref_text": text,
        }
        for item, text in rows
    ]
    next_cursor = _encode_item_cursor(rows[-1][0].id) if has_more and rows else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/items/{item_id}")
def get_item(
    item_id: int,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("dataset:read")),
) -> dict[str, Any]:
    """返回样本参考文本、时间分段、说话人属性与音频技术信息（M1 §3.2/§3.3）。"""
    item = db.get(Item, item_id)
    if item is None:
        raise ApiError("E_NOT_FOUND", "item 不存在", status_code=404)
    subset = db.get(DatasetSubset, item.subset_id)
    asset = db.get(AudioAsset, item.audio_asset_id)
    references = {
        ref.kind: ref.text
        for ref in db.scalars(select(RefText).where(RefText.item_id == item_id)).all()
    }
    segments = db.scalars(
        select(RefSegment)
        .where(RefSegment.item_id == item_id)
        .order_by(RefSegment.start_ms, RefSegment.seq)
    ).all()
    speaker_rows = db.scalars(
        select(SpeakerMeta)
        .where(
            SpeakerMeta.dataset_version_id == item.dataset_version_id,
            SpeakerMeta.speaker_id.in_(item.speakers),
        )
        .order_by(SpeakerMeta.speaker_id)
    ).all()
    return {
        "id": item.id,
        "dataset_version_id": str(item.dataset_version_id),
        "subset": subset.name if subset else None,
        "item_key": item.item_key,
        "pair_key": item.pair_key,
        "split": item.split,
        "duration_ms": item.duration_ms,
        "lang": item.lang,
        "speakers": item.speakers,
        "buckets": item.buckets,
        "ref_text": references.get("verbatim"),
        "itn_text": references.get("itn", references.get("itn_expected")),
        "references": references,
        "segments": [
            {
                "seq": segment.seq,
                "speaker": segment.speaker,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
                "skip": segment.skip,
                "overlap": segment.overlap,
            }
            for segment in segments
        ],
        "speaker_meta": {
            speaker.speaker_id: {
                "gender": speaker.gender,
                "age": speaker.age,
                "accent": speaker.accent,
                "meta": speaker.meta,
            }
            for speaker in speaker_rows
        },
        "audio": {
            "sample_rate": asset.sample_rate,
            "channels": asset.channels,
            "codec": asset.codec,
            "bytes": asset.bytes,
        }
        if asset
        else None,
    }


def _encode_item_cursor(item_id: int) -> str:
    """将稳定的 item.id 排序键封装为不透明游标（M7 §4.4）。"""
    payload = json.dumps({"sort_value": item_id, "id": item_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_item_cursor(cursor: str) -> int:
    """校验游标结构，错误统一按 API 输入错误返回。"""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        item_id = int(payload["id"])
        sort_value = int(payload["sort_value"])
        if item_id < 1 or sort_value != item_id:
            raise ValueError("无效游标")
        return item_id
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ApiError("E_VALIDATION", "非法游标", status_code=400) from exc


def _encode_diff_cursor(offset: int) -> str:
    """将 Diff 明细偏移量编码为可往返的不透明游标。"""
    payload = json.dumps({"offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_diff_cursor(cursor: str) -> int:
    """校验 Diff 游标，错误统一返回 API 输入错误。"""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        offset = payload["offset"]
        if type(offset) is not int or offset < 0:
            raise ValueError("无效游标")
        return offset
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise ApiError("E_VALIDATION", "非法 Diff 游标", status_code=400) from exc
