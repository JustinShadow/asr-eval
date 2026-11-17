"""数据集导入、版本生命周期与能力计算。

导入按 M1 状态机处理逐条质量问题、样本持久化、能力统计与可复现 manifest。
"""

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain import audit
from app.domain.models import (
    AudioAsset,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
    Job,
    RefSegment,
    RefText,
    SpeakerMeta,
)
from app.importers.aishell1 import Aishell1Importer
from app.importers.aishell3 import Aishell3Importer
from app.importers.alimeeting import AlimeetingImporter
from app.importers.apy import ApyImporter
from app.importers.audio import AudioFingerprint, AudioMetadata, fingerprint_wav, read_wav_metadata
from app.importers.base import Importer, RawItem, Segment, SpeakerInfo
from app.importers.kespeech import KespeechImporter
from app.importers.magicdata import MagicDataImporter
from app.importers.srt import SrtImporter

AISHELL_ROOT_HINT = "data_aishell"
# M7 §7：每 50 条提交一次扫描进度，兼顾界面更新频率与数据库写入开销。
IMPORT_SCAN_PROGRESS_INTERVAL = 50
# M1 §9.3：每批 500 条控制事务开销，同时让慢存储上的进度仍能及时刷新。
IMPORT_BATCH_SIZE = 500
SOURCE_TYPES: dict[str, Callable[[], Importer]] = {
    "aishell1": Aishell1Importer,
    "aishell3": Aishell3Importer,
    "apy": ApyImporter,
    "datatang": ApyImporter,
    "alimeeting": AlimeetingImporter,
    "magicdata": MagicDataImporter,
    "kespeech": KespeechImporter,
    "srt": SrtImporter,
}


def create_dataset(
    db: Session, *, name: str, source_type: str | None, description: str | None
) -> Dataset:
    """创建数据集；同名直接 409。"""
    exists = db.scalar(select(Dataset).where(Dataset.name == name))
    if exists is not None:
        raise ApiError("E_CONFLICT", "数据集名称已存在", status_code=409)
    dataset = Dataset(name=name, source_type=source_type, description=description)
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def resolve_importer(
    dataset: Dataset,
    root: Path,
    *,
    source_type: str | None = None,
) -> tuple[str, Importer]:
    """按用户选择或目录结构识别导入器，并检查根路径白名单。"""
    _check_root_allowed(root)
    selected_type = source_type or dataset.source_type
    if selected_type in {None, "auto"}:
        if not root.is_dir():
            raise ApiError("E_NO_IMPORTER", "数据目录不存在", status_code=422)
        candidates: list[str] = []
        candidates_to_check = (
            "aishell1",
            "aishell3",
            "kespeech",
            "alimeeting",
            "magicdata",
            "apy",
            "srt",
        )
        for candidate in candidates_to_check:
            if candidate == "srt" and not any(
                path.suffix.casefold() in {".mp4", ".srt"}
                for path in root.rglob("*")
                if path.is_file()
            ):
                continue
            if SOURCE_TYPES[candidate]().detect(root):
                candidates.append(candidate)
        if len(candidates) != 1:
            if not candidates:
                raise ApiError("E_NO_IMPORTER", "无法识别数据目录格式，请手动选择来源类型", 422)
            raise ApiError(
                "E_IMPORT_TYPE_AMBIGUOUS",
                "数据目录匹配多个格式，请手动选择来源类型",
                422,
                {"candidates": candidates},
            )
        selected_type = candidates[0]
    factory = SOURCE_TYPES.get(selected_type)
    if factory is None:
        raise ApiError("E_NO_IMPORTER", f"暂不支持 {selected_type}", 422)
    importer = factory()
    if not root.is_dir() or not importer.detect(root):
        raise ApiError(
            "E_NO_IMPORTER",
            f"目录不符合 {selected_type} 的结构",
            422,
        )
    return selected_type, importer


def select_importer(
    dataset: Dataset,
    root: Path,
    *,
    source_type: str | None = None,
) -> Importer:
    """按来源类型选择导入器；保留旧调用签名供同步导入与测试使用。"""
    return resolve_importer(dataset, root, source_type=source_type)[1]


def import_dataset(
    db: Session,
    dataset: Dataset,
    *,
    root: Path,
    limit: int,
    version: str | None,
    created_by: str | None,
) -> DatasetVersion:
    """按 source_type 选择导入器；同步执行。"""
    source_type, importer = resolve_importer(dataset, root)
    dataset.source_type = source_type

    return _run_import(
        db,
        dataset,
        root=root,
        limit=limit,
        version=version,
        created_by=created_by,
        importer=importer,
    )


def _check_root_allowed(root: Path) -> None:
    """导入根目录白名单（M1 §8 E_PATH_ESCAPE）。

    白名单为空 = 本机开发不限制；生产必须通过 IMPORT_ALLOWED_ROOTS 配置。
    """
    settings = get_settings()
    if not settings.import_allowed_roots:
        return
    resolved = root.expanduser().resolve()
    for entry in settings.import_allowed_roots:
        allowed = Path(entry).expanduser().resolve()
        if resolved == allowed or resolved.is_relative_to(allowed):
            return
    raise ApiError(
        "E_PATH_ESCAPE",
        f"导入根目录不在白名单内：{root}",
        status_code=422,
    )


def _validate_raw_paths(root: Path, raw: RawItem, *, resolved_root: Path | None = None) -> None:
    """拒绝数据集目录中的符号链接越界（M1 §1.3/§8）。"""
    allowed_root = resolved_root or root.expanduser().resolve()
    for path in (raw.audio, raw.origin_ref):
        if path is not None and not path.resolve().is_relative_to(allowed_root):
            raise ApiError("E_PATH_ESCAPE", f"导入文件越过白名单根目录：{path}", status_code=422)


def _run_import(
    db: Session,
    dataset: Dataset,
    *,
    root: Path,
    limit: int,
    version: str | None,
    created_by: str | None,
    importer: Importer,
    task: Job | None = None,
    allow_missing_ref: bool = False,
    subset_mapping: Mapping[str, str] | None = None,
    audio_profile_overrides: Mapping[str, str] | None = None,
) -> DatasetVersion:
    """通用导入：建版本/子集、逐条落库、汇总统计（M1 §5.1 状态机）。

    - task 非空时回写 phase/progress（异步任务进度，§5.9）；
    - 断点续跑：同版本 importing/failed 重入时跳过已存在的 (version, item_key)；
    - 失败阈值：failed/total > 1% → ready_with_errors（需人工确认）。
    """
    if not root.is_dir() or not importer.detect(root):
        raise ApiError(
            "E_NO_IMPORTER",
            f"目录不符合 {dataset.source_type} 的结构",
            status_code=422,
        )

    version_name = version or _next_version(db, dataset.id)
    duplicate = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset.id, DatasetVersion.version == version_name
        )
    )
    record: DatasetVersion | None = None
    if duplicate is not None:
        # 断点续跑：仅 importing/failed 可重入（M1 §5.9）
        if duplicate.status in {"importing", "failed"}:
            record = duplicate
            record.status = "importing"
        else:
            raise ApiError("E_CONFLICT", f"版本 {version_name} 已存在", status_code=409)
    if record is None:
        record = DatasetVersion(
            dataset_id=dataset.id,
            version=version_name,
            status="importing",
            import_params={
                "root": str(root),
                "limit": limit,
                "allow_missing_ref": allow_missing_ref,
                "subset_mapping": subset_mapping or {},
                "audio_profile_overrides": audio_profile_overrides or {},
            },
            created_by=created_by,
        )
        db.add(record)
    db.flush()
    if task is not None:
        task.ref_type = "dataset_version"
        task.ref_id = str(record.id)

    def check_task_cancellation() -> None:
        """在可安全停下的批次边界响应取消请求（M7 §6.2）。"""
        if task is None:
            return
        db.refresh(task, attribute_names=["status", "cancel_requested_at"])
        if task.status == "cancelled" or task.cancel_requested_at is not None:
            raise ApiError("E_JOB_CANCELLED", "导入任务已取消", status_code=409)

    phase_started_at: float | None = None
    active_phase: str | None = None

    def set_phase(phase: str, progress: dict[str, Any] | None = None) -> None:
        nonlocal active_phase, phase_started_at
        if phase != active_phase:
            active_phase = phase
            phase_started_at = perf_counter()
        record.status = "importing"
        if task is not None:
            check_task_cancellation()
            task.status = "running"
            if task.started_at is None:
                task.started_at = datetime.now(UTC)
            merged_progress = dict(task.progress or {})
            if progress is not None:
                merged_progress.update(progress)
            merged_progress["phase"] = phase
            if phase == "persisting":
                total = merged_progress.get("total")
                completed = merged_progress.get("completed")
                if (
                    isinstance(total, int)
                    and isinstance(completed, int)
                    and completed > 0
                    and phase_started_at is not None
                ):
                    elapsed_ms = (perf_counter() - phase_started_at) * 1000
                    remaining = max(0, total - completed)
                    merged_progress["eta_ms"] = int(elapsed_ms * remaining / completed)
                else:
                    merged_progress["eta_ms"] = None
            else:
                merged_progress["eta_ms"] = None
            task.progress = merged_progress
        db.commit()

    set_phase("scanning", {"scanned": 0, "persisted": 0, "completed": 0, "errors": 0})

    imported = 0
    duration_total = 0
    batch_size = IMPORT_BATCH_SIZE
    fingerprint_pool: ThreadPoolExecutor | None = None
    # 按 M1 §8 分组收集错误；报告写入 stats.errors（计数 + 前 20 条样例）
    error_counts: dict[str, int] = {}
    error_samples: dict[str, list[str]] = {}
    seen_keys: set[str] = set()
    failed_rows: set[int] = set()

    def note_error(code: str, sample: str) -> None:
        error_counts[code] = error_counts.get(code, 0) + 1
        samples = error_samples.setdefault(code, [])
        if len(samples) < 20:
            samples.append(sample)

    # 能力实算（§5.5）的逐 subset 累积器
    @dataclass
    class _Acc:
        durations: list[int] = field(default_factory=list)
        refs: list[str] = field(default_factory=list)
        speakers: set[str] = field(default_factory=set)
        segments_by_item: list[list[Segment]] = field(default_factory=list)
        itn_items: int = 0
        buckets: list[dict[str, object]] = field(default_factory=list)
        langs: set[str] = field(default_factory=set)

    acc: dict[str, _Acc] = {}

    def accumulate(subset: str, duration: int, ref: str, raw: RawItem) -> None:
        bucket = acc.setdefault(subset, _Acc())
        bucket.durations.append(duration)
        bucket.refs.append(ref)
        bucket.segments_by_item.append(raw.segments)
        if raw.itn_text:
            bucket.itn_items += 1
        if raw.speaker and raw.speaker.casefold() != "unknown":
            bucket.speakers.add(raw.speaker)
        bucket.speakers.update(
            str(segment["speaker"])
            for segment in raw.segments
            if segment["speaker"].strip() and segment["speaker"].casefold() != "unknown"
        )
        bucket.buckets.append(raw.buckets)
        bucket.langs.add(raw.lang)

    try:
        resolved_root = root.expanduser().resolve()
        # 逐批记录扫描数，让 SSE 在目录解析过程中也能推送可见进度（M1 §5.1、M7 §7）。
        raw_items: list[RawItem] = []
        for raw in importer.scan(root, limit):
            raw_items.append(raw)
            if len(raw_items) % IMPORT_SCAN_PROGRESS_INTERVAL == 0:
                set_phase("scanning", {"scanned": len(raw_items)})
        if len(raw_items) % IMPORT_SCAN_PROGRESS_INTERVAL:
            set_phase("scanning", {"scanned": len(raw_items)})

        # 扫描完成后取得准确总数，再分批并行指纹（M1 §5.1）。
        for raw in raw_items:
            original_subset = raw.subset
            raw.subset = (subset_mapping or {}).get(original_subset, original_subset)
            raw.audio_profile = (audio_profile_overrides or {}).get(
                raw.subset,
                (audio_profile_overrides or {}).get(original_subset, raw.audio_profile),
            )
        subset_names: list[str] = []
        for raw in raw_items:
            if raw.subset not in subset_names:
                subset_names.append(raw.subset)
        if not subset_names:
            subset_names = ["all"]
        subsets: dict[str, DatasetSubset] = {}
        # 断点续跑：复用已有 subset，避免唯一约束冲突（M1 §5.9）
        existing_subsets = {
            str(subset.name): subset
            for subset in db.scalars(
                select(DatasetSubset).where(DatasetSubset.dataset_version_id == record.id)
            ).all()
        }
        for index, name in enumerate(subset_names):
            if name in existing_subsets:
                subsets[name] = existing_subsets[name]
                continue
            profile = next((raw.audio_profile for raw in raw_items if raw.subset == name), "as_is")
            subset = DatasetSubset(
                dataset_version_id=record.id,
                name=name,
                audio_profile=profile,
                is_primary=index == 0,
            )
            db.add(subset)
            subsets[name] = subset
        db.flush()

        manifest_lines: list[tuple[str, str, str, str, int]] = []
        set_phase(
            "persisting",
            {
                "scanned": len(raw_items),
                "total": len(raw_items),
                "completed": 0,
                "persisted": imported,
                "errors": sum(error_counts.values()),
            },
        )
        # 断点续跑：重入时跳过该版本已落库的 item_key（M1 §5.9）
        existing_keys: set[str] = set()
        if duplicate is not None:
            existing_keys = set(
                db.scalars(select(Item.item_key).where(Item.dataset_version_id == record.id)).all()
            )
        for start in range(0, len(raw_items), batch_size):
            check_task_cancellation()
            chunk: list[RawItem] = []
            chunk_indexes: list[int] = []
            for row_index in range(start, min(start + batch_size, len(raw_items))):
                raw = raw_items[row_index]
                _validate_raw_paths(root, raw, resolved_root=resolved_root)
                missing_ref_allowed = allow_missing_ref and any(
                    issue.code == "E_REF_MISSING" for issue in raw.issues
                )
                blocking_issues = [
                    issue
                    for issue in raw.issues
                    if not (missing_ref_allowed and issue.code == "E_REF_MISSING")
                ]
                if raw.issues:
                    for issue in raw.issues:
                        note_error(issue.code, issue.detail)
                if blocking_issues:
                    failed_rows.add(row_index)
                    continue
                if not raw.ref_text.strip() and not missing_ref_allowed:
                    note_error("E_REF_EMPTY", str(raw.origin_ref or raw.audio))
                    failed_rows.add(row_index)
                    continue
                if not raw.audio.is_file():
                    note_error("E_AUDIO_MISSING", str(raw.audio))
                    failed_rows.add(row_index)
                    continue
                try:
                    chunk.append(_prepare_import_audio(raw))
                    chunk_indexes.append(row_index)
                except (OSError, subprocess.SubprocessError) as exc:
                    note_error("E_AUDIO_DECODE", f"{raw.audio}: {exc}")
                    failed_rows.add(row_index)
            if not chunk:
                set_phase(
                    "persisting",
                    {
                        "scanned": len(raw_items),
                        "total": len(raw_items),
                        "completed": min(start + batch_size, len(raw_items)),
                        "persisted": imported,
                        "errors": sum(error_counts.values()),
                    },
                )
                continue
            if fingerprint_pool is None:
                fingerprint_pool = ThreadPoolExecutor(max_workers=16)
            fingerprints = list(
                fingerprint_pool.map(lambda item: fingerprint_wav(item.audio), chunk)
            )

            check_task_cancellation()
            valid_chunk: list[RawItem] = []
            valid_fingerprints: list[AudioFingerprint] = []
            for row_index, raw, fp in zip(chunk_indexes, chunk, fingerprints, strict=True):
                if fp.duration_ms <= 0 or fp.sample_rate <= 0 or fp.channels <= 0:
                    note_error("E_AUDIO_DECODE", str(raw.origin_audio or raw.audio))
                    failed_rows.add(row_index)
                    continue
                if raw.audio_profile == "mono_ch0" and fp.channels < 2:
                    note_error("E_PROFILE_UNSUPPORTED", str(raw.audio))
                    failed_rows.add(row_index)
                    continue
                if raw.audio_profile == "as_is" and (fp.channels != 1 or fp.sample_rate != 16000):
                    note_error("E_PROFILE_UNSUPPORTED", str(raw.audio))
                    failed_rows.add(row_index)
                    continue
                if raw.item_key in seen_keys:
                    note_error("E_DUP_KEY", raw.item_key)
                    failed_rows.add(row_index)
                    continue
                seen_keys.add(raw.item_key)
                normalized = _normalize_segments(raw, fp.duration_ms, note_error)
                valid_chunk.append(normalized)
                valid_fingerprints.append(fp)
            if not valid_chunk:
                set_phase(
                    "persisting",
                    {
                        "scanned": len(raw_items),
                        "total": len(raw_items),
                        "completed": min(start + batch_size, len(raw_items)),
                        "persisted": imported,
                        "errors": sum(error_counts.values()),
                    },
                )
                continue
            chunk = valid_chunk
            fingerprints = valid_fingerprints
            assets = _ensure_assets(db, chunk, fingerprints)
            db.flush()
            pending: list[tuple[Item, RawItem, str | None]] = []
            for raw, fp in zip(chunk, fingerprints, strict=True):
                asset = assets[fp.sha256]
                # 断点续跑时也统计本次完整扫描结果，避免能力值只覆盖新增条目。
                accumulate(raw.subset, fp.duration_ms, raw.ref_text, raw)
                if raw.item_key in existing_keys:
                    # 断点续跑：已落库，静默跳过
                    continue
                ref_sha = (
                    hashlib.sha256(raw.ref_text.encode("utf-8")).hexdigest()[:16]
                    if raw.ref_text.strip()
                    else None
                )
                content_uid = hashlib.sha256(
                    f"{asset.sha256}:{ref_sha or 'no-reference'}".encode()
                ).hexdigest()[:24]
                item = Item(
                    dataset_version_id=record.id,
                    subset_id=subsets[raw.subset].id,
                    item_key=raw.item_key,
                    pair_key=raw.pair_key,
                    content_uid=content_uid,
                    audio_asset_id=asset.id,
                    split=raw.split,
                    duration_ms=fp.duration_ms,
                    lang=raw.lang,
                    speakers=_speaker_ids(raw),
                    buckets=raw.buckets,
                    ref_sha=ref_sha,
                )
                db.add(item)
                pending.append((item, raw, ref_sha))
                duration_total += fp.duration_ms
                manifest_lines.append(
                    (raw.item_key, asset.sha256, ref_sha or "", raw.subset, fp.duration_ms)
                )

            db.flush()
            for item, raw, ref_sha in pending:
                if ref_sha is not None:
                    db.add(
                        RefText(
                            item_id=item.id,
                            kind="verbatim",
                            lang=raw.lang,
                            text=raw.ref_text,
                            sha256=ref_sha,
                        )
                    )
                # 参考分段（M1 §4）：长音频/会议用于 DER/命名与 Badcase 泳道
                for seq, segment in enumerate(raw.segments):
                    db.add(
                        RefSegment(
                            item_id=item.id,
                            seq=seq,
                            speaker=(segment["speaker"] or "unknown")[:64],
                            start_ms=segment["start_ms"],
                            end_ms=segment["end_ms"],
                            text=segment["text"],
                            skip=segment.get("skip", False),
                            overlap=segment.get("overlap", False),
                        )
                    )
                if raw.speakers:
                    speaker_rows = [
                        _speaker_meta_row(record.id, speaker_id, info)
                        for speaker_id, info in raw.speakers.items()
                    ]
                    statement = pg_insert(SpeakerMeta).values(speaker_rows)
                    statement = statement.on_conflict_do_update(
                        index_elements=["dataset_version_id", "speaker_id"],
                        set_={
                            "gender": statement.excluded.gender,
                            "age": statement.excluded.age,
                            "accent": statement.excluded.accent,
                            "meta": statement.excluded.meta,
                        },
                    )
                    db.execute(statement)
                if raw.itn_text:
                    itn_sha = hashlib.sha256(raw.itn_text.encode("utf-8")).hexdigest()[:16]
                    db.add(
                        RefText(
                            item_id=item.id,
                            kind="itn",
                            lang=raw.lang,
                            text=raw.itn_text,
                            sha256=itn_sha,
                        )
                    )
            imported += len(pending)
            # set_phase 会将本批条目与进度同事务提交，避免额外一次空 commit。
            set_phase(
                "persisting",
                {
                    "scanned": len(raw_items),
                    "total": len(raw_items),
                    "completed": min(start + batch_size, len(raw_items)),
                    "persisted": imported,
                    "errors": sum(error_counts.values()),
                },
            )

        if fingerprint_pool is not None:
            fingerprint_pool.shutdown(wait=True)
            fingerprint_pool = None

        # 每 subset 独立算 capabilities（§5.5），版本级只存加权汇总
        set_phase(
            "analyzing",
            {
                "scanned": len(raw_items),
                "total": len(raw_items),
                "completed": len(raw_items),
                "persisted": imported,
                "errors": sum(error_counts.values()),
            },
        )
        subset_caps: dict[str, dict[str, object]] = {}
        for name, bucket in acc.items():
            subset_caps[name] = _compute_capabilities(
                durations=bucket.durations,
                refs=bucket.refs,
                speakers=bucket.speakers,
                segments_by_item=bucket.segments_by_item,
                itn_items=bucket.itn_items,
                buckets=bucket.buckets,
                langs=bucket.langs,
                dataset_type=dataset.source_type,
            )
        for name, subset in subsets.items():
            caps = subset_caps.get(name)
            if caps is not None:
                subset.capabilities = caps
                subset.stats = {
                    "decode_errors": error_counts.get("E_AUDIO_DECODE", 0),
                }
        record.capabilities = _aggregate_capabilities(subset_caps, dataset_type=dataset.source_type)
        check_task_cancellation()

        # Manifest（§5.7）：排序后 hash，保证同内容同 hash
        manifest_dir = get_settings().data_dir / "manifests" / str(dataset.id) / version_name
        manifest_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(manifest_lines)
        digest = hashlib.sha256()
        with gzip.open(manifest_dir / "manifest.jsonl.gz", "wt", encoding="utf-8") as gz:
            for item_key, audio_sha, ref_sha, subset_name, duration in ordered:
                line = json.dumps(
                    {
                        "item_key": item_key,
                        "audio_sha256": audio_sha,
                        "ref_sha256": ref_sha,
                        "subset": subset_name,
                        "duration_ms": duration,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                gz.write(line + "\n")
                digest.update(f"{item_key}|{audio_sha}|{ref_sha}\n".encode())
        manifest_hash = digest.hexdigest()[:64]
        meta = {
            "item_count": len(ordered),
            "manifest_hash": manifest_hash,
            "capabilities": record.capabilities,
            "created_at": datetime.now(UTC).isoformat(),
        }
        (manifest_dir / "manifest.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        record.manifest_key = str(manifest_dir / "manifest.meta.json")
        record.manifest_hash = manifest_hash

        check_task_cancellation()
        record.status = "ready"
        # 断点续跑时本轮 imported 只含新增条目；总数与时长从库中重算（M1 §5.9）
        total_count, total_duration = db.execute(
            select(func.count(), func.coalesce(func.sum(Item.duration_ms), 0)).where(
                Item.dataset_version_id == record.id
            )
        ).one()
        record.item_count = int(total_count)
        record.total_duration_ms = int(total_duration)
        # 失败阈值（M1 §5.1）：失败样本占比 > 1% → ready_with_errors，需人工确认。
        if len(raw_items) > 0 and len(failed_rows) / len(raw_items) > 0.01:
            record.status = "ready_with_errors"
        record.stats = {
            "imported": imported,
            "decode_errors": error_counts.get("E_AUDIO_DECODE", 0),
            "errors": {
                code: {"count": count, "samples": error_samples[code]}
                for code, count in sorted(error_counts.items())
            },
            "splits": _split_counts(db, record.id),
            "finished_at": datetime.now(UTC).isoformat(),
        }
        # 用 item 表汇总，避免再次扫描
        for _name, subset in subsets.items():
            count, total = db.execute(
                select(func.count(), func.coalesce(func.sum(Item.duration_ms), 0)).where(
                    Item.subset_id == subset.id
                )
            ).one()
            subset.item_count = int(count)
            subset.total_duration_ms = int(total)
        db.commit()
    except Exception as exc:
        if fingerprint_pool is not None:
            fingerprint_pool.shutdown(wait=True, cancel_futures=True)
        db.rollback()
        failed = db.get(DatasetVersion, record.id)
        if failed is not None:
            failed.status = "failed"
            failed.stats = {"error": f"{type(exc).__name__}: {exc}"}
            db.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError("E_IMPORT_FAILED", f"导入失败: {exc}", status_code=500) from exc

    db.refresh(record)
    return record


def delete_version(db: Session, version_id: Any, *, actor: str | None) -> None:
    """删除未被评测定义或活动任务引用的数据集版本（M1 §3.2、M7 §3.5）。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    benchmark_id = db.scalar(
        select(Benchmark.id).where(Benchmark.dataset_version_id == version.id).limit(1)
    )
    active_job_id = db.scalar(
        select(Job.id)
        .where(
            Job.ref_type == "dataset_version",
            Job.ref_id == str(version.id),
            Job.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if version.status == "importing" or benchmark_id is not None or active_job_id is not None:
        raise ApiError(
            "E_VERSION_IN_USE",
            "数据集版本仍被评测定义或任务引用，不能删除",
            status_code=409,
            detail={
                "benchmark_id": str(benchmark_id) if benchmark_id else None,
                "job_id": str(active_job_id) if active_job_id else None,
            },
        )

    manifest_dir = _manifest_dir_for_version(version)
    audit.record(
        db,
        "dataset.delete",
        actor=actor,
        resource=f"dataset_version:{version.id}",
        detail={"dataset_id": str(version.dataset_id), "version": version.version},
    )
    db.delete(version)
    db.commit()
    if manifest_dir is not None and manifest_dir.is_dir():
        shutil.rmtree(manifest_dir)


def delete_dataset(db: Session, dataset_id: Any, *, actor: str | None) -> None:
    """整组未被基准引用且没有活动任务时，删除数据集及其所有版本（M1 §3.2）。"""
    dataset = db.scalar(select(Dataset).where(Dataset.id == dataset_id).with_for_update())
    if dataset is None:
        raise ApiError("E_NOT_FOUND", "数据集不存在", status_code=404)

    versions = db.scalars(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.id)
        .with_for_update()
    ).all()
    version_ids = [version.id for version in versions]
    benchmark_id = (
        db.scalar(
            select(Benchmark.id).where(Benchmark.dataset_version_id.in_(version_ids)).limit(1)
        )
        if version_ids
        else None
    )
    active_dataset_job_id = db.scalar(
        select(Job.id)
        .where(
            Job.ref_type == "dataset",
            Job.ref_id == str(dataset.id),
            Job.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    active_version_job_id = (
        db.scalar(
            select(Job.id)
            .where(
                Job.ref_type == "dataset_version",
                Job.ref_id.in_([str(version_id) for version_id in version_ids]),
                Job.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if version_ids
        else None
    )
    importing_versions = [version for version in versions if version.status == "importing"]
    has_active_job = active_dataset_job_id is not None or active_version_job_id is not None
    if benchmark_id is not None or has_active_job or importing_versions:
        raise ApiError(
            "E_DATASET_IN_USE",
            "数据集被基准引用或存在活动任务，不能删除",
            status_code=409,
            detail={
                "benchmark_id": str(benchmark_id) if benchmark_id else None,
                "active_job_id": str(active_dataset_job_id or active_version_job_id)
                if active_dataset_job_id or active_version_job_id
                else None,
                "importing_version_ids": [str(version.id) for version in importing_versions],
            },
        )

    manifest_dirs = {
        manifest_dir
        for version in versions
        if (manifest_dir := _manifest_dir_for_version(version)) is not None
    }
    audit.record(
        db,
        "dataset.deleted",
        actor=actor,
        resource=f"dataset:{dataset.id}",
        detail={"name": dataset.name, "version_count": len(versions)},
    )
    db.delete(dataset)
    db.commit()
    for manifest_dir in manifest_dirs:
        if manifest_dir.is_dir():
            shutil.rmtree(manifest_dir)


def _manifest_dir_for_version(version: DatasetVersion) -> Path | None:
    """只返回 data/manifests 内的单版本目录，防止删除越界路径。"""
    if not version.manifest_key:
        return None
    manifests_root = (get_settings().data_dir / "manifests").resolve()
    candidate = Path(version.manifest_key).expanduser().resolve().parent
    if candidate != manifests_root and candidate.is_relative_to(manifests_root):
        return candidate
    return None


def recompute_version_capabilities(db: Session, version_id: Any) -> dict[str, object]:
    """从已落库样本重算版本与 subset 能力（M1 §3.2/§5.5）。"""
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ApiError("E_NOT_FOUND", "版本不存在", status_code=404)
    if version.status not in {"ready", "ready_with_errors"}:
        raise ApiError("E_CONFLICT", "只有已完成导入的版本可以重新分析", status_code=409)
    dataset = db.get(Dataset, version.dataset_id)
    subsets = db.scalars(
        select(DatasetSubset).where(DatasetSubset.dataset_version_id == version.id)
    ).all()
    subset_by_id = {subset.id: subset for subset in subsets}
    items = db.scalars(
        select(Item).where(Item.dataset_version_id == version.id).order_by(Item.id)
    ).all()
    item_ids = [item.id for item in items]
    refs_by_item: dict[int, dict[str, str]] = {}
    segments_by_item: dict[int, list[Segment]] = {}
    if item_ids:
        for ref in db.scalars(select(RefText).where(RefText.item_id.in_(item_ids))).all():
            refs_by_item.setdefault(ref.item_id, {})[ref.kind] = ref.text
        for segment in db.scalars(
            select(RefSegment)
            .where(RefSegment.item_id.in_(item_ids))
            .order_by(RefSegment.item_id, RefSegment.start_ms, RefSegment.seq)
        ).all():
            segments_by_item.setdefault(segment.item_id, []).append(
                Segment(
                    speaker=segment.speaker,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                    skip=segment.skip,
                    overlap=segment.overlap,
                )
            )

    @dataclass
    class _SavedAcc:
        durations: list[int] = field(default_factory=list)
        refs: list[str] = field(default_factory=list)
        speakers: set[str] = field(default_factory=set)
        segments_by_item: list[list[Segment]] = field(default_factory=list)
        itn_items: int = 0
        buckets: list[dict[str, object]] = field(default_factory=list)
        langs: set[str] = field(default_factory=set)

    accumulators = {subset.name: _SavedAcc() for subset in subsets}
    for item in items:
        subset = subset_by_id.get(item.subset_id)
        if subset is None:
            continue
        accumulator = accumulators[subset.name]
        ref_texts = refs_by_item.get(item.id, {})
        segments = segments_by_item.get(item.id, [])
        accumulator.durations.append(item.duration_ms)
        accumulator.refs.append(ref_texts.get("verbatim", ""))
        accumulator.itn_items += int(bool(ref_texts.get("itn") or ref_texts.get("itn_expected")))
        accumulator.segments_by_item.append(segments)
        accumulator.speakers.update(
            speaker for speaker in item.speakers if speaker and speaker.casefold() != "unknown"
        )
        accumulator.speakers.update(
            segment["speaker"]
            for segment in segments
            if segment["speaker"].strip() and segment["speaker"].casefold() != "unknown"
        )
        accumulator.buckets.append(item.buckets)
        if item.lang:
            accumulator.langs.add(item.lang)

    subset_capabilities: dict[str, dict[str, object]] = {}
    for subset in subsets:
        accumulator = accumulators[subset.name]
        capabilities = _compute_capabilities(
            durations=accumulator.durations,
            refs=accumulator.refs,
            speakers=accumulator.speakers,
            segments_by_item=accumulator.segments_by_item,
            itn_items=accumulator.itn_items,
            buckets=accumulator.buckets,
            langs=accumulator.langs,
            dataset_type=dataset.source_type if dataset else None,
        )
        subset.capabilities = capabilities
        subset.item_count = len(accumulator.durations)
        subset.total_duration_ms = sum(accumulator.durations)
        subset_capabilities[subset.name] = capabilities

    version.capabilities = _aggregate_capabilities(
        subset_capabilities,
        dataset_type=dataset.source_type if dataset else None,
    )
    version.item_count = len(items)
    version.total_duration_ms = sum(item.duration_ms for item in items)
    version.stats = {
        **(version.stats or {}),
        "reanalyzed_at": datetime.now(UTC).isoformat(),
    }
    return version.capabilities


def _next_version(db: Session, dataset_id: Any) -> str:
    """版本号只做自增，不解析语义。"""
    count = db.scalar(
        select(func.count())
        .select_from(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
    )
    return f"v{(count or 0) + 1}"


def _speaker_ids(raw: RawItem) -> list[str]:
    """汇总原始说话人、分段说话人与元数据键，避免多人样本只保存首位说话人。"""
    speaker_ids = set(raw.speakers)
    if raw.speaker and raw.speaker.casefold() != "unknown":
        speaker_ids.add(raw.speaker)
    speaker_ids.update(
        segment["speaker"]
        for segment in raw.segments
        if segment["speaker"].strip() and segment["speaker"].casefold() != "unknown"
    )
    return sorted(speaker_ids)


def _speaker_meta_row(version_id: Any, speaker_id: str, info: SpeakerInfo) -> dict[str, object]:
    """将导入器元数据映射到显式字段，其余来源字段保留在 JSONB。"""
    raw_meta = info.get("meta")
    meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    meta.update(
        {
            key: value
            for key, value in info.items()
            if key not in {"gender", "age", "accent", "meta"}
        }
    )
    return {
        "dataset_version_id": version_id,
        "speaker_id": speaker_id,
        "gender": info.get("gender"),
        "age": info.get("age"),
        "accent": info.get("accent"),
        "meta": meta,
    }


def _prepare_import_audio(raw: RawItem) -> RawItem:
    """把设计规定的 MP4 输入抽成受管 16kHz 单声道 WAV。"""
    if raw.audio.suffix.casefold() != ".mp4":
        return raw

    source_digest = hashlib.sha256()
    with raw.audio.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            source_digest.update(chunk)
    target_dir = get_settings().data_dir / "audio_imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source_digest.hexdigest()}.wav"
    if target.is_file() and target.stat().st_size >= 44:
        return replace(raw, audio=target, origin_audio=raw.origin_audio or raw.audio)

    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.wav")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise OSError("未安装 ffmpeg，无法抽取 MP4 音轨")
    try:
        subprocess.run(  # noqa: S603 - 可执行文件固定为 ffmpeg，输入路径已限制在白名单根目录内。
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw.audio),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if not temporary.is_file() or temporary.stat().st_size < 44:
            raise OSError("ffmpeg 没有生成有效 WAV")
        os.replace(temporary, target)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "ffmpeg 抽轨失败").strip()
        raise OSError(detail[:512]) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return replace(raw, audio=target, origin_audio=raw.origin_audio or raw.audio)


def _normalize_segments(
    raw: RawItem,
    duration_ms: int,
    note_error: Callable[[str, str], None],
) -> RawItem:
    """按 M1 §8 修正非法或越界分段，并把修正记入质量报告。"""
    normalized: list[Segment] = []
    for original in raw.segments:
        start = int(original["start_ms"])
        end = int(original["end_ms"])
        if start < 0:
            start = 0
            note_error("E_SEGMENT_INVALID", f"{raw.item_key}: start_ms 小于 0")
        if end <= start:
            end = start + 1
            note_error("E_SEGMENT_INVALID", f"{raw.item_key}: end_ms 不大于 start_ms")
        if end > duration_ms + 200:
            end = duration_ms
            note_error("E_SEGMENT_OVERFLOW", f"{raw.item_key}: 分段超出音频时长 200ms 以上")
        if end <= start:
            start = max(0, duration_ms - 1)
            end = duration_ms
            note_error("E_SEGMENT_INVALID", f"{raw.item_key}: 分段起点超出音频时长")
        segment: Segment = {
            "speaker": original["speaker"],
            "start_ms": start,
            "end_ms": end,
            "text": original["text"],
        }
        if "skip" in original:
            segment["skip"] = original["skip"]
        if "overlap" in original:
            segment["overlap"] = original["overlap"]
        normalized.append(segment)
    return replace(raw, segments=normalized)


def _ensure_assets(
    db: Session, chunk: list[RawItem], fingerprints: list[AudioFingerprint]
) -> dict[str, AudioAsset]:
    """按 sha 批量查/建 audio_asset；返回 sha -> asset。"""
    shas = {fp.sha256 for fp in fingerprints}
    assets = {
        asset.sha256: asset
        for asset in db.scalars(select(AudioAsset).where(AudioAsset.sha256.in_(shas)))
    }
    for raw, fp in zip(chunk, fingerprints, strict=True):
        if fp.sha256 in assets:
            continue
        asset = AudioAsset(
            sha256=fp.sha256,
            uri=str(raw.audio),
            bytes=fp.bytes,
            duration_ms=fp.duration_ms,
            sample_rate=fp.sample_rate,
            channels=fp.channels,
            codec=fp.codec,
            origin_path=str(raw.origin_audio or raw.audio),
        )
        db.add(asset)
        assets[fp.sha256] = asset
    return assets


def _split_counts(db: Session, version_id: Any) -> dict[str, int]:
    rows = db.execute(
        select(Item.split, func.count())
        .where(Item.dataset_version_id == version_id)
        .group_by(Item.split)
    ).all()
    return {str(split): int(count) for split, count in rows}


_PUNCT_ANY = set("，。？！、；：")
# 句末标点是标点可评性的真正依据（M1 §5.5 punct_sentence_ratio）
_PUNCT_SENTENCE_END = ("。", "！", "？")


def _compute_capabilities(
    *,
    durations: list[int],
    refs: list[str],
    speakers: set[str],
    segments_by_item: list[list[Segment]],
    itn_items: int,
    buckets: list[dict[str, object]],
    langs: set[str],
    dataset_type: str | None,
) -> dict[str, object]:
    """按 M1 §5.5 从参考文本与时长实算 subset 能力。

    为什么实算而不是按数据集硬编码：resolver/可用性门控（M3/M5）
    依赖的是"这份参考里到底有没有标点/分段/说话人"，而非数据集名义类型。
    """
    count = len(durations)
    sorted_durations = sorted(durations)
    segment_items = sum(bool(segments) for segments in segments_by_item)
    speaker_items = sum(
        any(
            segment["speaker"].strip() and segment["speaker"].casefold() != "unknown"
            for segment in segments
        )
        for segments in segments_by_item
    )
    overlap_ms, speaker_time_ms = _speech_overlap_durations(segments_by_item)

    def percentile(ratio: float) -> int:
        if not sorted_durations:
            return 0
        index = min(len(sorted_durations) - 1, int(len(sorted_durations) * ratio))
        return sorted_durations[index]

    punct_any = sum(1 for ref in refs if any(ch in ref for ch in _PUNCT_ANY))
    punct_sentence = sum(1 for ref in refs if ref.rstrip().endswith(_PUNCT_SENTENCE_END))
    text_count = sum(1 for ref in refs if ref.strip())
    punct_sentence_ratio = (punct_sentence / count) if count else 0.0
    bucket_counts: dict[str, dict[str, int]] = {}
    for item_buckets in buckets:
        for dim, value in (item_buckets or {}).items():
            if value is None:
                continue
            dim_counts = bucket_counts.setdefault(str(dim), {})
            dim_counts[str(value)] = dim_counts.get(str(value), 0) + 1
    return {
        "item_count": count,
        "total_duration_ms": sum(sorted_durations),
        "duration_p50_ms": percentile(0.50),
        "duration_p95_ms": percentile(0.95),
        "duration_max_ms": sorted_durations[-1] if sorted_durations else 0,
        "text_ratio": round(text_count / count, 4) if count else 0.0,
        "punct_any_ratio": round(punct_any / count, 4) if count else 0.0,
        "punct_sentence_ratio": round(punct_sentence_ratio, 4),
        "punct_supported": (
            dataset_type not in {"apy", "datatang", "magicdata"} and punct_sentence_ratio >= 0.8
        ),
        "itn_ratio": round(itn_items / count, 4) if count else 0.0,
        "segment_ratio": round(segment_items / count, 4) if count else 0.0,
        "speaker_ratio": round(speaker_items / count, 4) if count else 0.0,
        "overlap_ratio": round(overlap_ms / speaker_time_ms, 4) if speaker_time_ms else 0.0,
        "speaker_count": len(speakers),
        "langs": sorted(langs),
        "bucket_counts": bucket_counts,
    }


def _speech_overlap_durations(segments_by_item: list[list[Segment]]) -> tuple[int, int]:
    """以多说话人重叠区间并集除以说话人标注总时长（M1 §5.5）。"""
    overlap_total = 0
    speaker_time_total = 0
    for segments in segments_by_item:
        events: list[tuple[int, str, int]] = []
        for segment in segments:
            start = int(segment["start_ms"])
            end = int(segment["end_ms"])
            speaker = segment["speaker"].strip()
            if end <= start or not speaker or speaker.casefold() == "unknown":
                continue
            speaker_time_total += end - start
            events.append((start, speaker, 1))
            events.append((end, speaker, -1))
        events.sort(key=lambda event: event[0])
        active: dict[str, int] = {}
        previous_ms: int | None = None
        event_index = 0
        while event_index < len(events):
            current_ms = events[event_index][0]
            if previous_ms is not None and current_ms > previous_ms:
                active_speakers = sum(count > 0 for count in active.values())
                interval_ms = current_ms - previous_ms
                if active_speakers > 1:
                    overlap_total += interval_ms
            while event_index < len(events) and events[event_index][0] == current_ms:
                _, speaker, delta = events[event_index]
                active[speaker] = active.get(speaker, 0) + delta
                event_index += 1
            previous_ms = current_ms
    return overlap_total, speaker_time_total


_RATIO_KEYS = (
    "text_ratio",
    "punct_any_ratio",
    "punct_sentence_ratio",
    "itn_ratio",
    "segment_ratio",
    "speaker_ratio",
    "overlap_ratio",
)


def _aggregate_capabilities(
    subset_caps: dict[str, dict[str, object]],
    *,
    dataset_type: str | None,
) -> dict[str, object]:
    """版本级 capabilities：按条数加权汇总（M1 §5.6 规则 5），仅用于列表展示。"""
    items = [(int(cast("int", caps.get("item_count", 0))), caps) for caps in subset_caps.values()]
    total = sum(count for count, _ in items)
    if not total:
        return {"item_count": 0}
    aggregate: dict[str, object] = {"item_count": total}
    for key in _RATIO_KEYS:
        weighted = sum(float(cast("float", caps.get(key, 0.0))) * count for count, caps in items)
        aggregate[key] = round(weighted / total, 4)
    aggregate["punct_supported"] = (
        dataset_type not in {"apy", "datatang", "magicdata"}
        and float(aggregate["punct_sentence_ratio"]) >= 0.8  # type: ignore[arg-type]
    )
    aggregate["total_duration_ms"] = sum(
        int(cast("int", caps.get("total_duration_ms", 0))) for _, caps in items
    )
    speaker_total = 0
    langs: set[str] = set()
    merged_buckets: dict[str, dict[str, int]] = {}
    for _, caps in items:
        speaker_total += int(cast("int", caps.get("speaker_count", 0)))
        langs.update(cast("list[str]", caps.get("langs", [])))
        for dim, counts in cast("dict[str, dict[str, int]]", caps.get("bucket_counts", {})).items():
            target = merged_buckets.setdefault(dim, {})
            for value, count in counts.items():
                target[value] = target.get(value, 0) + count
    aggregate["speaker_count"] = speaker_total
    aggregate["langs"] = sorted(langs)
    aggregate["bucket_counts"] = merged_buckets
    return aggregate


def preview_import(
    db: Session,
    dataset: Dataset,
    *,
    root: Path,
    limit: int = 50,
    source_type: str | None = None,
    allow_missing_ref: bool = False,
    subset_mapping: Mapping[str, str] | None = None,
    audio_profile_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """导入前预览（M1 §5.2）：只解析不落库，返回前 limit 条与问题标记。

    目的：在写库前发现"配错列、路径错、编码错"。
    """
    started = perf_counter()
    detected_source_type, importer = resolve_importer(dataset, root, source_type=source_type)
    items: list[dict[str, Any]] = []
    issues: dict[str, int] = {}
    raw_items = list(importer.scan(root, limit=limit))
    original_subsets = {raw.subset for raw in raw_items}
    unknown_mappings = set(subset_mapping or {}) - original_subsets
    if unknown_mappings:
        raise ApiError("E_VALIDATION", "Subset 映射包含预览中不存在的来源", 422)
    source_subsets = [raw.subset for raw in raw_items]
    for raw in raw_items:
        original_subset = raw.subset
        raw.subset = (subset_mapping or {}).get(original_subset, original_subset)
        raw.audio_profile = (audio_profile_overrides or {}).get(
            raw.subset,
            (audio_profile_overrides or {}).get(original_subset, raw.audio_profile),
        )

    def record_issue(item_issues: list[str], code: str) -> None:
        item_issues.append(code)
        issues[code] = issues.get(code, 0) + 1

    prepared: list[RawItem | None] = []
    preparation_issues: list[list[str]] = []
    for raw in raw_items:
        item_issues: list[str] = []
        _validate_raw_paths(root, raw)
        missing_ref_allowed = allow_missing_ref and any(
            issue.code == "E_REF_MISSING" for issue in raw.issues
        )
        blocking_issues = [
            issue
            for issue in raw.issues
            if not (missing_ref_allowed and issue.code == "E_REF_MISSING")
        ]
        for issue in raw.issues:
            record_issue(item_issues, issue.code)
        if blocking_issues:
            prepared.append(None)
        elif not raw.ref_text.strip() and not missing_ref_allowed:
            record_issue(item_issues, "E_REF_EMPTY")
            prepared.append(None)
        elif not raw.audio.is_file():
            record_issue(item_issues, "E_AUDIO_MISSING")
            prepared.append(None)
        else:
            try:
                prepared.append(_prepare_import_audio(raw))
            except (OSError, subprocess.SubprocessError):
                record_issue(item_issues, "E_AUDIO_DECODE")
                prepared.append(None)
        preparation_issues.append(item_issues)

    valid_positions = [index for index, raw in enumerate(prepared) if raw is not None]
    valid_raws = [prepared[index] for index in valid_positions]

    def preview_audio(item: RawItem) -> AudioFingerprint | AudioMetadata:
        """WAV 预览只读头信息；其他已准备格式沿用完整指纹路径。"""
        if item.audio.suffix.casefold() == ".wav":
            return read_wav_metadata(item.audio)
        return fingerprint_wav(item.audio)

    with ThreadPoolExecutor(max_workers=max(1, min(16, len(valid_raws)))) as pool:
        fingerprints = list(pool.map(preview_audio, valid_raws)) if valid_raws else []
    fingerprint_by_position = dict(zip(valid_positions, fingerprints, strict=True))

    issue_samples: list[dict[str, str]] = []
    valid_count = 0
    total_duration_ms = 0
    total_audio_bytes = 0
    subset_stats: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_items):
        source_audio = raw.origin_audio or raw.audio
        item_issues = preparation_issues[index]
        fp = fingerprint_by_position.get(index)
        duration_ms = 0
        if fp is not None:
            duration_ms = fp.duration_ms
            if fp.duration_ms <= 0 or fp.sample_rate <= 0 or fp.channels <= 0:
                record_issue(item_issues, "E_AUDIO_DECODE")
            elif (raw.audio_profile == "mono_ch0" and fp.channels < 2) or (
                raw.audio_profile == "as_is" and (fp.channels != 1 or fp.sample_rate != 16000)
            ):
                record_issue(item_issues, "E_PROFILE_UNSUPPORTED")
            else:

                def record_segment_issue(
                    code: str, _detail: str, target: list[str] = item_issues
                ) -> None:
                    record_issue(target, code)

                normalized = _normalize_segments(
                    raw,
                    fp.duration_ms,
                    record_segment_issue,
                )
                raw = normalized
                valid_count += 1
                total_duration_ms += fp.duration_ms
                with suppress(OSError):
                    total_audio_bytes += source_audio.stat().st_size
        if item_issues and len(issue_samples) < 20:
            issue_samples.extend(
                {
                    "item_key": raw.item_key,
                    "code": code,
                    "detail": str(raw.origin_ref or raw.origin_audio or raw.audio),
                }
                for code in item_issues
            )
            issue_samples = issue_samples[:20]
        source_subset = source_subsets[index]
        subset = subset_stats.setdefault(
            source_subset,
            {
                "name": source_subset,
                "mapped_name": raw.subset,
                "count": 0,
                "audio_profile": raw.audio_profile,
                "channels": [],
                "sample_rates": [],
            },
        )
        subset["count"] = int(subset["count"]) + 1
        if fp is not None:
            channels = cast("list[int]", subset["channels"])
            if fp.channels not in channels:
                channels.append(fp.channels)
            sample_rates = cast("list[int]", subset["sample_rates"])
            if fp.sample_rate not in sample_rates:
                sample_rates.append(fp.sample_rate)
        items.append(
            {
                "item_key": raw.item_key,
                "duration_ms": duration_ms,
                "ref": raw.ref_text[:80],
                "speaker": raw.speaker,
                "subset": raw.subset,
                "source_subset": source_subset,
                "audio_profile": raw.audio_profile,
                "channels": fp.channels if fp is not None else None,
                "sample_rate": fp.sample_rate if fp is not None else None,
                "buckets": raw.buckets,
                "issues": item_issues,
            }
        )
    return {
        "source_type": detected_source_type,
        "total_scanned": len(items),
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "valid_count": valid_count,
        "total_duration_ms": total_duration_ms,
        "total_audio_bytes": total_audio_bytes,
        "issues": issues,
        "issue_samples": issue_samples,
        "subsets": list(subset_stats.values()),
        "items": items,
    }


def version_diff(
    db: Session,
    version: DatasetVersion,
    against: DatasetVersion,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """版本 diff（M1 §5.8、M8 §9.8）：按 item_key 对齐，计数完整，明细分页。"""
    from app.domain.models import AudioAsset

    def snapshot(version_id: Any) -> dict[str, dict[str, Any]]:
        rows = db.execute(
            select(Item.item_key, Item.ref_sha, AudioAsset.sha256, Item.buckets)
            .join(AudioAsset, AudioAsset.id == Item.audio_asset_id)
            .where(Item.dataset_version_id == version_id)
        ).all()
        return {
            str(key): {
                "ref_sha": str(ref_sha),
                "audio_sha": str(sha),
                "buckets": dict(buckets or {}),
            }
            for key, ref_sha, sha, buckets in rows
        }

    a = snapshot(version.id)
    b = snapshot(against.id)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    common = set(a) & set(b)
    text_changed = sorted(k for k in common if a[k]["ref_sha"] != b[k]["ref_sha"])
    audio_changed = sorted(k for k in common if a[k]["audio_sha"] != b[k]["audio_sha"])
    bucket_changed = sorted(
        k
        for k in common
        if k not in text_changed and k not in audio_changed and a[k]["buckets"] != b[k]["buckets"]
    )

    def detail(keys: list[str], kind: str) -> list[dict[str, Any]]:
        return [{"item_key": key, "change": kind} for key in keys]

    all_details = (
        detail(added, "added")
        + detail(removed, "removed")
        + detail(text_changed, "text_changed")
        + detail(audio_changed, "audio_changed")
        + detail(bucket_changed, "bucket_changed")
    )
    page_end = min(offset + limit, len(all_details))

    return {
        "base_version_id": str(version.id),
        "against_version_id": str(against.id),
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "text_changed": len(text_changed),
            "audio_changed": len(audio_changed),
            "bucket_changed": len(bucket_changed),
        },
        "offset": offset,
        "total_details": len(all_details),
        "next_offset": page_end if page_end < len(all_details) else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "details": all_details[offset:page_end],
    }
