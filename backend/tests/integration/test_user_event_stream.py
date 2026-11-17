"""用户级 PostgreSQL 变更通知与 SSE ticket 验收。"""

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Device
from app.domain.user_events import user_event_bus

pytestmark = pytest.mark.integration


def _read_sse_event(
    client: TestClient,
    ticket: str,
    cursor: str,
    *,
    cursor_in_query: bool = False,
) -> tuple[int, list[str]]:
    """读取一条 SSE 帧后关闭流，避免测试保留后台连接。"""
    lines: list[str] = []
    params = {"ticket": ticket}
    headers = {"Last-Event-ID": cursor}
    if cursor_in_query:
        params["last_event_id"] = cursor
        headers = {}
    with client.stream("GET", "/api/v1/events", params=params, headers=headers) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            lines.append(line)
            if line.startswith("data:"):
                return response.status_code, lines
    return response.status_code, lines


def test_committed_resource_change_reaches_ticketed_stream_and_resyncs(
    client: TestClient,
) -> None:
    """ORM 事务提交后经 LISTEN/NOTIFY 到达 SSE，ticket 一次性且断档要求重同步。"""
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    auth_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    listener = client.app.state.user_event_listener
    assert listener.wait_until_ready(timeout=5)
    previous = user_event_bus.buffer.replay("user", None).latest
    cursor = previous.event_id if previous is not None else 0

    factory = client.app.state.evaluation_session_factory
    suffix = uuid.uuid4().hex[:8]
    with factory() as db:
        device = Device(
            name=f"用户流设备-{suffix}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9000,
        )
        db.add(device)
        db.commit()
        device_id = str(device.id)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        replay = user_event_bus.buffer.replay("user", str(cursor))
        if any(
            event.event == "resource.changed" and json.loads(event.data).get("id") == device_id
            for event in replay.events
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("PostgreSQL LISTEN/NOTIFY 未转发设备变更")

    ticket_response = client.post("/api/v1/auth/stream-ticket", headers=auth_headers)
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]
    status, lines = _read_sse_event(client, ticket, str(cursor), cursor_in_query=True)
    assert status == 200
    assert "event: resource.changed" in lines
    event_payload = next(json.loads(line[6:]) for line in lines if line.startswith("data:"))
    assert event_payload["type"] == "device"
    assert event_payload["id"] == device_id
    assert event_payload["version"] == 1
    assert "actor" in event_payload and "at" in event_payload

    reused = client.get("/api/v1/events", params={"ticket": ticket})
    assert reused.status_code == 401
    assert reused.json()["code"] == "E_TICKET_INVALID"

    next_ticket = client.post("/api/v1/auth/stream-ticket", headers=auth_headers).json()["ticket"]
    _, resync_lines = _read_sse_event(client, next_ticket, "999999999")
    assert "event: resync" in resync_lines
    resync_payload = next(json.loads(line[6:]) for line in resync_lines if line.startswith("data:"))
    assert resync_payload["reason"] == "history_unavailable"

    disconnected_cursor = next(int(line[4:]) for line in resync_lines if line.startswith("id:"))
    time.sleep(30)
    with factory() as db:
        later_device = Device(
            name=f"断线续传设备-{uuid.uuid4().hex[:8]}",
            platform="205",
            kind="etsstt",
            host="127.0.0.1",
            port=9001,
        )
        db.add(later_device)
        db.commit()
        later_device_id = str(later_device.id)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        replay = user_event_bus.buffer.replay("user", str(disconnected_cursor))
        if any(
            event.event == "resource.changed"
            and json.loads(event.data).get("id") == later_device_id
            for event in replay.events
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("SSE 断开 30 秒后产生的设备变更未进入事件缓冲")

    reconnect_ticket = client.post("/api/v1/auth/stream-ticket", headers=auth_headers).json()[
        "ticket"
    ]
    _, resumed_lines = _read_sse_event(client, reconnect_ticket, str(disconnected_cursor))
    assert "event: resource.changed" in resumed_lines
    resumed_payload = next(
        json.loads(line[6:]) for line in resumed_lines if line.startswith("data:")
    )
    assert resumed_payload["id"] == later_device_id
