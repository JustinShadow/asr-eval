"""导入预览与版本 diff 集成测试（M1 §5.2/§5.8）。

需要 PostgreSQL（asr_eval_test）；数据库不可用时整组跳过。
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.datasets import import_dataset, preview_import, version_diff
from app.domain.models import Base, Dataset

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


def _apy_tree(tmp_path: Path, extra: bool = False) -> Path:
    import wave

    sessions = ["001_0001", "001_0002"] + (["001_0003"] if extra else [])
    for index, ses in enumerate(sessions):
        session_dir = tmp_path / "data" / "category" / "2" / ses
        session_dir.mkdir(parents=True, exist_ok=True)
        audio = session_dir / "ast_phone.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000 * (index + 1))
        # extra 版本把第 2 条的参考文本改掉，用于 diff text_changed
        text = f"再见{index}。" if extra and index == 1 else f"你好{index}。"
        (session_dir / "ast_phone.txt").write_text(f"0.0\t1.0\tO1\t{text}\n", encoding="utf-8")
        (session_dir / "ast_phone.metadata").write_text(
            f"SES\t{ses}\nACT\tCantonese\nACC\tGuangZhou;GuangZhou\nSAM\t16000\nNCH\t1\n",
            encoding="utf-8",
        )
    return tmp_path


def test_preview_does_not_persist(db: Session, tmp_path: Path) -> None:
    """预览只解析不落库（M1 §5.2）。"""
    suffix = f"pv{abs(hash(str(tmp_path))) % 10**10:010d}"
    dataset = Dataset(name=f"ds-{suffix}", source_type="apy")
    db.add(dataset)
    db.commit()
    root = _apy_tree(tmp_path)

    from app.api.datasets import ImportRequest, preview_import_endpoint

    body = ImportRequest(root=str(root), source_type="auto")
    result = preview_import_endpoint(dataset_id=dataset.id, body=body, db=db, _=None)
    assert result["total_scanned"] == 2
    assert result["source_type"] == "apy"
    assert result["valid_count"] == 2
    assert result["elapsed_ms"] >= 0
    assert result["items"][0]["item_key"] == "001_0001/ast/phone"
    assert result["items"][0]["issues"] == []
    # 不落库：该数据集不应有任何版本与条目
    from sqlalchemy import func, select

    from app.domain.models import DatasetVersion

    version_count = db.scalar(
        select(func.count())
        .select_from(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset.id)
    )
    assert version_count == 0


def test_preview_reads_wav_header_without_hashing_audio(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大 WAV 的 M1 §5.2 预览只读容器头，避免预览完整扫描音频字节。"""
    dataset = Dataset(name=f"轻量预览-{uuid.uuid4().hex[:10]}", source_type="apy")
    db.add(dataset)
    db.commit()
    root = _apy_tree(tmp_path)

    def fail_fingerprint(_path: Path) -> None:
        pytest.fail("WAV 预览不应计算完整音频 SHA-256")

    monkeypatch.setattr("app.domain.datasets.fingerprint_wav", fail_fingerprint)
    result = preview_import(db, dataset, root=root, source_type="apy")

    assert result["valid_count"] == 2
    assert result["total_audio_bytes"] > 0
    assert result["items"][0]["duration_ms"] == 1000
    assert result["items"][0]["sample_rate"] == 16000
    assert result["items"][0]["channels"] == 1


def test_preview_reports_source_and_mapped_subset_capabilities(db: Session, tmp_path: Path) -> None:
    """自动识别与 subset 映射返回来源、目标名、声道和采样率。"""
    dataset = Dataset(name=f"映射预览-{uuid.uuid4().hex[:10]}", source_type=None)
    db.add(dataset)
    db.commit()
    root = _apy_tree(tmp_path)

    result = preview_import(
        db,
        dataset,
        root=root,
        source_type="auto",
        subset_mapping={"phone": "near"},
        audio_profile_overrides={"phone": "as_is"},
    )

    assert result["source_type"] == "apy"
    assert result["subsets"] == [
        {
            "name": "phone",
            "mapped_name": "near",
            "count": 2,
            "audio_profile": "as_is",
            "channels": [1],
            "sample_rates": [16000],
        }
    ]
    assert result["items"][0]["subset"] == "near"
    assert result["items"][0]["source_subset"] == "phone"


def test_version_diff_counts_and_details(db: Session, tmp_path: Path) -> None:
    """diff：added/text_changed 计数与明细（M1 §5.8）。"""
    suffix = f"df{abs(hash(str(tmp_path))) % 10**10:010d}"
    dataset = Dataset(name=f"ds-{suffix}", source_type="apy")
    db.add(dataset)
    db.commit()

    root_v1 = _apy_tree(tmp_path)
    v1 = import_dataset(db, dataset, root=root_v1, limit=100, version="v1", created_by="t")
    root_v2 = _apy_tree(tmp_path / "v2", extra=True)
    v2 = import_dataset(db, dataset, root=root_v2, limit=100, version="v2", created_by="t")

    result = version_diff(db, v1, v2)
    assert result["counts"]["added"] == 1  # 001_0003 新增
    assert result["counts"]["removed"] == 0
    # 001_0002 参考文本变了 → text_changed
    assert result["counts"]["text_changed"] == 1
    changes = {d["item_key"] for d in result["details"] if d["change"] == "text_changed"}
    assert len(changes) == 1

    from app.api.datasets import diff_version

    first_page = diff_version(
        version_id=v2.id,
        against=v1.id,
        limit=1,
        cursor=None,
        db=db,
        _=None,
    )
    assert first_page["counts"]["added"] == 1
    assert len(first_page["details"]) == 1
    assert first_page["details"][0]["change"] == "added"
    assert first_page["offset"] == 0
    assert first_page["total_details"] == 2
    assert first_page["next_cursor"] is not None
    assert first_page["previous_cursor"] is None

    second_page = diff_version(
        version_id=v2.id,
        against=v1.id,
        limit=1,
        cursor=first_page["next_cursor"],
        db=db,
        _=None,
    )
    assert second_page["details"][0]["change"] == "text_changed"
    assert second_page["previous_cursor"] is not None
    assert second_page["next_cursor"] is None

    from app.core.errors import ApiError

    with pytest.raises(ApiError) as exc_info:
        diff_version(
            version_id=v2.id,
            against=v1.id,
            limit=1,
            cursor="invalid",
            db=db,
            _=None,
        )
    assert exc_info.value.code == "E_VALIDATION"
