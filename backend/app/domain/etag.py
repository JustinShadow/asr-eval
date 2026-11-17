"""配置资源的 ETag 生成与 If-Match 检查。"""

import hashlib
import json
from typing import Any

from app.core.errors import ApiError


def make_etag(resource: str, version: int, configuration: dict[str, Any]) -> str:
    """按资源版本和规范化配置生成强 ETag。"""
    serialized = json.dumps(
        configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]
    return f'"{resource}-{version}-{digest}"'


def require_if_match(value: str | None) -> str:
    """配置写请求必须提供 If-Match。"""
    if not value:
        raise ApiError(
            "E_PRECONDITION_REQUIRED",
            "配置更新必须提供 If-Match",
            status_code=428,
        )
    normalized = value.strip()
    if not normalized:
        raise ApiError(
            "E_PRECONDITION_REQUIRED",
            "配置更新必须提供 If-Match",
            status_code=428,
        )
    return normalized


def check_if_match(
    supplied: str,
    current_etag: str,
    *,
    current: dict[str, Any],
    attempted: dict[str, Any],
    force: bool = False,
    reason: str | None = None,
) -> bool:
    """发现旧版本时回传当前对象及字段差异；强制覆盖时返回是否绕过旧 ETag。"""
    if force and not (reason and reason.strip()):
        raise ApiError(
            "E_VALIDATION",
            "强制覆盖必须提供原因",
            status_code=422,
        )
    if not force and reason is not None:
        raise ApiError(
            "E_VALIDATION",
            "reason 只能与 force=true 一起使用",
            status_code=422,
        )
    if current_etag in {candidate.strip() for candidate in supplied.split(",")}:
        return False
    if force:
        return True
    changed_fields = sorted(
        field for field, value in attempted.items() if current.get(field) != value
    )
    raise ApiError(
        "E_PRECONDITION_FAILED",
        "配置已被其他用户修改，请先查看当前版本",
        status_code=412,
        detail={
            "current": current,
            "current_etag": current_etag,
            "changed_fields": changed_fields,
            "updated_by": current.get("updated_by"),
            "updated_at": current.get("updated_at"),
        },
    )


def if_none_match_matches(value: str | None, etag: str) -> bool:
    """检查 GET 条件请求，支持 ETag 列表及通配符。"""
    if not value:
        return False
    return any(candidate.strip() in {etag, "*", f"W/{etag}"} for candidate in value.split(","))
