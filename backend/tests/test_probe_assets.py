"""M2 内置合成探测素材的格式、响度和参考标注验收。"""

import hashlib
import json
import math
import wave
from array import array
from pathlib import Path
from typing import Any

ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "probe"


def _read_pcm(path: Path) -> tuple[int, list[int]]:
    """读取 PCM WAV，并返回采样率和单声道采样值。"""
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        rate = audio.getframerate()
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
    return rate, samples.tolist()


def _rms_dbfs(samples: list[int]) -> float:
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return 20 * math.log10(rms / 32768) if rms else -120.0


def test_probe_audio_matches_design_and_manifest() -> None:
    """素材符合 M2 §附录 A：采样规格、时长、响度、文本和重叠。"""
    manifest: dict[str, Any] = json.loads((ASSET_DIR / "manifest.json").read_text("utf-8"))
    ref: dict[str, Any] = json.loads((ASSET_DIR / "probe_ref.json").read_text("utf-8"))
    expected_durations = {
        "probe_spk_A.wav": 6,
        "probe_spk_B.wav": 6,
        "probe_two_spk_10s.wav": 10,
    }
    files = {entry["file"]: entry for entry in manifest["files"]}
    pcm_by_name: dict[str, list[int]] = {}
    loudness: list[float] = []

    for filename, duration_s in expected_durations.items():
        path = ASSET_DIR / filename
        entry = files[filename]
        rate, samples = _read_pcm(path)
        assert rate == 16000
        assert len(samples) == rate * duration_s
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]
        assert entry["sample_rate"] == rate
        assert entry["channels"] == 1
        assert entry["sample_width_bits"] == 16
        assert max(abs(sample) for sample in samples) < 32768
        measured_rms = _rms_dbfs(samples)
        assert abs(measured_rms - entry["rms_dbfs"]) <= 0.02
        pcm_by_name[filename] = samples
        loudness.append(measured_rms)

    assert max(loudness) - min(loudness) <= 2.0
    assert files["probe_spk_A.wav"]["voice"] != files["probe_spk_B.wav"]["voice"]
    assert pcm_by_name["probe_spk_A.wav"] != pcm_by_name["probe_spk_B.wav"]

    segments = ref["segments"]
    assert {segment["speaker"] for segment in segments} == {"A", "B"}
    assert all(0 <= segment["start_ms"] < segment["end_ms"] <= 10000 for segment in segments)
    assert all(
        sum(
            segment["end_ms"] - segment["start_ms"]
            for segment in segments
            if segment["speaker"] == speaker
        )
        >= 3000
        for speaker in ("A", "B")
    )
    overlaps = [
        (left, right)
        for index, left in enumerate(segments)
        for right in segments[index + 1 :]
        if min(left["end_ms"], right["end_ms"]) > max(left["start_ms"], right["start_ms"])
    ]
    assert len(overlaps) == 1
    overlap_ms = min(overlaps[0][0]["end_ms"], overlaps[0][1]["end_ms"]) - max(
        overlaps[0][0]["start_ms"], overlaps[0][1]["start_ms"]
    )
    assert 500 <= overlap_ms <= 1000
    text = "".join(segment["text"] for segment in segments)
    assert {"，", "。", "？"} <= set(text)
    assert ref["source"] == "synthetic_tts"
