#!/usr/bin/env python3
"""ASR 评测平台本机运维命令。"""

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _get_json(base_url: str, path: str, timeout: float) -> tuple[dict[str, Any] | None, str | None]:
    """请求只读诊断端点并解析 JSON。"""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "API 根地址必须使用 HTTP 或 HTTPS"
    if parsed.query or parsed.fragment:
        return None, "API 根地址不能带 query 或 fragment"
    endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}{path}"
    request = Request(endpoint, headers={"Accept": "application/json"})  # noqa: S310
    try:
        # URL scheme 已限制为 HTTP(S)，命令只执行固定的只读健康端点。
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read())
    except HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError) as exc:
        return None, str(exc.reason if isinstance(exc, URLError) else exc)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "响应不是有效 JSON"
    if not isinstance(payload, dict):
        return None, "响应 JSON 顶层不是对象"
    return payload, None


def _print_result(ok: bool, message: str) -> None:
    """输出一条中文诊断结果。"""
    print(f"[{'OK' if ok else 'FAIL'}] {message}")


def _check_status(check: Any) -> str | None:
    """读取探针检查项的统一状态字段。"""
    if isinstance(check, str):
        return check
    if isinstance(check, dict) and isinstance(check.get("status"), str):
        return check["status"]
    return None


def doctor(base_url: str, timeout: float) -> int:
    """检查进程存活、依赖就绪和数据库迁移版本。"""
    success = True
    health, error = _get_json(base_url, "/healthz", timeout)
    health_ok = error is None and health is not None and health.get("status") == "ok"
    _print_result(health_ok, "/healthz 正常" if health_ok else f"/healthz 失败：{error or health}")
    success = success and health_ok

    ready, error = _get_json(base_url, "/readyz", timeout)
    ready_checks = ready.get("checks", {}) if ready else {}
    ready_ok = bool(
        error is None
        and ready
        and ready.get("status") == "ok"
        and ready_checks.get("database") == "ok"
        and ready_checks.get("data_dir") == "ok"
        and _check_status(ready_checks.get("storage")) == "ok"
        and _check_status(ready_checks.get("nfs")) in {"ok", "not_configured"}
    )
    _print_result(
        ready_ok,
        f"/readyz {ready.get('status')}：{ready_checks}"
        if ready is not None and error is None
        else f"/readyz 失败：{error}",
    )
    success = success and ready_ok

    version, error = _get_json(base_url, "/version", timeout)
    schema_version = version.get("schema_version") if version else None
    schema_head = version.get("schema_head") if version else None
    version_ok = bool(
        error is None
        and version
        and version.get("version")
        and schema_version not in {None, "", "0", "unknown", "not_configured"}
        and schema_head not in {None, "", "unknown", "not_configured"}
        and schema_version == schema_head
    )
    version_message = (
        f"版本 {version.get('version')}，数据库 schema {schema_version}，代码 head {schema_head}"
        if version is not None and error is None
        else f"/version 失败：{error}"
    )
    if not version_ok and version is not None and error is None:
        version_message += "；schema 与代码不一致，请检查后运行 make migrate"
    _print_result(
        version_ok,
        version_message,
    )
    success = success and version_ok

    print("\n自检通过" if success else "\n自检未通过，请根据失败项检查服务配置与日志")
    return 0 if success else 1


def main() -> int:
    """解析并执行 asrctl 子命令。"""
    parser = argparse.ArgumentParser(prog="asrctl", description="ASR 评测平台本机运维命令")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="检查本机服务健康状态")
    doctor_parser.add_argument(
        "--base-url",
        default=os.environ.get("ASR_API_URL", "http://127.0.0.1:8080"),
        help="API 根地址，默认 http://127.0.0.1:8080",
    )
    doctor_parser.add_argument("--timeout", type=float, default=3.0, help="单个端点超时秒数")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.command == "doctor":
        return doctor(args.base_url, args.timeout)
    parser.error("未知命令")
    return 2


if __name__ == "__main__":
    sys.exit(main())
