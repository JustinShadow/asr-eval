"""用户级资源变更信号、事务通知与进程内 SSE 缓冲。"""

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

import psycopg
from sqlalchemy import event, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.sse import SSEEvent, SSEEventBuffer
from app.domain.models import (
    AppUser,
    Benchmark,
    Device,
    Evaluation,
    GatePolicy,
    Job,
    MetricProfile,
    Report,
    Run,
    UserGrant,
)

logger = logging.getLogger(__name__)


class UserEventBus:
    """保存当前进程的用户级事件，并抑制同一资源的短时重复信号。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.buffer = SSEEventBuffer(max_events=500, retention_seconds=600, clock=clock)
        self._clock = clock
        self._recent: dict[tuple[str, str], float] = {}
        self._lock = Lock()

    def publish(self, payload: dict[str, Any]) -> SSEEvent | None:
        """按资源在 500ms 窗口内合并连续失效通知。"""
        resource_type = str(payload.get("type", ""))
        resource_id = str(payload.get("id", ""))
        if not resource_type or not resource_id:
            return None
        now = self._clock()
        key = (resource_type, resource_id)
        with self._lock:
            last = self._recent.get(key)
            if last is not None and now - last < 0.5:
                return None
            self._recent[key] = now
            for old_key, sent_at in list(self._recent.items()):
                if now - sent_at > 600:
                    del self._recent[old_key]
        return self.buffer.publish("user", "resource.changed", payload)

    def publish_resync(self, reason: str) -> SSEEvent:
        """通知连接中的客户端整体重新校验活跃查询。"""
        return self.buffer.publish("user", "resync", {"reason": reason})


user_event_bus = UserEventBus()


class UserEventListener:
    """独立 PostgreSQL LISTEN 连接，将提交后的通知转发到进程 SSE 缓冲。"""

    def __init__(self, engine: Engine) -> None:
        connection_url = engine.url.render_as_string(hide_password=False)
        self._connection_url = connection_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._stop = Event()
        self._ready = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        """启动后台监听线程；重复启动保持幂等。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._listen, name="asr-user-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止监听并在退出前释放 PostgreSQL 连接。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._ready.clear()

    def wait_until_ready(self, timeout: float = 5) -> bool:
        """等待 LISTEN 成功，主要供启动探测和集成测试使用。"""
        return self._ready.wait(timeout)

    def _listen(self) -> None:
        connected_once = False
        while not self._stop.is_set():
            try:
                with psycopg.connect(self._connection_url, autocommit=True) as connection:
                    connection.execute("LISTEN asr_events")
                    self._ready.set()
                    if connected_once:
                        user_event_bus.publish_resync("listener_reconnected")
                    connected_once = True
                    while not self._stop.is_set():
                        for notification in connection.notifies(timeout=0.5, stop_after=100):
                            self._consume_notification(notification.payload)
            except Exception as exc:
                self._ready.clear()
                if not self._stop.is_set():
                    logger.warning("PostgreSQL 用户事件监听断开，将重连：%s", exc)
                    self._stop.wait(1)

    @staticmethod
    def _consume_notification(payload: str) -> None:
        """验证轻量通知 JSON 后放入本进程缓冲。"""
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            logger.warning("忽略无法解析的 asr_events 通知")
            return
        if not isinstance(data, dict):
            return
        user_event_bus.publish(data)


def _event_data(resource: object, resource_type: str) -> dict[str, Any] | None:
    """从领域实体提取不含业务正文的失效信号。"""
    actor: str | None = None
    label: str | None = None
    version = 1
    if isinstance(resource, (Run, Evaluation, Job, Device, Benchmark, MetricProfile, GatePolicy)):
        resource_id = getattr(resource, "id", None)
        version = int(getattr(resource, "version", 1) or 1)
        actor = getattr(resource, "updated_by", None) or getattr(resource, "created_by", None)
        label = getattr(resource, "name", None)
        if isinstance(resource, Evaluation):
            label = "评分任务"
        elif isinstance(resource, Job):
            label = resource.kind
        elif isinstance(resource, GatePolicy):
            label = resource.name
    elif isinstance(resource, Report):
        resource_id = resource.id
        actor = resource.created_by
        label = resource.kind
    elif isinstance(resource, AppUser):
        resource_id = resource.username
        actor = resource.username
        label = "用户权限"
    elif isinstance(resource, UserGrant):
        resource_id = resource.username
        actor = resource.granted_by
        label = "用户授权"
    else:
        return None

    if resource_type == "permissions":
        version = int(datetime.now(UTC).timestamp() * 1_000_000)
    if resource_id is None:
        return None
    return {
        "type": resource_type,
        "id": str(resource_id),
        "version": version,
        "actor": actor,
        "label": label,
        "at": datetime.now(UTC).isoformat(),
    }


def _resource_type(resource: object) -> str | None:
    """把变更实体映射为 M7 用户流的资源类别。"""
    if isinstance(resource, Run):
        return "run"
    if isinstance(resource, Evaluation):
        return "evaluation"
    if isinstance(resource, Device):
        return "device"
    if isinstance(resource, (Benchmark, MetricProfile, GatePolicy)):
        return "config"
    if isinstance(resource, Job):
        return "job"
    if isinstance(resource, Report):
        return "report"
    if isinstance(resource, (AppUser, UserGrant)):
        return "permissions"
    return None


def _is_permission_change(resource: object) -> bool:
    """只把用户权限字段变化广播为 permissions.changed。"""
    if isinstance(resource, UserGrant):
        return True
    if isinstance(resource, AppUser):
        state = inspect(resource)
        return any(
            state.attrs[field].history.has_changes() for field in ("role", "active", "ldap_groups")
        )
    return False


def notify_resource_change(
    session: Session,
    *,
    resource_type: str,
    resource_id: str,
    version: int,
    actor: str | None,
    label: str | None,
) -> None:
    """为绕过 ORM flush 的原子 SQL 更新追加同事务通知。"""
    payload = {
        "type": resource_type,
        "id": resource_id,
        "version": version,
        "actor": actor,
        "label": label,
        "at": datetime.now(UTC).isoformat(),
    }
    session.execute(select(func.pg_notify("asr_events", json.dumps(payload, ensure_ascii=False))))


@event.listens_for(Session, "before_flush")
def _advance_async_resource_versions(
    session: Session, _flush_context: Any, _instances: Any
) -> None:
    """Run、评分和 job 每次 ORM 更新都推进可供前端失效的版本号。"""
    for resource in session.dirty:
        if isinstance(resource, (Run, Evaluation, Job)) and session.is_modified(
            resource, include_collections=True
        ):
            resource.version += 1


@event.listens_for(Session, "after_flush")
def _notify_resource_changes(session: Session, _flush_context: Any) -> None:
    """在业务事务中写入 pg_notify，提交后其他 API 进程即可收到信号。"""
    resources = [*session.new, *session.dirty, *session.deleted]
    notified: set[tuple[str, str, int]] = session.info.setdefault("asr_notified", set())
    for resource in resources:
        resource_type = _resource_type(resource)
        if resource_type is None:
            continue
        if isinstance(resource, AppUser) and not _is_permission_change(resource):
            continue
        payload = _event_data(resource, resource_type)
        if payload is None:
            continue
        key = (resource_type, payload["id"], int(payload["version"]))
        if key in notified:
            continue
        notified.add(key)
        notify_resource_change(
            session,
            resource_type=resource_type,
            resource_id=payload["id"],
            version=int(payload["version"]),
            actor=payload["actor"],
            label=payload["label"],
        )


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _clear_notified_resources(session: Session) -> None:
    """允许同一 Session 的后续事务再次发送相同版本的变更信号。"""
    session.info.pop("asr_notified", None)
