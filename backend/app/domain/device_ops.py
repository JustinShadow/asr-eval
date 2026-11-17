"""受控执行设备准备与重启，并将状态写入统一 Job 和运维审计。"""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.etsstt import IdentityOutcome, probe_identity
from app.adapters.ssh_tunnel import device_endpoint
from app.core.errors import ApiError
from app.domain import audit
from app.domain.credentials import (
    CredentialUnavailable,
    DeviceCredentialValue,
    credential_for_device,
)
from app.domain.models import (
    Deployment,
    Device,
    DeviceLease,
    Job,
    OpsAction,
    Run,
    RunTarget,
)

OpsName = Literal["restart"]
_workers: dict[uuid.UUID, threading.Thread] = {}
_logger = logging.getLogger(__name__)


class DeviceOpsError(RuntimeError):
    """设备操作失败，包含可写入 Job 与审计记录的安全摘要。"""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def start_device_operation(
    db: Session,
    device_id: uuid.UUID,
    *,
    action: OpsName,
    reason: str,
    expected_fingerprint_uid: str,
    actor: str,
    ip: str | None,
) -> Job:
    """在设备行锁内检查租约、运行目标与指纹，并原子登记 Job/审计。"""
    device = db.scalar(
        select(Device)
        .where(Device.id == device_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if device is None:
        raise ApiError("E_NOT_FOUND", "设备不存在", status_code=404)
    if device.kind not in {"etsstt", "gateway"}:
        raise ApiError("E_UNSUPPORTED", "云端设备不支持本地运维操作", status_code=422)
    try:
        credential = credential_for_device(db, device)
    except CredentialUnavailable as exc:
        raise ApiError("E_CREDENTIAL_UNAVAILABLE", str(exc), status_code=503) from exc
    if credential is not None and credential.kind not in {"ssh_password", "ssh_private_key"}:
        raise ApiError("E_CREDENTIAL_UNAVAILABLE", "当前设备保存的凭据类型不匹配", status_code=503)

    deployment = db.scalar(
        select(Deployment)
        .where(Deployment.device_id == device.id)
        .order_by(Deployment.captured_at.desc())
    )
    if deployment is None:
        raise ApiError(
            "E_FINGERPRINT_UNAVAILABLE", "设备尚无部署指纹，无法安全执行运维", status_code=409
        )
    if deployment.uid != expected_fingerprint_uid:
        raise ApiError("E_FINGERPRINT_CHANGED", "设备指纹已变化，请刷新后重新确认", status_code=409)

    lease = db.scalar(
        select(DeviceLease.device_id).where(
            DeviceLease.device_id == device.id,
            DeviceLease.expires_at > datetime.now(UTC),
        )
    )
    running_target = db.scalar(
        select(RunTarget.id)
        .join(Run, Run.id == RunTarget.run_id)
        .where(
            RunTarget.device_id == device.id,
            RunTarget.status == "running",
            Run.status.in_(("queued", "running", "cancelling")),
        )
    )
    if lease is not None or running_target is not None:
        raise ApiError("E_DEVICE_BUSY", "设备正在执行 Run，无法进行运维操作", status_code=409)

    active_job = db.scalar(
        select(Job.id).where(
            Job.kind.in_(("probe", "voiceprint_check", "device_ops")),
            Job.ref_type == "device",
            Job.ref_id == str(device.id),
            Job.status.in_(("queued", "running")),
        )
    )
    if active_job is not None:
        raise ApiError("E_DEVICE_BUSY", "设备已有探测、自检或运维任务正在执行", status_code=409)

    job_id = uuid.uuid4()
    action_id = uuid.uuid4()
    job = Job(
        id=job_id,
        kind="device_ops",
        ref_type="device",
        ref_id=str(device.id),
        status="queued",
        progress={"phase": "queued", "action": action},
        created_by=actor,
    )
    ops_action = OpsAction(
        id=action_id,
        device_id=device.id,
        action=action,
        actor=actor,
        status="queued",
        detail={
            "job_id": str(job.id),
            "reason": reason,
            "expected_fingerprint_uid": expected_fingerprint_uid,
            "platform_code": device.platform,
        },
    )
    db.add_all([job, ops_action])
    audit.record(
        db,
        f"device.ops.{action}",
        actor=actor,
        resource=f"device:{device.id}",
        detail={
            "ops_action_id": str(ops_action.id),
            "job_id": str(job.id),
            "reason": reason,
            "fingerprint_uid": expected_fingerprint_uid,
            "status": "queued",
        },
        ip=ip,
    )
    db.commit()
    db.refresh(job)

    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_device_operation,
        args=(factory, job_id, action_id, device.id, action, expected_fingerprint_uid),
        daemon=True,
        name=f"device-ops-{job_id}",
    )
    _workers[job_id] = thread
    thread.start()
    return job


def _run_device_operation(
    factory: sessionmaker[Session],
    job_id: uuid.UUID,
    action_id: uuid.UUID,
    device_id: uuid.UUID,
    action: OpsName,
    expected_uid: str,
) -> None:
    """后台执行单个动作并保存 Job、设备健康与审计终态。"""
    db = factory()
    result: dict[str, Any] | None = None
    try:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        ops_action = db.scalar(select(OpsAction).where(OpsAction.id == action_id).with_for_update())
        device = db.get(Device, device_id)
        if job is None or ops_action is None or device is None:
            return
        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.progress = {"phase": "connecting", "action": action}
        ops_action.status = "running"
        db.commit()

        current_uid = db.scalar(
            select(Deployment.uid)
            .where(Deployment.device_id == device.id)
            .order_by(Deployment.captured_at.desc())
        )
        if current_uid != expected_uid:
            raise DeviceOpsError("E_FINGERPRINT_CHANGED", "后台执行前设备指纹已变化")
        credential = credential_for_device(db, device)
        result = _perform_operation(device, action, credential)
        health = _wait_until_healthy(device, credential)
        result["health"] = health
        device.status = "online" if health == "online" else "degraded"
        device.last_error = None if health == "online" else "版本采集未完整，但设备接口已恢复"
        ops_action.status = "succeeded"
        ops_action.detail = {**(ops_action.detail or {}), **result}
        job.status = "succeeded"
        job.progress = {"phase": "done", "action": action}
        job.result = {"ops_action_id": str(ops_action.id), **result}
        job.finished_at = datetime.now(UTC)
        audit.record(
            db,
            "device.ops.completed",
            actor=ops_action.actor,
            resource=f"device:{device.id}",
            detail={"ops_action_id": str(ops_action.id), "action": action, **result},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        ops_action = db.get(OpsAction, action_id)
        device = db.get(Device, device_id)
        code = exc.code if isinstance(exc, DeviceOpsError) else "E_DEVICE_OPS_FAILED"
        message = str(exc)[:1000]
        detail = exc.detail if isinstance(exc, DeviceOpsError) else {}
        if job is not None:
            job.status = "failed"
            job.error_code = code
            job.error_detail = {"message": message}
            job.progress = {**(job.progress or {}), "phase": "failed"}
            job.finished_at = datetime.now(UTC)
        if ops_action is not None:
            ops_action.status = "failed"
            ops_action.detail = {
                **(ops_action.detail or {}),
                **detail,
                "error_code": code,
                "error": message,
            }
        if device is not None and code in {
            "E_DEVICE_OPS_FAILED",
            "E_DEVICE_OPS_TIMEOUT",
            "E_OPS_HEALTHCHECK_FAILED",
        }:
            device.status = "needs_restart"
            device.last_error = message[:1024]
        if ops_action is not None:
            audit.record(
                db,
                "device.ops.failed",
                actor=ops_action.actor,
                resource=f"device:{device_id}",
                detail={
                    "ops_action_id": str(action_id),
                    "action": action,
                    "error_code": code,
                    **detail,
                },
            )
        db.commit()
        _logger.warning("设备运维任务失败 job_id=%s code=%s", job_id, code)
    finally:
        db.close()
        _workers.pop(job_id, None)


def _perform_operation(
    device: Device, action: OpsName, credential: DeviceCredentialValue | None = None
) -> dict[str, Any]:
    """按设备类型选择固定命令；所有外部结果只保留短摘要。"""
    if device.kind == "gateway":
        kubectl = shutil.which("kubectl")
        if kubectl is None:
            raise DeviceOpsError("E_OPS_TOOL_MISSING", "运行环境未安装 kubectl")
        command = [kubectl, "rollout", "restart", "deployment/etsllm"]
        result = _run_process(command, timeout_s=45)
        _require_success(result, command)
        return _command_detail(command, result)

    target = _ssh_target(device)
    script = _restart_script()
    command = [
        "ssh",
        "-p",
        str(device.ssh_port),
        "-oBatchMode=yes",
        "-oConnectTimeout=10",
        "-oStrictHostKeyChecking=yes",
        target,
        "python3",
        "-",
    ]
    input_bytes = script.encode("utf-8")
    if credential is None:
        result = _run_process(command, timeout_s=60, input_bytes=input_bytes)
    else:
        result = asyncio.run(
            _run_authenticated_ssh_process(device, command, input_bytes, credential)
        )
    payload = _remote_payload(result, command)
    if not payload.get("ok"):
        raise DeviceOpsError(
            str(payload.get("error_code") or "E_DEVICE_OPS_FAILED"),
            str(payload.get("error") or "设备命令执行失败"),
            _safe_remote_detail(payload),
        )
    return _safe_remote_detail(payload)


def _wait_until_healthy(device: Device, credential: DeviceCredentialValue | None = None) -> str:
    """准备/重启后最多等待 30 秒，确认身份接口恢复。"""
    deadline = time.monotonic() + 30
    last_error = "设备身份检查未成功"
    while time.monotonic() <= deadline:
        try:
            asr_host, asr_port = device_endpoint(device)
            outcome: IdentityOutcome = asyncio.run(
                probe_identity(
                    asr_host,
                    asr_port,
                    ssh_host=device.host,
                    system_info_host=device.host,
                    kind=device.kind,
                    platform=device.platform,
                    ssh_user=device.ssh_user,
                    ssh_port=device.ssh_port,
                    ssh_password=_ssh_credential_kwargs(credential).get("ssh_password"),
                    ssh_private_key=_ssh_credential_kwargs(credential).get("ssh_private_key"),
                    ssh_passphrase=_ssh_credential_kwargs(credential).get("ssh_passphrase"),
                )
            )
            if outcome.ok and outcome.version_collection_ok:
                return "online"
            if outcome.ok:
                last_error = "设备接口已恢复，但版本采集仍不完整"
                time.sleep(3)
                continue
            last_error = outcome.error or last_error
        except Exception as exc:  # 网络恢复窗口内允许继续重试。
            last_error = str(exc)[:500]
        time.sleep(3)
    raise DeviceOpsError(
        "E_OPS_HEALTHCHECK_FAILED",
        f"运维命令已执行，但设备在 30 秒内未恢复健康：{last_error}",
        {"healthcheck_error": last_error},
    )


def _ssh_credential_kwargs(credential: DeviceCredentialValue | None) -> dict[str, str]:
    """转换凭据类型为适配器参数，调用方不得记录返回值。"""
    if credential is None:
        return {}
    if credential.kind == "ssh_password":
        return {"ssh_password": credential.secret}
    if credential.kind == "ssh_private_key":
        kwargs = {"ssh_private_key": credential.secret}
        if credential.passphrase is not None:
            kwargs["ssh_passphrase"] = credential.passphrase
        return kwargs
    raise DeviceOpsError("E_CREDENTIAL_UNAVAILABLE", "当前设备保存的凭据类型不匹配")


async def _run_authenticated_ssh_process(
    device: Device,
    command: list[str],
    input_bytes: bytes,
    credential: DeviceCredentialValue,
) -> subprocess.CompletedProcess[bytes]:
    """通过 AsyncSSH 把设备脚本送入 stdin，避免明文凭据出现在进程参数或临时文件。"""
    import asyncssh

    target = _ssh_target(device)
    username, host = target.split("@", maxsplit=1)
    host = host.strip("[]")
    try:
        options: dict[str, Any] = {
            "port": device.ssh_port,
            "username": username,
            "connect_timeout": 10,
        }
        if credential.kind == "ssh_password":
            options.update(password=credential.secret, client_keys=[])
        elif credential.kind == "ssh_private_key":
            options.update(
                client_keys=[
                    asyncssh.import_private_key(credential.secret, passphrase=credential.passphrase)
                ],
                password_auth=False,
            )
        else:
            raise DeviceOpsError("E_CREDENTIAL_UNAVAILABLE", "当前设备保存的凭据类型不匹配")
        async with asyncssh.connect(host, **options) as connection:
            result = await connection.run(
                "python3 -", input=input_bytes, timeout=60, check=False, encoding=None
            )
    except DeviceOpsError:
        raise
    except Exception:
        raise DeviceOpsError("E_DEVICE_OPS_FAILED", "SSH 连接或命令执行失败") from None
    stdout = result.stdout or b""
    stderr = result.stderr or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    if isinstance(stderr, str):
        stderr = stderr.encode()
    return subprocess.CompletedProcess(
        args=command,
        returncode=result.exit_status if result.exit_status is not None else 255,
        stdout=stdout,
        stderr=stderr,
    )


def _ssh_target(device: Device) -> str:
    """校验 SSH 目标，避免设备字段被解释为选项或命令片段。"""
    if not device.ssh_user or not re.fullmatch(r"[A-Za-z0-9._-]+", device.ssh_user):
        raise DeviceOpsError("E_CREDENTIAL_UNAVAILABLE", "设备未配置有效的 SSH 用户")
    host = device.host.strip("[]")
    if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._:-]+", host):
        raise DeviceOpsError("E_DEVICE_CONFIG_INVALID", "设备 SSH 地址格式无效")
    return f"{device.ssh_user}@{host}"


def _run_process(
    command: list[str], *, timeout_s: int, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """以参数数组执行本地命令，不经过 shell。"""
    try:
        return subprocess.run(  # noqa: S603 - 命令固定为系统 SSH/kubectl，不经过 shell。
            command,
            input=input_bytes,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeviceOpsError("E_DEVICE_OPS_TIMEOUT", "设备操作命令超时") from exc
    except OSError as exc:
        raise DeviceOpsError("E_DEVICE_OPS_FAILED", f"无法启动设备操作命令：{exc}") from exc


def _require_success(result: subprocess.CompletedProcess[bytes], command: list[str]) -> None:
    if result.returncode != 0:
        raise DeviceOpsError(
            "E_DEVICE_OPS_FAILED",
            "设备操作命令返回失败",
            _command_detail(command, result),
        )


def _command_detail(
    command: list[str], result: subprocess.CompletedProcess[bytes]
) -> dict[str, Any]:
    return {
        "command": " ".join(command[:5]),
        "return_code": result.returncode,
        "stdout_summary": result.stdout.decode("utf-8", errors="replace")[-1500:],
        "stderr_summary": result.stderr.decode("utf-8", errors="replace")[-1500:],
    }


def _remote_payload(
    result: subprocess.CompletedProcess[bytes], command: list[str]
) -> dict[str, Any]:
    stdout = result.stdout.decode("utf-8", errors="replace")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DeviceOpsError(
            "E_DEVICE_OPS_FAILED",
            "设备没有返回可验证的操作结果",
            _command_detail(command, result),
        ) from exc
    if not isinstance(payload, dict):
        raise DeviceOpsError(
            "E_DEVICE_OPS_FAILED",
            "设备返回了无法识别的操作结果",
            _command_detail(command, result),
        )
    if result.returncode != 0 and payload.get("ok"):
        payload["ok"] = False
        payload.setdefault("error", "远程脚本退出码非零")
    return payload


def _safe_remote_detail(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "backup_path",
        "platform_code",
        "command",
        "return_code",
        "stdout_summary",
        "stderr_summary",
        "rolled_back",
    }
    return {
        key: str(value)[-1500:] if isinstance(value, str) else value
        for key, value in payload.items()
        if key in allowed
    }


def _restart_script() -> str:
    """生成仅调用已知服务管理器的远端脚本。"""
    return """import json, shutil, subprocess, sys
commands = []
if shutil.which("systemctl"):
    commands.append(["systemctl", "restart", "etsllm"])
if shutil.which("supervisorctl"):
    commands.append(["supervisorctl", "restart", "etsllm"])
if not commands:
    print(json.dumps({
        "ok": False,
        "error_code": "E_OPS_TOOL_MISSING",
        "error": "未找到 systemctl 或 supervisorctl",
    }))
    sys.exit(2)
for command in commands[:1]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    print(json.dumps({
        "ok": result.returncode == 0,
        "command": " ".join(command),
        "return_code": result.returncode,
        "stdout_summary": result.stdout[-1500:],
        "stderr_summary": result.stderr[-1500:],
        "error": "服务重启命令失败" if result.returncode else None,
    }))
    sys.exit(result.returncode)
"""


def list_device_ops(db: Session, device_id: uuid.UUID, *, limit: int = 50) -> list[OpsAction]:
    """按时间倒序读取设备运维历史。"""
    return list(
        db.scalars(
            select(OpsAction)
            .where(OpsAction.device_id == device_id)
            .order_by(OpsAction.created_at.desc(), OpsAction.id.desc())
            .limit(limit)
        ).all()
    )


def ops_action_dict(action: OpsAction) -> dict[str, Any]:
    """序列化运维记录，不包含设备凭据。"""
    detail = dict(action.detail or {})
    return {
        "id": str(action.id),
        "device_id": str(action.device_id),
        "action": action.action,
        "actor": action.actor,
        "status": action.status,
        "detail": detail,
        "created_at": action.created_at.isoformat(),
    }


def wait_for_device_ops_tasks(timeout_s: float = 35.0) -> None:
    """测试辅助：等待当前设备运维后台任务结束。"""
    for thread in list(_workers.values()):
        thread.join(timeout=timeout_s)
