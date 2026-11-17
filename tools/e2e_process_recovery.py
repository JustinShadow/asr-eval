#!/usr/bin/env python3
"""隔离验证 app 进程崩溃后 Run 恢复并完成识别评分。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.domain.models import (  # noqa: E402
    Base,
    Benchmark,
    CaseResult,
    Device,
    DeviceLease,
    Evaluation,
    Job,
    Run,
    RunTarget,
)

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
TRANSCRIPT = "2024年这是一条合成回归样本"


def _reserve_port() -> int:
    """取得本机临时 TCP 端口，供一次性 app 与假设备进程使用。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(process: subprocess.Popen[bytes], url: str, label: str) -> None:
    """等待子进程 HTTP 服务就绪，并在启动失败时给出明确错误。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} 提前退出，exit={process.returncode}")
        try:
            response = httpx.get(url, timeout=0.8, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"等待 {label} 就绪超时：{url}")


def _start_app(
    *,
    port: int,
    environment: dict[str, str],
    log_file: BinaryIO,
) -> subprocess.Popen[bytes]:
    """以单进程 Uvicorn 启动 app，便于精确模拟进程崩溃。"""
    # 仅以当前 Python 环境启动固定 uvicorn 模块，不经 shell 执行任意命令。
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def _stop_process(process: subprocess.Popen[bytes] | None, *, force: bool) -> None:
    """只停止本脚本创建的临时子进程。"""
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGKILL if force else signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_state(engine: Engine, run_id: uuid.UUID) -> dict[str, object]:
    """读取恢复 E2E 所需的持久状态，不返回音频或转写文本。"""
    with Session(engine) as db:
        run = db.get(Run, run_id)
        if run is None:
            raise RuntimeError("E2E Run 不存在")
        target = db.scalar(select(RunTarget).where(RunTarget.run_id == run_id))
        if target is None:
            raise RuntimeError("E2E RunTarget 不存在")
        lease = db.get(DeviceLease, target.device_id)
        job = db.scalar(
            select(Job)
            .where(Job.kind == "run", Job.ref_type == "run", Job.ref_id == str(run_id))
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        evaluation = db.scalar(
            select(Evaluation)
            .where(Evaluation.run_id == run_id)
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .limit(1)
        )
        result_count = db.scalar(
            select(func.count(CaseResult.id)).where(CaseResult.run_target_id == target.id)
        )
        return {
            "run_status": run.status,
            "target_status": target.status,
            "target_stats": dict(target.stats or {}),
            "job_status": job.status if job is not None else None,
            "lease_present": lease is not None,
            "lease_expires_at": lease.expires_at if lease is not None else None,
            "lease_acquired_at": lease.acquired_at if lease is not None else None,
            "evaluation_status": evaluation.status if evaluation is not None else None,
            "result_count": result_count,
        }


def _wait_for_first_attempt(
    engine: Engine,
    run_id: uuid.UUID,
    marker: Path,
    process: subprocess.Popen[bytes],
) -> dict[str, object]:
    """等 worker 已持有设备租约并到达假设备 final 屏障。"""
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"首个 app 进程提前退出，exit={process.returncode}")
        state = _run_state(engine, run_id)
        if (
            marker.exists()
            and state["run_status"] == "running"
            and state["target_status"] == "running"
        ):
            if state["lease_expires_at"] is None:
                raise RuntimeError("Run 正在执行但没有设备租约")
            return state
        if state["run_status"] in {"failed", "cancelled", "partial"}:
            raise RuntimeError(f"首次 Run 未进入可中断执行状态：{state}")
        time.sleep(0.1)
    raise TimeoutError("Run 未在 45 秒内到达假设备 final 屏障")


def _wait_for_lease_expiry(engine: Engine, device_id: uuid.UUID) -> None:
    """等待 PG 中真实 120 秒租约 TTL 自然到期，不改写租约时间。"""
    announced = 10**9
    deadline = time.monotonic() + 135
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "select extract(epoch from expires_at - now()) "
                    "from device_lease where device_id = :device_id"
                ),
                {"device_id": device_id},
            ).first()
        if row is None or row[0] is None:
            return
        remaining = float(row[0])
        if remaining <= 0:
            return
        bucket = int(remaining // 15)
        if bucket < announced:
            announced = bucket
            print(f"等待 PostgreSQL 设备租约自然过期：剩余约 {remaining}s", flush=True)
        time.sleep(min(1.0, remaining))
    raise TimeoutError("设备租约超过 135 秒仍未自然到期")


def _wait_for_completion(
    engine: Engine,
    run_id: uuid.UUID,
    process: subprocess.Popen[bytes],
) -> dict[str, object]:
    """等待恢复后的 Run、评分与 Job 全部进入成功终态。"""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"重启后的 app 提前退出，exit={process.returncode}")
        state = _run_state(engine, run_id)
        if state["run_status"] in {"failed", "partial", "cancelled"}:
            raise RuntimeError(f"恢复后的 Run 未成功：{state}")
        if (
            state["run_status"] == "completed"
            and state["target_status"] == "completed"
            and state["job_status"] == "succeeded"
            and state["evaluation_status"] == "completed"
            and state["result_count"] == 1
            and not state["lease_present"]
        ):
            return state
        time.sleep(0.2)
    raise TimeoutError(f"恢复后的 Run 未在 60 秒内完成：{_run_state(engine, run_id)}")


def main() -> None:
    """在独立 PostgreSQL schema 中实测 app 被杀后 Run 可恢复。"""
    database_name = urlsplit(TEST_DATABASE_URL).path.lstrip("/")
    if "test" not in database_name.split("_"):
        raise RuntimeError("进程恢复 E2E 只允许连接名称包含独立 test 标记的 PostgreSQL 数据库")

    schema = f"process_recovery_{uuid.uuid4().hex[:12]}"
    api_port = _reserve_port()
    device_port = _reserve_port()
    api_url = f"http://127.0.0.1:{api_port}"
    admin_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    schema_engine: Engine | None = None
    fake_process: subprocess.Popen[bytes] | None = None
    app_process: subprocess.Popen[bytes] | None = None

    with tempfile.TemporaryDirectory(prefix="asr-eval-process-recovery.") as temp_name:
        temp_dir = Path(temp_name)
        data_dir = temp_dir / "data"
        marker = temp_dir / "first-final.waiting"
        fake_log = (temp_dir / "fake-device.log").open("wb")
        app_logs = []
        try:
            with admin_engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_engine = create_engine(
                TEST_DATABASE_URL,
                connect_args={"options": f"-csearch_path={schema}"},
                pool_pre_ping=True,
            )
            Base.metadata.create_all(schema_engine)

            process_environment = os.environ.copy()
            process_environment.update(
                {
                    "APP_ENV": "dev",
                    "AUTH_MODE": "local",
                    "DATABASE_URL": TEST_DATABASE_URL,
                    "PGOPTIONS": f"-c search_path={schema}",
                    "DATA_DIR": str(data_dir),
                    "CREDENTIALS_KEY_FILE": str(data_dir / "secrets" / "credentials_key"),
                    "JWT_KEY": "process-recovery-e2e-key-not-used-outside-test-db",
                    "RUNNER_WORKERS": "1",
                    "RUNNER_POLL_INTERVAL_S": "0.1",
                    "SCORING_WORKERS": "1",
                    "ASR_E2E_DATA_DIR": str(temp_dir),
                    "ASR_E2E_FAKE_DEVICE_PORT": str(device_port),
                    "ASR_E2E_TRANSCRIPT": TRANSCRIPT,
                }
            )
            process_environment["PYTHONPATH"] = os.pathsep.join(
                filter(
                    None,
                    (str(BACKEND_ROOT), process_environment.get("PYTHONPATH")),
                )
            )
            fake_environment = process_environment | {
                "ASR_E2E_HOLD_FIRST_FINAL": "1",
                "ASR_E2E_FIRST_FINAL_MARKER": str(marker),
            }
            # 仅启动本地假设备固定入口，不经 shell 执行任意命令。
            fake_process = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "fakes.device_server:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(device_port),
                ],
                cwd=BACKEND_ROOT / "tests",
                env=fake_environment,
                stdout=fake_log,
                stderr=subprocess.STDOUT,
            )
            _wait_http(
                fake_process,
                f"http://127.0.0.1:{device_port}/v1/models",
                "本地假设备",
            )

            subprocess.run(
                [sys.executable, "../tools/e2e_seed.py"],
                cwd=BACKEND_ROOT,
                env=process_environment,
                check=True,
                timeout=30,
            )
            with Session(schema_engine) as db:
                benchmark = db.scalar(select(Benchmark).limit(1))
                device = db.scalar(select(Device).where(Device.name == "E2E 本地假设备"))
                if benchmark is None or device is None:
                    raise RuntimeError("E2E 种子未创建 Benchmark 或假设备")
                version_id = str(benchmark.dataset_version_id)
                benchmark_id = str(benchmark.id)
                device_id = device.id

            first_log = (temp_dir / "app-first.log").open("wb")
            app_logs.append(first_log)
            app_process = _start_app(
                port=api_port, environment=process_environment, log_file=first_log
            )
            _wait_http(app_process, f"{api_url}/healthz", "首个 app 进程")
            with httpx.Client(base_url=api_url, timeout=10, trust_env=False) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "admin12345"},
                )
                if login.status_code != 200:
                    raise RuntimeError(f"隔离 E2E 本地登录失败，HTTP {login.status_code}")
                token = login.json()["access_token"]
                created = client.post(
                    "/api/v1/runs",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "name": "进程崩溃恢复验收",
                        "dataset_version_id": version_id,
                        "benchmark_id": benchmark_id,
                        "device_id": str(device_id),
                        "auto_score": True,
                    },
                )
                if created.status_code != 201:
                    raise RuntimeError(f"隔离 E2E Run 创建失败，HTTP {created.status_code}")
                run_id = uuid.UUID(str(created.json()["run"]["id"]))

            running_state = _wait_for_first_attempt(schema_engine, run_id, marker, app_process)
            app_process.send_signal(signal.SIGKILL)
            app_process.wait(timeout=10)
            app_process = None
            print(
                "已 SIGKILL 首个 app 进程；Run/target/Job 与设备租约仍保存在 PostgreSQL。",
                flush=True,
            )

            stopped_state = _run_state(schema_engine, run_id)
            if not all(
                stopped_state[key] == expected
                for key, expected in (
                    ("run_status", "running"),
                    ("target_status", "running"),
                    ("job_status", "running"),
                )
            ):
                raise RuntimeError(f"进程被杀后持久状态不符合预期：{stopped_state}")
            acquired_at = running_state["lease_acquired_at"]
            expires_at = running_state["lease_expires_at"]
            if not isinstance(acquired_at, datetime) or not isinstance(expires_at, datetime):
                raise RuntimeError("无法读取 PostgreSQL 设备租约时间戳")
            lease_seconds = (expires_at - acquired_at).total_seconds()
            if not 115 <= lease_seconds <= 125:
                raise RuntimeError(f"设备租约时长应为 120 秒，实际 {lease_seconds:.1f}s")

            _wait_for_lease_expiry(schema_engine, device_id)
            print("设备租约已按 PostgreSQL 时间自然过期，启动第二个 app 进程。", flush=True)
            second_log = (temp_dir / "app-second.log").open("wb")
            app_logs.append(second_log)
            app_process = _start_app(
                port=api_port,
                environment=process_environment,
                log_file=second_log,
            )
            _wait_http(app_process, f"{api_url}/healthz", "重启后的 app 进程")
            final_state = _wait_for_completion(schema_engine, run_id, app_process)
            target_stats = final_state["target_stats"]
            recovered = isinstance(target_stats, dict) and bool(target_stats.get("recovered"))
            print(
                "进程恢复 E2E 通过：Run=completed、target=completed、Job=succeeded、"
                "Evaluation=completed、CaseResult=1、活动租约=0；设备租约配置为 120 秒。"
            )
            print(
                "状态摘要："
                f"首次运行={stopped_state['run_status']}，"
                f"恢复后={final_state['run_status']}，"
                f"target recovered={recovered}"
            )
        finally:
            _stop_process(app_process, force=True)
            _stop_process(fake_process, force=True)
            fake_log.close()
            for log_file in app_logs:
                log_file.close()
            if schema_engine is not None:
                schema_engine.dispose()
            with admin_engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            admin_engine.dispose()


if __name__ == "__main__":
    main()
