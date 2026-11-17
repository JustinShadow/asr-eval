"""asrctl doctor 命令的 HTTP 契约测试。"""

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any


def _run_doctor(payloads: dict[str, dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    """用本地假 API 服务运行只读 doctor 命令。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = payloads.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        script = Path(__file__).resolve().parents[2] / "tools" / "asrctl.py"
        # 运行的是仓库脚本，命令与参数固定，唯一动态参数是本机临时 HTTP 端口。
        return subprocess.run(  # noqa: S603
            [sys.executable, str(script), "doctor", "--base-url", base_url],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_asrctl_doctor_passes_with_ready_service() -> None:
    result = _run_doctor(
        {
            "/healthz": {"status": "ok"},
            "/readyz": {
                "status": "ok",
                "checks": {
                    "data_dir": "ok",
                    "database": "ok",
                    "nfs": {"status": "not_configured"},
                    "storage": {"status": "ok", "used_percent": 20.0},
                },
            },
            "/version": {
                "version": "0.1.0",
                "schema_version": "0036_device_credentials",
                "schema_head": "0036_device_credentials",
            },
        }
    )

    assert result.returncode == 0
    assert "[OK] /healthz 正常" in result.stdout
    assert "[OK] /readyz ok" in result.stdout
    assert (
        "数据库 schema 0036_device_credentials，代码 head 0036_device_credentials" in result.stdout
    )
    assert "自检通过" in result.stdout


def test_asrctl_doctor_fails_when_database_or_schema_is_unavailable() -> None:
    result = _run_doctor(
        {
            "/healthz": {"status": "ok"},
            "/readyz": {"status": "degraded", "checks": {"database": "error"}},
            "/version": {
                "version": "0.1.0",
                "schema_version": "0035_previous",
                "schema_head": "0036_device_credentials",
            },
        }
    )

    assert result.returncode == 1
    assert "[FAIL] /readyz degraded" in result.stdout
    assert "[FAIL] 版本" in result.stdout
    assert "代码 head 0036_device_credentials" in result.stdout
    assert "自检未通过" in result.stdout
