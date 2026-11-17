"""设备台账与探测接口。

契约见 docs/m2-device-design.md §3.3；探测模式由设备领域服务校验。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require
from app.api.idempotency import apply_idempotent_response
from app.core.db import get_db
from app.core.errors import ApiError
from app.domain import audit
from app.domain.credentials import (
    credential_kind_for_device,
    remove_device_credential,
    save_device_credential,
)
from app.domain.device_ops import OpsName, list_device_ops, ops_action_dict, start_device_operation
from app.domain.devices import start_probe, validate_probe_request
from app.domain.etag import check_if_match, if_none_match_matches, make_etag, require_if_match
from app.domain.idempotency import execute_idempotently
from app.domain.imports import job_dict
from app.domain.models import (
    Deployment,
    Device,
    DeviceLease,
    Job,
    OpsAction,
    ProbeRun,
    Run,
    RunTarget,
)
from app.domain.voiceprint_checks import start_voiceprint_check, validate_voiceprint_check_request

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])
deployment_router = APIRouter(prefix="/api/v1/deployments", tags=["devices"])

Platform = Literal["205", "600", "502"]
DeviceKind = Literal["etsstt", "gateway", "cloud"]


class DeviceCreate(BaseModel):
    """新增设备请求；平台码可由首次身份探测自动识别。"""

    name: str = Field(min_length=1, max_length=128)
    platform: Platform | None = None
    kind: DeviceKind
    host: str = Field(min_length=1, max_length=128)
    port: int = Field(ge=1, le=65535)
    ssh_user: str | None = None
    ssh_port: int = Field(default=22, ge=1, le=65535)
    tags: dict[str, Any] = Field(default_factory=dict)


class DevicePatch(BaseModel):
    """设备配置的局部更新请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    platform: Platform | None = None
    kind: DeviceKind | None = None
    host: str | None = Field(default=None, min_length=1, max_length=128)
    port: int | None = Field(default=None, ge=1, le=65535)
    ssh_user: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    tags: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_non_nullable_fields(self) -> "DevicePatch":
        """阻止 PATCH 把必填配置显式置空。"""
        for field in {"name", "platform", "kind", "host", "port", "ssh_port", "tags"}:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        if not self.model_fields_set:
            raise ValueError("至少提供一个要更新的字段")
        return self


class DeviceOpsRequest(BaseModel):
    """高危运维请求；原因和当前指纹均由操作者显式确认。"""

    confirm: bool
    expected_fingerprint_uid: str = Field(pattern=r"^[0-9a-f]{16}$")
    reason: str = Field(min_length=3, max_length=500)


class DeviceCredentialWrite(BaseModel):
    """只写入设备凭据；密文字段不在任何响应中回显。"""

    kind: Literal["ssh_password", "ssh_private_key", "cloud_api_key"]
    secret: SecretStr
    passphrase: SecretStr | None = None


def _latest_deployment(db: Session, device_id: uuid.UUID) -> Deployment | None:
    return db.scalar(
        select(Deployment)
        .where(Deployment.device_id == device_id)
        .order_by(Deployment.captured_at.desc())
    )


def _device_dict(device: Device, lease: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(device.id),
        "sn": device.sn,
        "name": device.name,
        "platform": device.platform,
        "kind": device.kind,
        "host": device.host,
        "port": device.port,
        "ssh_user": device.ssh_user,
        "ssh_port": device.ssh_port,
        "status": device.status,
        "last_error": device.last_error,
        "tags": device.tags,
        "created_at": device.created_at.isoformat(),
        "version": device.version,
        "updated_by": device.updated_by,
        "updated_at": device.updated_at.isoformat() if device.updated_at else None,
        "lease": lease,
    }


def _device_configuration(device: Device) -> dict[str, Any]:
    """仅收录可编辑配置，探测状态变化不使配置 ETag 失效。"""
    return {
        "name": device.name,
        "platform": device.platform,
        "kind": device.kind,
        "host": device.host,
        "port": device.port,
        "ssh_user": device.ssh_user,
        "ssh_port": device.ssh_port,
        "tags": device.tags or {},
    }


def _device_etag(device: Device, lease: dict[str, Any] | None = None) -> str:
    representation = {
        **_device_configuration(device),
        "status": device.status,
        "last_error": device.last_error,
        "lease": lease,
    }
    return make_etag("d", device.version, representation)


def _active_device_leases(
    db: Session, device_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量查出有效租约，避免设备列表逐行查询。"""
    if not device_ids:
        return {}
    rows = db.execute(
        select(DeviceLease, RunTarget, Run)
        .join(RunTarget, RunTarget.id == DeviceLease.run_target_id)
        .join(Run, Run.id == RunTarget.run_id)
        .where(
            DeviceLease.device_id.in_(device_ids),
            DeviceLease.expires_at > datetime.now(UTC),
        )
    ).all()
    return {
        lease.device_id: {
            "holder_run_id": str(run.id),
            "holder_run_name": run.name,
            "holder_user": run.created_by,
            "acquired_at": lease.acquired_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
        }
        for lease, _target, run in rows
    }


def _active_device_lease(db: Session, device_id: uuid.UUID) -> dict[str, Any] | None:
    return _active_device_leases(db, [device_id]).get(device_id)


def _deployment_dict(deployment: Deployment) -> dict[str, Any]:
    return {
        "id": str(deployment.id),
        "uid": deployment.uid,
        "platform": deployment.platform,
        "bigversion": deployment.bigversion,
        "etsllm_version": deployment.etsllm_version,
        "model_key": deployment.model_key,
        "model_version": deployment.model_version,
        "accel": deployment.accel,
        "quant": deployment.quant,
        "soc": deployment.soc,
        "capabilities": deployment.capabilities,
        "limits": deployment.limits,
        "probe_meta": deployment.probe_meta,
        "captured_at": deployment.captured_at.isoformat(),
    }


def _get_or_404(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    return device


@router.get("")
def list_devices(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """设备列表；按名称排序。"""
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    leases = _active_device_leases(db, [device.id for device in devices])
    items = []
    for device in devices:
        payload = _device_dict(device, leases.get(device.id))
        deployment = _latest_deployment(db, device.id)
        payload["latest_deployment"] = _deployment_dict(deployment) if deployment else None
        items.append(payload)
    return {"items": items}


@router.post("", status_code=201)
def create_device(
    body: DeviceCreate,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
) -> dict[str, Any]:
    """登记设备；省略平台码时暂存为 unknown，随后由身份探测识别。"""
    device_values = body.model_dump(exclude={"platform"})
    device = Device(
        **device_values,
        platform=body.platform or "unknown",
        updated_by=str(claims.get("sub", "")),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    response.headers["ETag"] = _device_etag(device)
    return _device_dict(device)


@router.get(
    "/{device_id}",
    response_model=dict[str, Any],
    responses={304: {"description": "配置未变化"}},
)
def get_device(
    device_id: uuid.UUID,
    response: Response,
    if_none_match: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any] | Response:
    """设备详情。"""
    device = _get_or_404(db, device_id)
    lease = _active_device_lease(db, device.id)
    etag = _device_etag(device, lease)
    response.headers["ETag"] = etag
    if if_none_match_matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    payload = _device_dict(device, lease)
    latest = _latest_deployment(db, device.id)
    payload["latest_deployment"] = _deployment_dict(latest) if latest else None
    return payload


@router.get("/{device_id}/credential")
def get_device_credential(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """返回设备凭据是否已配置及类型，不返回密文或明文。"""
    device = _get_or_404(db, device_id)
    kind = credential_kind_for_device(db, device)
    return {
        "configured": kind is not None,
        "kind": kind,
        "has_password": kind == "ssh_password",
        "has_ssh_key": kind == "ssh_private_key",
        "has_api_key": kind == "cloud_api_key",
    }


@router.put("/{device_id}/credential")
def put_device_credential(
    device_id: uuid.UUID,
    body: DeviceCredentialWrite,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
) -> dict[str, Any]:
    """按当前设备 ETag 原子更新加密凭据。"""
    supplied_etag = require_if_match(if_match)
    device = db.scalar(select(Device).where(Device.id == device_id).with_for_update())
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    lease = _active_device_lease(db, device.id)
    current_etag = _device_etag(device, lease)
    check_if_match(
        supplied_etag,
        current_etag,
        current=_device_dict(device, lease),
        attempted={"credential": body.kind},
    )
    _ensure_device_idle_for_credentials(db, device, lease)

    if device.kind == "cloud":
        if body.kind != "cloud_api_key":
            raise ApiError("E_SEMANTIC", "云端设备只接受 API Key", status_code=422)
    elif device.kind in {"etsstt", "gateway"}:
        if body.kind not in {"ssh_password", "ssh_private_key"}:
            raise ApiError("E_SEMANTIC", "本地设备只接受 SSH 密码或私钥", status_code=422)
    else:
        raise ApiError("E_UNSUPPORTED", "不支持的设备类型", status_code=422)

    secret = body.secret.get_secret_value()
    passphrase = body.passphrase.get_secret_value() if body.passphrase else None
    if passphrase is not None and body.kind != "ssh_private_key":
        raise ApiError("E_SEMANTIC", "只有 SSH 私钥凭据可以设置口令", status_code=422)
    if not secret or len(secret) > 65_536 or (passphrase is not None and len(passphrase) > 4096):
        raise ApiError("E_SEMANTIC", "凭据内容长度无效", status_code=422)
    save_device_credential(
        db,
        device,
        kind=body.kind,
        secret=secret,
        passphrase=passphrase,
    )
    device.updated_by = str(claims.get("sub", ""))
    device.updated_at = datetime.now(UTC)
    audit.record(
        db,
        "device.credential.updated",
        actor=device.updated_by,
        resource=f"device:{device.id}",
        detail={"kind": body.kind, "configured": True, "version": device.version},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(device)
    response.headers["ETag"] = _device_etag(device)
    return {
        "configured": True,
        "kind": body.kind,
        "has_password": body.kind == "ssh_password",
        "has_ssh_key": body.kind == "ssh_private_key",
        "has_api_key": body.kind == "cloud_api_key",
    }


@router.delete("/{device_id}/credential", status_code=204)
def delete_device_credential(
    device_id: uuid.UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
) -> Response:
    """删除凭据密文并提升设备版本，使旧 ETag 失效。"""
    supplied_etag = require_if_match(if_match)
    device = db.scalar(select(Device).where(Device.id == device_id).with_for_update())
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    lease = _active_device_lease(db, device.id)
    current_etag = _device_etag(device, lease)
    check_if_match(
        supplied_etag,
        current_etag,
        current=_device_dict(device, lease),
        attempted={"credential": None},
    )
    _ensure_device_idle_for_credentials(db, device, lease)
    kind = credential_kind_for_device(db, device)
    remove_device_credential(db, device)
    device.updated_by = str(claims.get("sub", ""))
    device.updated_at = datetime.now(UTC)
    audit.record(
        db,
        "device.credential.deleted",
        actor=device.updated_by,
        resource=f"device:{device.id}",
        detail={"kind": kind, "configured": False, "version": device.version},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    response.headers["ETag"] = _device_etag(device)
    return Response(status_code=204, headers={"ETag": response.headers["ETag"]})


def _ensure_device_idle_for_credentials(
    db: Session, device: Device, lease: dict[str, Any] | None
) -> None:
    """凭据变更期间禁止并发 Run、探测、自检与设备运维。"""
    active_job = db.scalar(
        select(Job.id).where(
            Job.ref_type == "device",
            Job.ref_id == str(device.id),
            Job.status.in_(("queued", "running")),
        )
    )
    active_op = db.scalar(
        select(OpsAction.id).where(
            OpsAction.device_id == device.id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if lease is not None or active_job is not None or active_op is not None:
        raise ApiError("E_DEVICE_BUSY", "设备运行期间不能修改凭据", status_code=409)


@deployment_router.get("/{deployment_id}")
def get_deployment(
    deployment_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """按 ID 读取不可变部署快照。"""
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise ApiError("E_NOT_FOUND", "部署快照不存在", status_code=404)
    return {"device_id": str(deployment.device_id), **_deployment_dict(deployment)}


@router.patch(
    "/{device_id}",
    responses={
        409: {"description": "设备正被 Run 占用，连接配置不可修改"},
        412: {"description": "ETag 已过期，响应 detail 携带当前对象"},
        428: {"description": "缺少 If-Match"},
    },
)
def patch_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None),
    force: bool = Query(default=False),
    reason: str | None = Query(default=None, min_length=3, max_length=500),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
) -> dict[str, Any]:
    """按 If-Match 原子更新设备配置并记录审计。"""
    supplied_etag = require_if_match(if_match)
    device = db.scalar(select(Device).where(Device.id == device_id).with_for_update())
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)

    lease = _active_device_lease(db, device.id)
    current = _device_dict(device, lease)
    current_etag = _device_etag(device, lease)
    updates = body.model_dump(exclude_unset=True)
    forced = check_if_match(
        supplied_etag,
        current_etag,
        current=current,
        attempted=updates,
        force=force,
        reason=reason,
    )

    changed_fields = [key for key, value in updates.items() if getattr(device, key) != value]
    if changed_fields:
        connection_fields = {"platform", "kind", "host", "port", "ssh_user", "ssh_port"}
        if connection_fields & set(changed_fields) and lease is not None:
            raise ApiError("E_DEVICE_BUSY", "设备运行期间不能修改连接配置", status_code=409)
        for key, value in updates.items():
            setattr(device, key, value)
        device.version += 1
        device.updated_by = str(claims.get("sub", ""))
        device.updated_at = datetime.now(UTC)
        audit.record(
            db,
            "device.config.updated",
            actor=device.updated_by,
            resource=f"device:{device.id}",
            detail={
                "changed_fields": changed_fields,
                "version": device.version,
                "force": forced,
                **({"reason": reason.strip()} if forced and reason else {}),
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(device)

    lease = _active_device_lease(db, device.id)
    response.headers["ETag"] = _device_etag(device, lease)
    return _device_dict(device, lease)


@router.delete("/{device_id}", status_code=204)
def delete_device(
    device_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:delete")),
) -> Response:
    """删除无活动任务且没有 Run 历史引用的设备，并保留审计记录。"""
    device = db.scalar(select(Device).where(Device.id == device_id).with_for_update())
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)

    lease = _active_device_lease(db, device_id)
    active_job = db.scalar(
        select(Job.id).where(
            Job.ref_type == "device",
            Job.ref_id == str(device_id),
            Job.status.in_(("queued", "running")),
        )
    )
    active_op = db.scalar(
        select(func.count())
        .select_from(OpsAction)
        .where(
            OpsAction.device_id == device_id,
            OpsAction.status.in_(("queued", "running")),
        )
    )
    if lease is not None or active_job is not None or active_op:
        raise ApiError("E_DEVICE_BUSY", "设备仍有运行中的任务或运维操作，不能删除", status_code=409)

    run_target_id = db.scalar(select(RunTarget.id).where(RunTarget.device_id == device_id).limit(1))
    if run_target_id is not None:
        raise ApiError("E_DEVICE_REFERENCED", "设备已有 Run 历史，不能删除", status_code=409)

    audit.record(
        db,
        "device.deleted",
        actor=str(claims.get("sub", "")),
        resource=f"device:{device_id}",
        detail={"name": device.name, "platform": device.platform},
        ip=request.client.host if request.client else None,
    )
    db.delete(device)
    db.commit()
    return Response(status_code=204)


@router.post("/{device_id}/probe", status_code=202)
def probe_device(
    device_id: uuid.UUID,
    response: Response,
    mode: Literal["auto", "lite", "identity", "full"] = Query(default="lite"),
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """异步执行 auto/lite/identity/full 探测，返回统一 Job 资源。"""
    device = _get_or_404(db, device_id)
    validate_probe_request(db, device, mode)

    def create() -> dict[str, Any]:
        job = start_probe(
            db,
            device,
            mode=mode,
            created_by=str(claims.get("sub", "")),
        )
        return job_dict(job)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"device_id": str(device_id), "mode": mode},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.post("/{device_id}/voiceprint-check", status_code=202)
def voiceprint_check_device(
    device_id: uuid.UUID,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """异步验证声纹暂存、提取与 matching 链路，返回统一 Job。"""
    device = _get_or_404(db, device_id)
    validate_voiceprint_check_request(db, device)

    def create() -> dict[str, Any]:
        job = start_voiceprint_check(
            db,
            device,
            created_by=str(claims.get("sub", "")),
        )
        return job_dict(job)

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"device_id": str(device_id), "mode": "voiceprint"},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("/{device_id}/deployments")
def list_deployments(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """部署快照历史，按采集时间倒序。"""
    _get_or_404(db, device_id)
    items = db.scalars(
        select(Deployment)
        .where(Deployment.device_id == device_id)
        .order_by(Deployment.captured_at.desc())
    ).all()
    return {"items": [_deployment_dict(item) for item in items]}


@router.get("/{device_id}/health")
def device_health(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """健康摘要及最近 24 小时探测记录，供时延图和失败明细使用。"""
    device = _get_or_404(db, device_id)
    since = datetime.now(UTC) - timedelta(hours=24)
    probe_filters = (ProbeRun.device_id == device.id, ProbeRun.created_at >= since)
    probe_count = int(
        db.scalar(select(func.count()).select_from(ProbeRun).where(*probe_filters)) or 0
    )
    success_count = int(
        db.scalar(
            select(func.count())
            .select_from(ProbeRun)
            .where(*probe_filters, ProbeRun.status == "ok")
        )
        or 0
    )
    probes = list(
        db.scalars(
            select(ProbeRun).where(*probe_filters).order_by(ProbeRun.created_at.desc()).limit(100)
        ).all()
    )
    probes.reverse()
    points = [
        {
            "id": str(probe.id),
            "mode": probe.mode,
            "status": probe.status,
            "duration_ms": probe.duration_ms,
            "error": probe.error,
            "created_at": probe.created_at.isoformat(),
        }
        for probe in probes
    ]
    last = db.scalar(
        select(ProbeRun)
        .where(ProbeRun.device_id == device.id)
        .order_by(ProbeRun.created_at.desc())
        .limit(1)
    )
    return {
        "state": device.status,
        "last_error": device.last_error,
        "window_start": since.isoformat(),
        "probe_count": probe_count,
        "success_count": success_count,
        "failure_count": probe_count - success_count,
        "probes": points,
        "last_probe": {
            "status": last.status,
            "at": last.created_at.isoformat(),
            "duration_ms": last.duration_ms,
        }
        if last
        else None,
    }


@router.post("/{device_id}/ops/{action}", status_code=202)
def operate_device(
    device_id: uuid.UUID,
    action: OpsName,
    body: DeviceOpsRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    claims: dict[str, Any] = Depends(require("device:ops")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """提交准备/重启 Job；服务端重新校验设备空闲与部署指纹。"""
    if not body.confirm:
        raise ApiError("E_CONFIRM_REQUIRED", "请先确认设备与运维原因", status_code=422)
    reason = body.reason.strip()
    if len(reason) < 3:
        raise ApiError("E_VALIDATION", "运维原因至少需要 3 个字符", status_code=422)

    def create() -> dict[str, Any]:
        job = start_device_operation(
            db,
            device_id,
            action=action,
            reason=reason,
            expected_fingerprint_uid=body.expected_fingerprint_uid,
            actor=str(claims.get("sub", "")),
            ip=request.client.host if request.client else None,
        )
        return {
            "id": str(job.id),
            "kind": job.kind,
            "ref": {"type": job.ref_type, "id": job.ref_id},
            "status": job.status,
            "progress": job.progress,
        }

    result = execute_idempotently(
        db,
        username=str(claims.get("sub", "")),
        key=idempotency_key,
        request_body={"device_id": str(device_id), "action": action, **body.model_dump()},
        operation=create,
        location=lambda value: f"/api/v1/jobs/{value['id']}",
        status_code=202,
    )
    apply_idempotent_response(response, result)
    return result.body


@router.get("/{device_id}/ops/history")
def device_ops_history(
    device_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("device:read")),
) -> dict[str, Any]:
    """查询设备准备/重启历史与审计摘要。"""
    _get_or_404(db, device_id)
    actions = list_device_ops(db, device_id, limit=limit)
    return {"items": [ops_action_dict(item) for item in actions]}
