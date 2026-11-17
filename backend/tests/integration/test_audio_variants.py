"""音频 profile 懒生成、缓存及样本音频接口测试（M1 §5.4）。"""

import io
import os
import shutil
import struct
import uuid
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain import audio_variants
from app.domain.audio_variants import resolve_audio_path
from app.domain.models import (
    AudioAsset,
    AudioVariant,
    Base,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Item,
)
from app.importers.audio import fingerprint_wav

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://asr:asr@127.0.0.1:5432/asr_eval_test",
)
pytestmark = pytest.mark.integration


@pytest.fixture()
def db() -> Iterator[Session]:
    """使用集成测试 PostgreSQL 准备音频资产。"""
    engine = create_engine(TEST_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _stereo_wav(path: Path, left_sample: int = 1000) -> None:
    """生成 44.1kHz 双声道测试音频，左右声道内容不同。"""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<hh", left_sample, -500) * 44100)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="该模块需要 ffmpeg")
def test_audio_variants_are_mono_cached_by_canonical_profile(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "stereo.wav"
    _stereo_wav(source, left_sample=uuid.uuid4().int % 30000 + 1)
    source_fingerprint = fingerprint_wav(source)
    asset = AudioAsset(
        sha256=source_fingerprint.sha256,
        uri=str(source),
        bytes=source_fingerprint.bytes,
        duration_ms=source_fingerprint.duration_ms,
        sample_rate=44100,
        channels=2,
        codec="pcm_s16le",
    )
    db.add(asset)
    db.commit()
    monkeypatch.setattr(
        audio_variants,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path / "cache"),
    )

    mixed = resolve_audio_path(db, asset, "mono_mix")
    channel_zero = resolve_audio_path(db, asset, "mono_ch0")
    db.commit()

    mixed_fingerprint = fingerprint_wav(mixed)
    channel_fingerprint = fingerprint_wav(channel_zero)
    assert (mixed_fingerprint.sample_rate, mixed_fingerprint.channels) == (16000, 1)
    assert (channel_fingerprint.sample_rate, channel_fingerprint.channels) == (16000, 1)
    assert mixed_fingerprint.sha256 != channel_fingerprint.sha256
    assert db.get(AudioVariant, (asset.id, "16k_mono")) is not None
    assert db.get(AudioVariant, (asset.id, "16k_mono_ch0")) is not None
    assert resolve_audio_path(db, asset, "16k_mono") == mixed


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="该模块需要 ffmpeg")
def test_item_audio_defaults_to_subset_profile(
    client: TestClient,
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mic.wav"
    _stereo_wav(source, left_sample=uuid.uuid4().int % 30000 + 1)
    fingerprint = fingerprint_wav(source)
    dataset = Dataset(name=f"音频接口-{uuid.uuid4().hex[:8]}", source_type="apy")
    db.add(dataset)
    db.flush()
    version = DatasetVersion(dataset_id=dataset.id, version="v1", status="ready")
    db.add(version)
    db.flush()
    subset = DatasetSubset(
        dataset_version_id=version.id,
        name="mic",
        audio_profile="mono_mix",
        is_primary=False,
    )
    asset = AudioAsset(
        sha256=fingerprint.sha256,
        uri=str(source),
        bytes=fingerprint.bytes,
        duration_ms=fingerprint.duration_ms,
        sample_rate=44100,
        channels=2,
        codec="pcm_s16le",
    )
    db.add_all([subset, asset])
    db.flush()
    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key="001/ast/mic",
        content_uid=uuid.uuid4().hex[:24],
        audio_asset_id=asset.id,
        split="all",
        duration_ms=fingerprint.duration_ms,
        lang="yue",
        speakers=["O1"],
    )
    db.add(item)
    db.commit()
    monkeypatch.setattr(
        audio_variants,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path / "cache"),
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin12345"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    response = client.get(
        f"/api/v1/items/{item.id}/audio",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    with wave.open(io.BytesIO(response.content), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels()) == (16000, 1)
    db.expire_all()
    assert db.get(AudioVariant, (asset.id, "16k_mono")) is not None
