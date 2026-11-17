"""用户登录后的编辑 presence 接口。"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.auth import current_claims
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain.authorization import claim_allows_permission
from app.domain.models import Benchmark, Dataset, DatasetVersion, Device, MetricProfile
from app.domain.presence import heartbeat, leave

router = APIRouter(prefix="/api/v1", tags=["presence"])

ResourceType = Literal["benchmark", "dataset", "device", "metric_profile"]


class PresenceChange(BaseModel):
    """Presence 心跳或离开请求。"""

    model_config = ConfigDict(populate_by_name=True)

    resource_type: ResourceType = Field(alias="type")
    id: uuid.UUID


def _check_resource_access(
    db: Session,
    claims: dict[str, Any],
    resource_type: ResourceType,
    resource_id: uuid.UUID,
) -> None:
    """资源必须存在，且用户必须有对应读取权限。"""
    model = {
        "benchmark": Benchmark,
        "dataset": Dataset,
        "device": Device,
        "metric_profile": MetricProfile,
    }[resource_type]
    resource = db.get(model, resource_id)
    if resource is None:
        raise ApiError("E_NOT_FOUND", "资源不存在", status_code=404)

    permission = {
        "benchmark": "benchmark:read",
        "dataset": "dataset:read",
        "device": "device:read",
        "metric_profile": "metric_profile:read",
    }[resource_type]
    path_params: dict[str, Any] = {}
    if resource_type == "dataset":
        path_params["dataset_id"] = resource_id
    elif resource_type == "benchmark":
        benchmark = resource if isinstance(resource, Benchmark) else None
        version = db.get(DatasetVersion, benchmark.dataset_version_id) if benchmark else None
        if version is not None:
            path_params["dataset_id"] = version.dataset_id
    if not claim_allows_permission(claims, permission, path_params=path_params):
        raise ApiError("E_FORBIDDEN", "没有查看该资源的权限", status_code=403)


@router.post("/presence", status_code=204)
def heartbeat_presence(
    body: PresenceChange,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(current_claims),
) -> Response:
    """续期当前用户的编辑提示，TTL 为 90 秒。"""
    _check_resource_access(db, claims, body.resource_type, body.id)
    heartbeat(
        db,
        resource_type=body.resource_type,
        resource_id=str(body.id),
        username=str(claims["sub"]),
    )
    return Response(status_code=204)


@router.delete("/presence", status_code=204)
def leave_presence(
    body: PresenceChange,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(current_claims),
) -> Response:
    """删除当前用户在资源上的编辑提示。"""
    _check_resource_access(db, claims, body.resource_type, body.id)
    leave(
        db,
        resource_type=body.resource_type,
        resource_id=str(body.id),
        username=str(claims["sub"]),
    )
    return Response(status_code=204)


heartbeat_presence.__dict__["requires_authentication"] = True
leave_presence.__dict__["requires_authentication"] = True
