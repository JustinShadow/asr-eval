"""执行期设备硬件资源采样（只读）。

通过设备 SSH 读取 /proc 与 sysfs；不修改设备文件，不保存凭据。
仅对配置了 SSH 的本地设备生效；失败时由调用方跳过，不影响 Run。
"""

from __future__ import annotations

import re
from typing import Any

from app.adapters.etsstt import _run_authenticated_ssh
from app.domain.credentials import DeviceCredentialValue

_RESOURCE_SCRIPT = r"""
printf '##MEM\n'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
printf '##LOAD\n'; cat /proc/loadavg
printf '##CPU1\n'; head -1 /proc/stat
sleep 0.5
printf '##CPU2\n'; head -1 /proc/stat
printf '##NPU\n'
cat /sys/kernel/debug/rknpu/load 2>/dev/null || \
  cat /sys/class/devfreq/27700000.npu/load 2>/dev/null || true
printf '##GPU\n'
cat /sys/class/devfreq/27800000.gpu/load 2>/dev/null || true
printf '##THERMAL\n'
for f in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$f" ] && cat "$f"
done 2>/dev/null
"""


def _sections(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in output.splitlines():
        marker = re.match(r"^##(\w+)$", line.strip())
        if marker:
            current = marker.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def _cpu_percent(first: str, second: str) -> float | None:
    def parse(line: str) -> tuple[int, int] | None:
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(value) for value in parts[1:]]
        except ValueError:
            return None
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    left = parse(first)
    right = parse(second)
    if left is None or right is None:
        return None
    total_delta = right[0] - left[0]
    idle_delta = right[1] - left[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(1.0, 1 - idle_delta / total_delta)) * 100, 2)


def _load_percent(lines: list[str]) -> tuple[float | None, int | None]:
    for line in lines:
        text = line.strip()
        if not text:
            continue
        match = re.search(r"Core\d*:\s*(\d+)%", text)
        if match:
            values = [int(value) for value in re.findall(r"Core\d*:\s*(\d+)%", text)]
            return (sum(values) / len(values) if values else None), None
        match = re.match(r"^(\d+)@(\d+)Hz$", text)
        if match:
            return float(match.group(1)), int(match.group(2)) // 1_000_000
    return None, None


def _memory_kb(lines: list[str]) -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    for line in lines:
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        match = re.search(r"\d+", parts[1])
        if match:
            values[key] = int(match.group(0))
    return values.get("MemTotal"), values.get("MemAvailable")


def parse_resource_output(output: str) -> dict[str, Any]:
    """把固定脚本输出解析为结构化资源指标。"""
    sections = _sections(output)
    mem_total, mem_available = _memory_kb(sections.get("MEM", []))
    cpu_pct = _cpu_percent((sections.get("CPU1") or [""])[0], (sections.get("CPU2") or [""])[0])
    npu_pct, npu_mhz = _load_percent(sections.get("NPU", []))
    gpu_pct, gpu_mhz = _load_percent(sections.get("GPU", []))
    temperatures = []
    for line in sections.get("THERMAL", []):
        try:
            value = int(line.strip())
        except ValueError:
            continue
        if value > 0:
            temperatures.append(value / 1000)
    load1 = None
    load_lines = (sections.get("LOAD") or [""])[0].split()
    if load_lines:
        try:
            load1 = float(load_lines[0])
        except ValueError:
            load1 = None
    mem_used_pct = None
    if mem_total and mem_available is not None:
        mem_used_pct = round((mem_total - mem_available) / mem_total * 100, 2)
    return {
        "cpu_pct": cpu_pct,
        "mem_total_mb": round(mem_total / 1024) if mem_total else None,
        "mem_available_mb": round(mem_available / 1024) if mem_available is not None else None,
        "mem_used_pct": mem_used_pct,
        "npu_pct": npu_pct,
        "npu_freq_mhz": npu_mhz,
        "gpu_pct": gpu_pct,
        "gpu_freq_mhz": gpu_mhz,
        "temp_c_max": round(max(temperatures), 1) if temperatures else None,
        "load1": load1,
    }


async def sample_device_resources(
    *,
    host: str,
    ssh_port: int,
    ssh_user: str | None,
    credential: DeviceCredentialValue | None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """执行一次只读采样；设备不支持或 SSH 不可用时抛异常。"""
    if not ssh_user:
        raise RuntimeError("设备未配置 SSH 用户")
    ssh_password = credential.secret if credential and credential.kind == "ssh_password" else None
    ssh_private_key = (
        credential.secret if credential and credential.kind == "ssh_private_key" else None
    )
    passphrase = credential.passphrase if credential else None
    output = await _run_authenticated_ssh(
        host,
        ssh_port,
        ssh_user,
        _RESOURCE_SCRIPT,
        ssh_password=ssh_password,
        ssh_private_key=ssh_private_key,
        ssh_passphrase=passphrase,
        timeout_s=timeout_s,
    )
    return parse_resource_output(output)
