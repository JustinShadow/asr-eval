"""为本地 E2E 创建一次性数据集、基准、管理员与假设备。"""

import hashlib
import io
import os
import wave
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.domain.benchmarks import compute_version_hash
from app.domain.metric_profiles import TEMPLATE_PARAMS, profile_hash
from app.domain.models import (
    AppUser,
    AudioAsset,
    Benchmark,
    Dataset,
    DatasetSubset,
    DatasetVersion,
    Deployment,
    Device,
    Item,
    MetricProfile,
    RefText,
)

TRANSCRIPT = "2024年这是一条合成回归样本"


def _write_synthetic_audio(path: Path) -> bytes:
    """生成 1 秒 16kHz 单声道静音 WAV，仅供隔离的 E2E 假设备使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 16_000)
    return path.read_bytes()


def _prepare_audio(data_dir: Path) -> tuple[Path, bytes, int, int, int]:
    """准备 E2E 用音频，并从 WAV 头读取真实媒体参数。"""
    source = os.environ.get("ASR_E2E_AUDIO_PATH")
    if source:
        source_path = Path(source)
        audio_path = data_dir / "audio" / "e2e-real-sample.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(source_path.read_bytes())
        audio_bytes = audio_path.read_bytes()
    else:
        audio_path = data_dir / "audio" / "e2e-silence.wav"
        audio_bytes = _write_synthetic_audio(audio_path)

    with wave.open(io.BytesIO(audio_bytes), "rb") as audio:
        duration_ms = round(audio.getnframes() * 1000 / audio.getframerate())
        sample_rate = audio.getframerate()
        channels = audio.getnchannels()
    return audio_path, audio_bytes, duration_ms, sample_rate, channels


def _configured_real_device() -> Device | None:
    """根据显式环境变量将一台真实设备加入隔离 E2E 数据库。"""
    name = os.environ.get("ASR_E2E_REAL_DEVICE_NAME")
    if not name:
        return None
    host = os.environ.get("ASR_E2E_REAL_DEVICE_HOST")
    port = os.environ.get("ASR_E2E_REAL_DEVICE_PORT")
    ssh_user = os.environ.get("ASR_E2E_REAL_DEVICE_SSH_USER")
    ssh_port = os.environ.get("ASR_E2E_REAL_DEVICE_SSH_PORT", "22")
    if not host or not port or not ssh_user:
        raise ValueError("真实设备 E2E 需要设置 NAME、HOST、PORT 和 SSH_USER")
    return Device(
        name=name,
        platform=os.environ.get("ASR_E2E_REAL_DEVICE_PLATFORM", "205"),
        kind=os.environ.get("ASR_E2E_REAL_DEVICE_KIND", "etsstt"),
        host=host,
        port=int(port),
        ssh_user=ssh_user,
        ssh_port=int(ssh_port),
        status="unknown",
    )


def seed_database(
    db: Session,
    audio_path: Path,
    audio_bytes: bytes,
    duration_ms: int,
    sample_rate: int,
    channels: int,
    transcript: str,
) -> None:
    """写入 Run 创建与 CER 评分所需的最小真实数据库记录。"""
    user = AppUser(
        username="admin",
        display_name="E2E 管理员",
        role="admin",
        role_source="local",
        password_hash=hash_password("admin12345"),
        active=True,
    )
    dataset = Dataset(
        name=os.environ.get("ASR_E2E_DATASET_NAME", "E2E 合成数据集"),
        source_type=os.environ.get("ASR_E2E_DATASET_SOURCE_TYPE", "synthetic"),
    )
    profile_params = dict(TEMPLATE_PARAMS["cer_only"])
    profile = MetricProfile(
        name="E2E CER 口径",
        params=profile_params,
        profile_hash=profile_hash(profile_params),
    )
    fake_device_port = int(os.environ.get("ASR_E2E_FAKE_DEVICE_PORT", "18701"))
    device = Device(
        name="E2E 本地假设备",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=fake_device_port,
        status="online",
    )
    probe_device = Device(
        name="E2E 首次探测设备",
        platform="205",
        kind="etsstt",
        host="127.0.0.1",
        port=fake_device_port,
        status="unknown",
    )
    seeded_devices = [user, dataset, profile, device, probe_device]
    real_device = _configured_real_device()
    if real_device is not None:
        seeded_devices.append(real_device)
    db.add_all(seeded_devices)
    db.flush()

    version = DatasetVersion(
        dataset_id=dataset.id,
        version="v1",
        status="ready",
        item_count=1,
        total_duration_ms=duration_ms,
        capabilities={"text_ratio": 1.0},
        created_by="admin",
    )
    db.add(version)
    db.flush()
    subset = DatasetSubset(
        dataset_version_id=version.id,
        name="all",
        is_primary=True,
        item_count=1,
        total_duration_ms=duration_ms,
        capabilities={"text_ratio": 1.0},
    )
    asset = AudioAsset(
        sha256=hashlib.sha256(audio_bytes).hexdigest(),
        uri=str(audio_path),
        bytes=len(audio_bytes),
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        channels=channels,
        codec="wav",
    )
    db.add_all([subset, asset])
    db.flush()

    item = Item(
        dataset_version_id=version.id,
        subset_id=subset.id,
        item_key="e2e-sample-001",
        content_uid=hashlib.sha256(b"e2e-sample-001").hexdigest()[:24],
        audio_asset_id=asset.id,
        split="test",
        duration_ms=duration_ms,
        lang="zh",
        buckets={"source": "e2e", "lang": "zh"},
    )
    db.add(item)
    db.flush()
    db.add(
        RefText(
            item_id=item.id,
            kind="verbatim",
            lang="zh",
            text=transcript,
            sha256=hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:16],
        )
    )

    deployment = Deployment(
        device_id=device.id,
        uid="e2e0000000000001",
        platform="205",
        accel={"type": "fake"},
        model_key="e2e-stt",
        capabilities={},
        limits={"realtime_slots": 1},
    )
    db.add(deployment)
    db.flush()

    sampling = {"mode": "all"}
    bucket_rules = {"dims": ["duration_bin"]}
    benchmark = Benchmark(
        name=os.environ.get("ASR_E2E_BENCHMARK_NAME", "E2E 合成基准"),
        dataset_version_id=version.id,
        metric_profile_id=profile.id,
        subsets=["all"],
        sampling=sampling,
        params={},
        bucket_rules=bucket_rules,
        version_hash=compute_version_hash(
            db,
            dataset_version_id=version.id,
            subsets=["all"],
            profile_hash=profile.profile_hash,
            sampling=sampling,
            bucket_rules=bucket_rules,
            params={},
        ),
    )
    db.add(benchmark)
    db.commit()


def main() -> None:
    """生成一次性音频并填充测试 PostgreSQL。"""
    data_dir = Path(os.environ["ASR_E2E_DATA_DIR"])
    audio_path, audio_bytes, duration_ms, sample_rate, channels = _prepare_audio(data_dir)
    transcript = os.environ.get("ASR_E2E_TRANSCRIPT", TRANSCRIPT)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            seed_database(
                db,
                audio_path,
                audio_bytes,
                duration_ms,
                sample_rate,
                channels,
                transcript,
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
