"""音频指纹的单元测试。"""

import hashlib
import wave
from pathlib import Path

from app.importers.audio import fingerprint_wav, read_wav_metadata


def test_fingerprint_matches_sha_and_duration(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    frames = b"\x00\x00" * 1600  # 0.1s @16k
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames)

    fp = fingerprint_wav(path)
    assert fp.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert fp.duration_ms == 100
    assert fp.sample_rate == 16000
    assert fp.channels == 1
    assert fp.bytes == path.stat().st_size


def test_wav_preview_metadata_matches_fingerprint_duration(tmp_path: Path) -> None:
    """预览和导入采用同一截断口径，避免分段总时长出现舍入差异。"""
    path = tmp_path / "fractional.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 10008)

    fingerprint = fingerprint_wav(path)
    metadata = read_wav_metadata(path)

    assert fingerprint.duration_ms == 625
    assert metadata.duration_ms == fingerprint.duration_ms
    assert metadata.bytes == path.stat().st_size
    assert metadata.sample_rate == 16000
    assert metadata.channels == 1
