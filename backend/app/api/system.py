"""系统管理员只读状态接口（M8 §9.13、M9 §10）。"""

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import require
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.domain.models import Device, Evaluation, Job, Run

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _storage_status(settings: Settings) -> dict[str, Any]:
    """探测对象目录可写性和磁盘余量，不返回本机路径。"""
    object_dir = settings.data_dir / "objects"
    try:
        object_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".status-", dir=object_dir):
            pass
        usage = shutil.disk_usage(object_dir)
    except OSError:
        return {"status": "error", "free_bytes": None, "used_percent": None}

    used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else None
    # M9 §10：85% 预警、95% 阻断阈值；这里只读展示，不阻断业务请求。
    status = (
        "error"
        if used_percent is not None and used_percent >= 95
        else "degraded"
        if used_percent is not None and used_percent >= 85
        else "ok"
    )
    return {"status": status, "free_bytes": usage.free, "used_percent": used_percent}


def _nfs_status(settings: Settings) -> dict[str, Any]:
    """检查配置的只读数据根目录是否存在且可读，不泄露真实挂载路径。"""
    roots = {str(settings.nfs_root)} if settings.nfs_root is not None else set()
    if not roots:
        return {"status": "not_configured", "configured_roots": 0, "available_roots": 0}
    available = sum(
        1 for root in roots if Path(root).is_dir() and os.access(root, os.R_OK | os.X_OK)
    )
    status = "ok" if available == len(roots) else "degraded" if available else "error"
    return {"status": status, "configured_roots": len(roots), "available_roots": available}


def _count_statuses(db: Session, model: type[Any], statuses: tuple[str, ...]) -> int:
    """按一组状态统计队列条目。"""
    return int(
        db.scalar(select(func.count()).select_from(model).where(model.status.in_(statuses))) or 0
    )


@router.get("/status")
def get_system_status(
    db: Session = Depends(get_db),
    _: dict[str, Any] = Depends(require("admin:system")),
) -> dict[str, Any]:
    """汇总数据库、存储、NFS、设备、版本和任务队列状态。"""
    settings = get_settings()
    database_status = "ok"
    schema_version = "unknown"
    devices: dict[str, Any] = {"status": "unknown", "online": None, "total": None, "by_status": {}}
    queues: dict[str, Any] = {
        "jobs": {"queued": None, "running": None},
        "runs": {"queued": None, "running": None},
        "evaluations": {"queued": None, "running": None},
    }
    try:
        db.execute(text("select 1"))
        has_schema_table = db.scalar(text("select to_regclass('alembic_version')"))
        if has_schema_table:
            schema = db.scalar(text("select version_num from alembic_version limit 1"))
            if isinstance(schema, str):
                schema_version = schema
        device_rows = db.execute(select(Device.status, func.count()).group_by(Device.status)).all()
        by_status = {str(status): int(count) for status, count in device_rows}
        device_total = sum(by_status.values())
        online = by_status.get("online", 0)
        devices = {
            "status": "not_configured"
            if device_total == 0
            else "ok"
            if online == device_total
            else "degraded"
            if online
            else "error",
            "online": online,
            "total": device_total,
            "by_status": by_status,
        }
        queues = {
            "jobs": {
                "queued": _count_statuses(db, Job, ("queued",)),
                "running": _count_statuses(db, Job, ("running",)),
            },
            "runs": {
                "queued": _count_statuses(db, Run, ("queued",)),
                "running": _count_statuses(db, Run, ("running", "cancelling")),
            },
            "evaluations": {
                "queued": _count_statuses(db, Evaluation, ("queued",)),
                "running": _count_statuses(db, Evaluation, ("scoring", "cancelling")),
            },
        }
    except Exception:
        database_status = "error"

    storage = _storage_status(settings)
    nfs = _nfs_status(settings)
    dependencies = {
        "database": {"status": database_status},
        "local_storage": storage,
        "nfs": nfs,
        "devices": devices,
    }
    unhealthy = any(
        dependency["status"] in {"error", "degraded"} for dependency in dependencies.values()
    )
    return {
        "status": "degraded" if unhealthy else "ok",
        "dependencies": dependencies,
        "version": {"app": settings.app_version, "schema": schema_version},
        "queues": queues,
        "as_of": datetime.now(UTC).isoformat(),
    }
