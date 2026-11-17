"""Run 期间周期性采样设备硬件资源；失败不阻塞执行。"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.domain.credentials import DeviceCredentialValue
from app.domain.device_resources import sample_device_resources
from app.domain.models import RunResourceSample


async def sample_resources_loop(
    factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    target_id: uuid.UUID,
    host: str,
    ssh_port: int,
    ssh_user: str | None,
    credential: DeviceCredentialValue | None,
    stop_event: asyncio.Event,
    interval_s: float = 15.0,
) -> None:
    """每 15s 采样一次；只读设备，失败静默跳过。"""
    while not stop_event.is_set():
        try:
            metrics = await sample_device_resources(
                host=host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                credential=credential,
            )
        except Exception:
            metrics = None
        if metrics is not None and any(
            metrics.get(key) is not None
            for key in ("cpu_pct", "mem_used_pct", "npu_pct", "temp_c_max")
        ):
            with factory() as db:
                db.add(
                    RunResourceSample(
                        run_id=run_id,
                        target_id=target_id,
                        sampled_at=datetime.now(UTC),
                        metrics=metrics,
                    )
                )
                db.commit()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            return


async def stop_sampler(task: asyncio.Task[None] | None) -> None:
    """取消采样任务并等待退出。"""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
