"""健康检查与版本信息。

/healthz 只判断进程存活；/readyz 判断依赖是否可用。
"""

import os
import shutil
import sys
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """存活探针：只要能响应就返回 ok。"""
    return {"status": "ok"}


@router.get("/readyz")
def readyz() -> dict[str, object]:
    """就绪探针：检查数据目录、数据库、可选 NFS 和磁盘水位。"""
    settings = get_settings()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    writable = _is_writable(data_dir)
    db = _database_status()

    storage = _disk_status(data_dir)
    nfs = _nfs_status(settings.nfs_root)
    data_dir_status = "ok" if writable else "error"
    checks: dict[str, object] = {
        "data_dir": data_dir_status,
        "database": db,
        "nfs": nfs,
        "storage": storage,
    }
    status = (
        "ok"
        if data_dir_status == "ok"
        and db == "ok"
        and storage["status"] == "ok"
        and nfs["status"] in {"ok", "not_configured"}
        else "degraded"
    )
    return {"status": status, "checks": checks}


@router.get("/version")
def version() -> dict[str, str]:
    """返回版本与运行时信息，便于排障。"""
    settings = get_settings()
    return {
        "version": settings.app_version,
        "env": settings.app_env,
        "python": sys.version.split()[0],
        "schema_version": _schema_version(),
        "schema_head": _schema_head(),
    }


def _schema_version() -> str:
    """只读读取当前 Alembic 版本；数据库未就绪时返回 unknown。"""
    settings = get_settings()
    if not settings.database_url:
        return "not_configured"
    try:
        with get_engine().connect() as conn:
            version = conn.execute(text("select version_num from alembic_version limit 1")).scalar()
        return str(version) if version else "unknown"
    except Exception:
        return "unknown"


def _schema_head() -> str:
    """返回当前代码包含的 Alembic head。"""
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    try:
        return ScriptDirectory(str(migrations)).get_current_head() or "unknown"
    except Exception:
        return "unknown"


def _database_status() -> str:
    """数据库连通性；未配置或连接失败都返回可读状态。"""
    settings = get_settings()
    if not settings.database_url:
        return "not_configured"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return "ok"
    except Exception:
        return "error"


def _is_writable(path: Path) -> bool:
    """通过写一个临时文件判断目录可写。"""
    probe = path / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _disk_status(path: Path) -> dict[str, object]:
    """按 M9 §10 返回数据目录的磁盘水位，不泄露路径。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"status": "error", "free_bytes": None, "used_percent": None}
    used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else None
    status = (
        "error"
        if used_percent is not None and used_percent >= 95
        else "degraded"
        if used_percent is not None and used_percent >= 85
        else "ok"
    )
    return {"status": status, "free_bytes": usage.free, "used_percent": used_percent}


def _nfs_status(root: Path | None) -> dict[str, object]:
    """检查可选 NFS 根是否可读，不扫描内容或暴露挂载路径。"""
    if root is None:
        return {"status": "not_configured"}
    available = root.is_dir() and os.access(root, os.R_OK | os.X_OK)
    return {"status": "ok" if available else "error"}
