"""设备回环 ASR 的 SSH 本地转发选择逻辑测试。"""

from types import SimpleNamespace

from app.adapters import ssh_tunnel


def test_loopback_and_gateway_use_direct_endpoint() -> None:
    """已在本机回环的设备或网关不额外建隧道。"""
    loopback = SimpleNamespace(
        id="d1", kind="etsstt", host="127.0.0.1", port=8701, ssh_user="etsme", ssh_port=22
    )
    gateway = SimpleNamespace(
        id="d2", kind="gateway", host="192.0.2.10", port=30025, ssh_user="tester", ssh_port=22
    )

    assert ssh_tunnel.device_endpoint(loopback) == ("127.0.0.1", 8701)
    assert ssh_tunnel.device_endpoint(gateway) == ("192.0.2.10", 30025)


def test_closed_direct_port_starts_ssh_tunnel(monkeypatch) -> None:
    """etsSTT 直连 8701 不通时，返回隧道本地端点。"""
    ssh_tunnel._direct_cache.clear()
    device = SimpleNamespace(
        id="d3", kind="etsstt", host="192.0.2.10", port=8701, ssh_user="etsme", ssh_port=22
    )
    calls: list[dict[str, object]] = []

    def fake_tunnel(**kwargs: object) -> int:
        calls.append(kwargs)
        return 23456

    monkeypatch.setattr(ssh_tunnel, "_port_open", lambda *_args: False)
    monkeypatch.setattr(ssh_tunnel, "_ensure_tunnel", fake_tunnel)

    assert ssh_tunnel.device_endpoint(device) == ("127.0.0.1", 23456)
    assert calls == [
        {
            "device_id": "d3",
            "ssh_host": "192.0.2.10",
            "ssh_port": 22,
            "ssh_user": "etsme",
            "remote_port": 8701,
        }
    ]


def test_direct_port_wins_when_reachable(monkeypatch) -> None:
    """设备已把 ASR 暴露到局域网时，不强制走隧道。"""
    ssh_tunnel._direct_cache.clear()
    device = SimpleNamespace(
        id="d4", kind="etsstt", host="192.0.2.10", port=8701, ssh_user="etsme", ssh_port=22
    )
    monkeypatch.setattr(ssh_tunnel, "_port_open", lambda *_args: True)

    assert ssh_tunnel.device_endpoint(device) == ("192.0.2.10", 8701)
