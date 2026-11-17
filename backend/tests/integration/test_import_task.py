"""异步导入任务集成测试（M1 §5.9，D-M7-6）。

需要 PostgreSQL（asr_eval_test）；数据库不可用时整组跳过。
"""

import os
import shutil
import subprocess
import uuid
import wave
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.domain import datasets as datasets_domain
from app.domain.imports import job_dict, start_import, wait_for_tasks
from app.domain.models import (
    AudioAsset,
    Base,
    Dataset,
    DatasetVersion,
    Item,
    Job,
    RefSegment,
    RefText,
    SpeakerMeta,
)
from app.importers.base import ImportIssue, RawItem
from app.importers.srt import SrtImporter

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _apy_tree(tmp_path: Path) -> Path:
    """构造一个 2 条 APY phone 语料树。"""
    import wave

    for index, ses in enumerate(["001_0001", "001_0002"]):
        session_dir = tmp_path / "data" / "category" / "2" / ses
        session_dir.mkdir(parents=True)
        audio = session_dir / "ast_phone.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000 * (index + 1))
        (session_dir / "ast_phone.txt").write_text(
            f"0.0\t1.0\tO1\t你好{index}。\n", encoding="utf-8"
        )
        (session_dir / "ast_phone.metadata").write_text(
            f"SES\t{ses}\nACT\tCantonese\nACC\tGuangZhou;GuangZhou\n"
            "SCD\tO1\nSEX\tO1_Female\nAGE\t30\nSAM\t16000\nNCH\t1\nMIT\tphone\n",
            encoding="utf-8",
        )
    return tmp_path


def test_async_import_creates_task_and_ready_version(
    client: TestClient, db: Session, tmp_path: Path
) -> None:
    suffix = f"ai{abs(hash(str(tmp_path))) % 10**10:010d}"
    dataset = Dataset(name=f"ds-{suffix}", source_type="apy")
    db.add(dataset)
    db.commit()

    root = _apy_tree(tmp_path)
    task = start_import(db, dataset, root=root, limit=100, version="v1", created_by="tester")
    wait_for_tasks(timeout_s=60)

    db.expire_all()
    loaded = db.get(Job, task.id)
    assert loaded is not None
    assert loaded.kind == "import"
    assert loaded.status == "succeeded"
    assert loaded.ref_type == "dataset_version"
    assert loaded.result is not None
    assert loaded.result["status"] == "ready"
    assert loaded.result["item_count"] == 2
    payload = job_dict(loaded)
    assert payload["status"] == "succeeded"
    assert payload["ref"]["type"] == "dataset_version"
    progress = payload["progress"]
    # M1 §5.1：扫描/落库阶段都要给出可解释的分母，完成后保留最终计数。
    assert progress["phase"] == "done"
    assert progress["scanned"] == 2
    assert progress["total"] == 2
    assert progress["completed"] == 2
    assert progress["persisted"] == 2
    assert progress["errors"] == 0
    assert "eta_ms" in progress

    version = db.get(DatasetVersion, uuid.UUID(str(loaded.result["resource_id"])))
    assert version is not None
    assert version.status == "ready"
    assert version.manifest_hash is not None
    item = db.scalar(select(Item).where(Item.dataset_version_id == version.id))
    assert item is not None
    assert item.speakers == ["O1"]
    segment = db.scalar(select(RefSegment).where(RefSegment.item_id == item.id))
    assert segment is not None and (segment.start_ms, segment.end_ms) == (0, 1000)
    speaker = db.get(SpeakerMeta, (version.id, "O1"))
    assert speaker is not None
    assert (speaker.gender, speaker.age, speaker.accent) == ("F", "30", "GuangZhou")


def test_resume_skips_existing_items(client: TestClient, db: Session, tmp_path: Path) -> None:
    """断点续跑：同版本重入跳过已落库 item_key（M1 §5.9）。"""
    suffix = f"rs{abs(hash(str(tmp_path))) % 10**10:010d}"
    dataset = Dataset(name=f"ds-{suffix}", source_type="apy")
    db.add(dataset)
    db.commit()

    root = _apy_tree(tmp_path)
    # 第一次：正常导入
    first = start_import(db, dataset, root=root, limit=100, version="v1", created_by="t")
    wait_for_tasks(timeout_s=60)

    # 模拟中断：把版本置回 importing
    db.expire_all()
    task = db.get(Job, first.id)
    version = db.get(DatasetVersion, uuid.UUID(str(task.result["resource_id"])))
    version.status = "importing"
    db.commit()

    second = start_import(db, dataset, root=root, limit=100, version="v1", created_by="t")
    wait_for_tasks(timeout_s=60)
    db.expire_all()
    done = db.get(Job, second.id)
    assert done is not None
    assert done.status == "succeeded"
    version = db.get(DatasetVersion, uuid.UUID(str(done.result["resource_id"])))
    assert version is not None
    # 没有重复条目：仍是 2 条
    assert version.item_count == 2


def test_path_escape_rejected(
    client: TestClient, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E_PATH_ESCAPE：根目录不在白名单 → 422（M1 §8）。"""
    monkeypatch.setenv("IMPORT_ALLOWED_ROOTS", '["/data/imports"]')
    get_settings.cache_clear()
    try:
        suffix = f"pe{abs(hash(str(tmp_path))) % 10**10:010d}"
        dataset = Dataset(name=f"ds-{suffix}", source_type="apy")
        db.add(dataset)
        db.commit()
        root = _apy_tree(tmp_path)
        from app.core.errors import ApiError

        with pytest.raises(ApiError) as exc_info:
            start_import(db, dataset, root=root, limit=10, version="v1", created_by="t")
        assert exc_info.value.code == "E_PATH_ESCAPE"
    finally:
        get_settings.cache_clear()


def test_hakka_txt_srt_import_extracts_managed_audio(
    client: TestClient,
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 §6：带 SRT 时码的 TXT 与 MP4 配对，音频抽为受管 16k 单声道 WAV。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("需要 ffmpeg 执行 MP4 抽轨验收")
    root = tmp_path / "客家话"
    root.mkdir()
    audio_path = root / "客家话样例.mp4"
    subprocess.run(  # noqa: S603 - 只调用本机 ffmpeg，以固定 lavfi 音调生成临时测试媒体。
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "aac",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )
    (root / "客家话样例.txt").write_text(
        "1\n00:00:00,200 --> 00:00:00,900\n第一句\n\n2\n00:00:01,000 --> 00:00:01,800\n第二句\n",
        encoding="utf-8",
    )
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=managed_dir, import_allowed_roots=[]),
    )
    dataset = Dataset(name=f"hakka-{uuid.uuid4().hex[:10]}", source_type="srt")
    db.add(dataset)
    db.commit()

    task = start_import(db, dataset, root=root, limit=10, version="v1", created_by="tester")
    wait_for_tasks(timeout_s=60)
    db.expire_all()
    loaded = db.get(Job, task.id)

    assert loaded is not None and loaded.status == "succeeded"
    assert loaded.result is not None and loaded.result["status"] == "ready"
    version = db.get(DatasetVersion, uuid.UUID(str(loaded.result["resource_id"])))
    assert version is not None
    assert version.capabilities["langs"] == ["hak"]
    item = db.scalar(select(Item).where(Item.dataset_version_id == version.id))
    assert item is not None
    asset = db.get(AudioAsset, item.audio_asset_id)
    assert asset is not None
    assert asset.uri.startswith(str(managed_dir / "audio_imports"))
    assert Path(asset.uri).is_file()
    assert asset.origin_path == str(audio_path)
    assert (asset.sample_rate, asset.channels) == (16000, 1)
    reference = db.get(RefText, (item.id, "verbatim"))
    assert reference is not None and reference.text == "第一句第二句"
    segments = db.scalars(
        select(RefSegment).where(RefSegment.item_id == item.id).order_by(RefSegment.seq)
    ).all()
    assert [(segment.start_ms, segment.end_ms) for segment in segments] == [
        (200, 900),
        (1000, 1800),
    ]


def test_import_can_keep_audio_without_reference_text(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 M1 §8 开启选项后保留无字幕音频，且不伪造参考文本。"""
    root = tmp_path / "只含音频"
    root.mkdir()
    media = root / "sample.mp4"
    media.touch()
    audio = root / "sample.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=managed_dir, import_allowed_roots=[]),
    )
    monkeypatch.setattr(
        datasets_domain,
        "_prepare_import_audio",
        lambda raw: replace(raw, audio=audio, origin_audio=raw.audio),
    )
    dataset = Dataset(name=f"无参考-{uuid.uuid4().hex[:10]}", source_type="srt")
    db.add(dataset)
    db.commit()

    version = datasets_domain._run_import(
        db,
        dataset,
        root=root,
        limit=10,
        version="v1",
        created_by="tester",
        importer=SrtImporter(),
        allow_missing_ref=True,
    )

    item = db.scalar(select(Item).where(Item.dataset_version_id == version.id))
    assert version.status == "ready"
    assert version.item_count == 1
    assert item is not None and item.ref_sha is None
    assert db.scalars(select(RefText).where(RefText.item_id == item.id)).all() == []


def test_import_skips_audio_without_reference_by_default(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未启用保留选项时，无字幕音频仍按 M1 §8 默认跳过。"""
    root = tmp_path / "默认跳过"
    root.mkdir()
    (root / "sample.mp4").touch()
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=managed_dir, import_allowed_roots=[]),
    )
    monkeypatch.setattr(
        datasets_domain,
        "_prepare_import_audio",
        lambda raw: replace(raw, audio=root / "missing.wav", origin_audio=raw.audio),
    )
    dataset = Dataset(name=f"默认跳过-{uuid.uuid4().hex[:10]}", source_type="srt")
    db.add(dataset)
    db.commit()

    version = datasets_domain._run_import(
        db,
        dataset,
        root=root,
        limit=10,
        version="v1",
        created_by="tester",
        importer=SrtImporter(),
    )

    assert version.item_count == 0
    assert version.status == "ready_with_errors"


def test_unreadable_hakka_audio_is_skipped_and_reported(
    client: TestClient,
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 §8：无法抽轨的媒体跳过入库，并进入逐错误码报告。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg 执行 MP4 解码失败验收")
    root = tmp_path / "坏样例"
    root.mkdir()
    (root / "坏样例.mp4").write_bytes(b"not an mp4")
    (root / "坏样例.txt").write_text("字幕", encoding="utf-8")
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=managed_dir, import_allowed_roots=[]),
    )
    dataset = Dataset(name=f"hakka-bad-{uuid.uuid4().hex[:10]}", source_type="srt")
    db.add(dataset)
    db.commit()

    task = start_import(db, dataset, root=root, limit=10, version="v1", created_by="tester")
    wait_for_tasks(timeout_s=60)
    db.expire_all()
    loaded = db.get(Job, task.id)

    assert loaded is not None and loaded.status == "succeeded"
    assert loaded.result is not None and loaded.result["status"] == "ready_with_errors"
    version = db.get(DatasetVersion, uuid.UUID(str(loaded.result["resource_id"])))
    assert version is not None and version.item_count == 0
    assert version.stats["errors"]["E_AUDIO_DECODE"]["count"] == 1


def test_ten_thousand_item_import_under_thirty_seconds(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 §10：一万条元数据导入在本机 PostgreSQL 环境低于 30 秒。"""
    root = tmp_path / "generated"
    root.mkdir()
    audio = root / "sample.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    managed_dir = tmp_path / "managed"
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=managed_dir, import_allowed_roots=[]),
    )
    dataset = Dataset(name=f"import-perf-{uuid.uuid4().hex[:10]}", source_type="synthetic")
    db.add(dataset)
    db.commit()

    class GeneratedImporter:
        """复用同一短 WAV，测数据库、能力统计与 manifest 导入路径。"""

        name = "generated"

        def detect(self, _root: Path) -> bool:
            return True

        def scan(self, _root: Path, limit: int) -> Iterator[RawItem]:
            for index in range(min(limit, 10_000)):
                yield RawItem(
                    item_key=f"sample-{index:05d}",
                    audio=audio,
                    ref_text=f"测试文本{index}",
                    split="test",
                    speaker="speaker-1",
                    duration_ms=0,
                    sample_rate=0,
                    channels=0,
                )

    started = perf_counter()
    version = datasets_domain._run_import(
        db,
        dataset,
        root=root,
        limit=10_000,
        version="v1",
        created_by="tester",
        importer=GeneratedImporter(),
    )
    elapsed_s = perf_counter() - started

    assert version.item_count == 10_000
    assert version.status == "ready"
    assert elapsed_s < 30, f"一万条导入耗时 {elapsed_s:.2f}s，超过 30s"


def test_import_error_threshold_counts_failed_items_not_warning_codes(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 §5.1/§8：失败率按样本计数，修复型分段告警不算失败。"""
    root = tmp_path / "threshold"
    root.mkdir()
    audio = root / "sample.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    monkeypatch.setattr(
        datasets_domain,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path / "managed", import_allowed_roots=[]),
    )
    dataset = Dataset(name=f"threshold-{uuid.uuid4().hex[:10]}", source_type="synthetic")
    db.add(dataset)
    db.commit()

    class ThresholdImporter:
        """一百条中包含一条双错误记录和一条可修复的越界分段。"""

        name = "threshold"

        def detect(self, _root: Path) -> bool:
            return True

        def scan(self, _root: Path, limit: int) -> Iterator[RawItem]:
            for index in range(min(limit, 100)):
                yield RawItem(
                    item_key=f"sample-{index:03d}",
                    audio=audio if index else root / "missing.wav",
                    ref_text=f"文本{index}",
                    split="test",
                    speaker="speaker-1",
                    duration_ms=0,
                    sample_rate=0,
                    channels=0,
                    segments=(
                        [{"speaker": "speaker-1", "start_ms": 0, "end_ms": 500, "text": "文本"}]
                        if index == 1
                        else []
                    ),
                    issues=(
                        [
                            ImportIssue("E_AUDIO_MISSING", "缺失音频"),
                            ImportIssue("E_REF_MISSING", "缺失参考"),
                        ]
                        if index == 0
                        else []
                    ),
                )

    version = datasets_domain._run_import(
        db,
        dataset,
        root=root,
        limit=100,
        version="v1",
        created_by="tester",
        importer=ThresholdImporter(),
    )

    assert version.item_count == 99
    assert version.status == "ready"
    assert version.stats["errors"]["E_AUDIO_MISSING"]["count"] == 1
    assert version.stats["errors"]["E_REF_MISSING"]["count"] == 1
    assert version.stats["errors"]["E_SEGMENT_OVERFLOW"]["count"] == 1
