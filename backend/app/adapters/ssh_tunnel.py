"""设备 ASR 回环端口的 SSH 本地转发（M2 §5.3.1）。

205/600 的 ASR 只监听设备 ``127.0.0.1:8701``，平台不能改设备 SSFConfig，
也不应把 8701 暴露到局域网。这里在平台侧建立一条只绑定 ``127.0.0.1`` 的
SSH 本地转发，adapter 再通过临时本地端口访问 /v1/models 与 ASR WebSocket。

约束：

- 隧道复用 ``ssh`` 客户端的 key/agent 与 known_hosts，不保存明文密钥到命令行；
- 仅对 ``kind=etsstt`` 且配置了 ``ssh_user`` 的设备启用；
- 目标地址固定为设备回环，不提供任意转发目标；
- 进程退出后下次调用自动重建。
"""

import atexit
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_TUNNEL_START_TIMEOUT_S = 6.0
_DIRECT_CHECK_TIMEOUT_S = 0.35
_DIRECT_CACHE_TTL_S = 30.0


class TunnelError(RuntimeError):
    """无法建立 SSH 本地转发。"""


class DeviceLike(Protocol):
    """隧道只需要设备连接字段，避免 adapters 反向依赖 domain。"""

    id: Any
    kind: str
    host: str
    port: int
    ssh_user: str | None
    ssh_port: int


@dataclass
class _Tunnel:
    process: subprocess.Popen[bytes]
    local_port: int
    ssh_host: str
    ssh_port: int
    ssh_user: str
    remote_port: int


@dataclass
class _DirectCache:
    reachable: bool
    expires_at: float


_tunnels: dict[str, _Tunnel] = {}
_direct_cache: dict[str, _DirectCache] = {}
_lock = threading.Lock()


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _port_open(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop_tunnel(tunnel: _Tunnel) -> None:
    if tunnel.process.poll() is None:
        tunnel.process.terminate()
        try:
            tunnel.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            tunnel.process.kill()
            tunnel.process.wait(timeout=2)


def _ensure_tunnel(
    *,
    device_id: str,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    remote_port: int,
) -> int:
    """复用或创建一条只绑定 127.0.0.1 的 SSH 本地转发。"""
    with _lock:
        existing = _tunnels.get(device_id)
        if existing is not None:
            if existing.process.poll() is None and _port_open(
                "127.0.0.1", existing.local_port, 0.2
            ):
                return existing.local_port
            _stop_tunnel(existing)
            _tunnels.pop(device_id, None)

        ssh = shutil.which("ssh")
        if ssh is None:
            raise TunnelError("运行环境缺少 ssh 客户端，无法建立设备端口转发")
        local_port = _free_port()
        command = [
            ssh,
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-p",
            str(ssh_port),
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        ]
        known_hosts = os.environ.get("ASR_DEVICE_SSH_KNOWN_HOSTS")
        if known_hosts:
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                    "-o",
                    "StrictHostKeyChecking=yes",
                ]
            )
        command.append(f"{ssh_user}@{ssh_host}")
        process = subprocess.Popen(  # noqa: S603 - 固定 ssh 参数，目标来自设备配置。
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + _TUNNEL_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TunnelError(
                    f"SSH 转发进程退出（exit={process.returncode}）；"
                    "请确认设备 SSH key/known_hosts 可用"
                )
            if _port_open("127.0.0.1", local_port, 0.2):
                _tunnels[device_id] = _Tunnel(
                    process=process,
                    local_port=local_port,
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    remote_port=remote_port,
                )
                logger.info(
                    "设备 %s ASR 隧道已建立：127.0.0.1:%s -> %s:%s",
                    device_id,
                    local_port,
                    ssh_host,
                    remote_port,
                )
                return local_port
            time.sleep(0.1)
        process.terminate()
        raise TunnelError("SSH 转发在超时内未就绪；请检查设备 SSH 与 127.0.0.1:端口")


def device_endpoint(device: DeviceLike) -> tuple[str, int]:
    """返回 adapter 应连接的 ASR 地址；必要时建立 SSH 本地转发。"""
    if device.kind != "etsstt" or not device.ssh_user or _is_loopback(device.host):
        return device.host, device.port

    device_id = str(device.id)
    now = time.monotonic()
    cached = _direct_cache.get(device_id)
    if cached is not None and cached.expires_at > now:
        if cached.reachable:
            return device.host, device.port
    else:
        reachable = _port_open(device.host, device.port, _DIRECT_CHECK_TIMEOUT_S)
        _direct_cache[device_id] = _DirectCache(
            reachable=reachable, expires_at=now + _DIRECT_CACHE_TTL_S
        )
        if reachable:
            return device.host, device.port

    local_port = _ensure_tunnel(
        device_id=device_id,
        ssh_host=device.host,
        ssh_port=device.ssh_port,
        ssh_user=device.ssh_user,
        remote_port=device.port,
    )
    return "127.0.0.1", local_port


def close_all_tunnels() -> None:
    """应用退出时尽力关闭所有转发进程。"""
    with _lock:
        for tunnel in _tunnels.values():
            _stop_tunnel(tunnel)
        _tunnels.clear()
        _direct_cache.clear()


atexit.register(close_all_tunnels)
