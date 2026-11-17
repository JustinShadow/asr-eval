"""异步导入任务（M1 §4/§5.9，统一使用 M7 §6 Job 资源）。

为什么用线程而不是进程：导入瓶颈是磁盘指纹（IO），线程池足够；
任务状态全在 PostgreSQL，进程崩溃后任务行残留 running，
重启后可对同版本重入（断点续跑，M1 §5.9）。
"""

import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ApiError
from app.domain.datasets import _run_import, resolve_importer
from app.domain.models import Dataset, Job

# 进行中的任务线程，便于测试等待（不用于生产调度）
_workers: dict[uuid.UUID, threading.Thread] = {}


def start_import(
    db: Session,
    dataset: Dataset,
    *,
    root: Path,
    limit: int,
    version: str | None,
    created_by: str | None,
    source_type: str | None = None,
    allow_missing_ref: bool = False,
    subset_mapping: Mapping[str, str] | None = None,
    audio_profile_overrides: Mapping[str, str] | None = None,
) -> Job:
    """创建导入任务并后台执行；返回 202 语义的任务行（D-M7-6）。"""
    # 同步校验：非法请求直接 422，而不是 202 后失败
    resolved_source_type, _ = resolve_importer(dataset, root, source_type=source_type)
    dataset.source_type = resolved_source_type

    task = Job(
        kind="import",
        ref_type="dataset",
        ref_id=str(dataset.id),
        status="queued",
        created_by=created_by,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 任务线程绑定与请求相同的数据库（测试注入 asr_eval_test 也能工作）
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    thread = threading.Thread(
        target=_run_task,
        args=(
            factory,
            task.id,
            dataset.id,
            root,
            limit,
            version,
            created_by,
            allow_missing_ref,
            subset_mapping or {},
            audio_profile_overrides or {},
        ),
        daemon=True,
    )
    _workers[task.id] = thread
    thread.start()
    return task


def _run_task(
    factory: sessionmaker[Session],
    task_id: uuid.UUID,
    dataset_id: uuid.UUID,
    root: Path,
    limit: int,
    version: str | None,
    created_by: str | None,
    allow_missing_ref: bool,
    subset_mapping: dict[str, str],
    audio_profile_overrides: dict[str, str],
) -> None:
    """任务线程体：独立会话执行 _run_import，终态回写任务行。"""
    db = factory()
    try:
        dataset = db.get(Dataset, dataset_id)
        task = db.scalar(select(Job).where(Job.id == task_id).with_for_update())
        if dataset is None or task is None:
            return
        if task.status == "cancelled":
            return
        if task.cancel_requested_at is not None:
            task.status = "cancelled"
            task.error_code = "E_JOB_CANCELLED"
            task.error_detail = {"message": "导入任务已取消"}
            task.finished_at = datetime.now(UTC)
            db.commit()
            return
        # select_importer 在 start_import 已通过；此处只取实例
        from app.domain.datasets import select_importer

        importer = select_importer(dataset, root)
        version_row = _run_import(
            db,
            dataset,
            root=root,
            limit=limit,
            version=version,
            created_by=created_by,
            importer=importer,
            task=task,
            allow_missing_ref=allow_missing_ref,
            subset_mapping=subset_mapping,
            audio_profile_overrides=audio_profile_overrides,
        )
        task = db.scalar(select(Job).where(Job.id == task_id).with_for_update())
        if task is not None:
            task.ref_type = "dataset_version"
            task.ref_id = str(version_row.id)
            task.progress = {**(task.progress or {}), "phase": "done"}
            task.result = {
                "resource_id": str(version_row.id),
                "dataset_id": str(dataset.id),
                "url": f"/api/v1/dataset-versions/{version_row.id}",
                "status": version_row.status,
                "item_count": version_row.item_count,
                "stats": version_row.stats,
            }
            task.status = "cancelled" if task.cancel_requested_at else "succeeded"
            if task.status == "cancelled":
                task.error_code = "E_JOB_CANCELLED"
                task.error_detail = {"message": "导入任务已取消"}
            task.finished_at = datetime.now(UTC)
            db.commit()
    except Exception as exc:
        db.rollback()
        task = db.get(Job, task_id)
        if task is not None:
            if isinstance(exc, ApiError):
                error_code = exc.code
                message = exc.message
            else:
                error_code = "E_IMPORT_FAILED"
                message = str(exc)
            cancelled = error_code == "E_JOB_CANCELLED" or task.cancel_requested_at is not None
            task.status = "cancelled" if cancelled else "failed"
            task.error_code = "E_JOB_CANCELLED" if cancelled else error_code
            task.error_detail = {"message": "导入任务已取消" if cancelled else message[:1024]}
            task.finished_at = datetime.now(UTC)
            db.commit()
    finally:
        db.close()
        _workers.pop(task_id, None)


def job_dict(task: Job) -> dict[str, Any]:
    """统一任务序列化（M7 §6）。"""
    return {
        "id": str(task.id),
        "kind": task.kind,
        "ref": {"type": task.ref_type, "id": task.ref_id},
        "status": task.status,
        "progress": task.progress or {},
        "result": task.result,
        "error_code": task.error_code,
        "error_detail": task.error_detail,
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def wait_for_tasks(timeout_s: float = 120.0) -> None:
    """测试辅助：等待所有进行中的任务线程结束。"""
    for thread in list(_workers.values()):
        thread.join(timeout=timeout_s)
