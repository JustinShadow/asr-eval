"""用户级事件 500ms 同资源合并单元测试。"""

import json

from app.domain.user_events import UserEventBus


def test_user_event_bus_coalesces_same_resource_inside_half_second() -> None:
    """短时重复变更只发一个失效信号，超过窗口后恢复发布。"""
    clock = [10.0]
    bus = UserEventBus(clock=lambda: clock[0])
    first = {"type": "run", "id": "r-1", "version": 1}
    second = {"type": "run", "id": "r-1", "version": 2}

    assert bus.publish(first) is not None
    clock[0] += 0.3
    assert bus.publish(second) is None

    clock[0] += 0.3
    assert bus.publish(second) is not None
    replay = bus.buffer.replay("user", "0")
    payloads = [json.loads(message.data) for message in replay.events]
    assert [payload["version"] for payload in payloads] == [1, 2]
