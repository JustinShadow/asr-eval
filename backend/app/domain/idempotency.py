"""创建类请求的 PostgreSQL 幂等保护（M7 §4.5）。"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.models import IdempotencyKey

IDEMPOTENCY_TTL = timedelta(hours=24)


def request_hash(request_body: dict[str, Any]) -> str:
    """按 JSON 键排序计算请求摘要，忽略对象键的输入顺序。"""
    canonical = json.dumps(
        request_body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotentResult:
    """接口响应及是否来自幂等缓存。"""

    body: dict[str, Any]
    status_code: int
    headers: dict[str, str]
    replayed: bool


def execute_idempotently(
    db: Session,
    *,
    username: str,
    key: str | None,
    request_body: dict[str, Any],
    operation: Callable[[], dict[str, Any]],
    location: Callable[[dict[str, Any]], str],
    status_code: int = 201,
) -> IdempotentResult:
    """串行化相同用户与 key 的请求并缓存原始成功响应。"""
    if key is None:
        body = operation()
        return IdempotentResult(
            body=body,
            status_code=status_code,
            headers={"Location": location(body)},
            replayed=False,
        )
    if not key.strip() or len(key) > 128:
        raise ApiError("E_VALIDATION", "Idempotency-Key 长度必须为 1～128 字符", status_code=400)

    body_hash = request_hash(request_body)
    lock_digest = hashlib.sha256(f"{username}\x00{key}".encode()).digest()[:8]
    lock_id = int.from_bytes(lock_digest, byteorder="big", signed=True)

    # session 级 advisory lock 跨越领域服务内部 commit，保证并发重试不会重复创建。
    bind = db.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    with engine.connect() as lock_connection:
        lock_connection.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        lock_connection.commit()
        try:
            existing = db.get(IdempotencyKey, {"username": username, "key": key})
            now = datetime.now(UTC)
            if existing is not None and existing.created_at > now - IDEMPOTENCY_TTL:
                if existing.request_hash != body_hash:
                    raise ApiError(
                        "E_IDEMPOTENCY_CONFLICT",
                        "相同 Idempotency-Key 不能用于不同请求内容",
                        status_code=409,
                    )
                return IdempotentResult(
                    body=dict(existing.response),
                    status_code=existing.status_code,
                    headers=dict(existing.headers or {}),
                    replayed=True,
                )
            if existing is not None:
                db.delete(existing)
                db.commit()

            body = operation()
            response_headers = {"Location": location(body)}
            db.add(
                IdempotencyKey(
                    username=username,
                    key=key,
                    request_hash=body_hash,
                    response=body,
                    status_code=status_code,
                    headers=response_headers,
                )
            )
            db.commit()
            return IdempotentResult(
                body=body,
                status_code=status_code,
                headers=response_headers,
                replayed=False,
            )
        finally:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
            )
            lock_connection.commit()
