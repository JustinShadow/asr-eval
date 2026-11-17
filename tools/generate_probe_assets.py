"""生成 M2 设备验收用的双说话人合成探测音频。"""

import asyncio
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path
from typing import Any

import edge_tts

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "probe"
SAMPLE_RATE = 16000
SAMPLE_COUNT = SAMPLE_RATE * 10
TARGET_RMS = 3500
PEAK_LIMIT = 30000
VOICE_A = "zh-CN-XiaoxiaoNeural"
VOICE_B = "zh-CN-YunxiNeural"

ENROLLMENTS = {
    "probe_spk_A.wav": (VOICE_A, "早上好，我是甲。今天我们测试语音识别。"),
    "probe_spk_B.wav": (VOICE_B, "你好，我是乙。现在开始测试声纹识别。"),
}
PHRASES = {
    "A1": (VOICE_A, "早上好，开始测试。"),
    "B1": (VOICE_B, "你好，我听到了。"),
    "A2": (VOICE_A, "我们一起说话。"),
    "B2": (VOICE_B, "声音有重叠。"),
    "A3": (VOICE_A, "识别结果包含逗号，"),
    "B3": (VOICE_B, "还有句号。结束了吗？"),
}


def _run_ffmpeg(source: Path, target: Path, *, tempo: float = 1.0) -> None:
    """将合成语音转成 16kHz、16bit、单声道 PCM WAV。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("生成探测素材需要 ffmpeg")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if tempo != 1.0:
        command.extend(["-af", f"atempo={tempo:.6f}"])
    command.extend(
        ["-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(target)]
    )
    subprocess.run(  # noqa: S603 - 命令参数逐项传递，不经过 shell。
        command, check=True, capture_output=True, timeout=120
    )


def _read_samples(path: Path) -> list[int]:
    """读取并校验标准 PCM 音频。"""
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getframerate() != SAMPLE_RATE
            or audio.getnchannels() != 1
            or audio.getsampwidth() != 2
        ):
            raise ValueError(f"音频格式不符：{path}")
        raw = audio.readframes(audio.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    return samples.tolist()


def _normalise(samples: list[int]) -> list[int]:
    """统一响度并保留削波余量。"""
    if not samples:
        raise ValueError("合成结果为空")
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    peak = max(abs(sample) for sample in samples)
    if rms == 0 or peak == 0:
        raise ValueError("合成结果为静音")
    gain = min(TARGET_RMS / rms, PEAK_LIMIT / peak)
    return [max(-32768, min(32767, round(sample * gain))) for sample in samples]


def _write_wav(path: Path, samples: list[int]) -> None:
    """以固定采样规格写入 WAV。"""
    packed = array("h", samples)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(packed.tobytes())


def _rms_dbfs(samples: list[int]) -> float:
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return round(20 * math.log10(rms / 32768), 2) if rms else -120.0


def _linear_rms(samples: list[int]) -> float:
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


async def _synthesise(voice: str, text: str, path: Path) -> None:
    """用指定中文合成音色生成短语音频。"""
    await edge_tts.Communicate(text, voice, rate="+20%").save(str(path))


def _audio_record(path: Path, *, voice: str, text: str, samples: list[int]) -> dict[str, Any]:
    """生成素材清单中的音频元数据。"""
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "duration_ms": round(len(samples) * 1000 / SAMPLE_RATE),
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_width_bits": 16,
        "rms_dbfs": _rms_dbfs(samples),
        "voice": voice,
        "text": text,
    }


async def generate() -> None:
    """生成三段音频、参考标注和哈希清单。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix="asr-probe-") as temporary:
        temporary_dir = Path(temporary)
        for filename, (voice, text) in ENROLLMENTS.items():
            source = temporary_dir / f"{Path(filename).stem}.mp3"
            decoded = temporary_dir / f"{Path(filename).stem}.wav"
            await _synthesise(voice, text, source)
            _run_ffmpeg(source, decoded)
            samples = _read_samples(decoded)
            target_count = SAMPLE_RATE * 6
            if len(samples) > target_count:
                ratio = len(samples) / (target_count - SAMPLE_RATE // 4)
                _run_ffmpeg(source, decoded, tempo=ratio)
                samples = _read_samples(decoded)
            samples = samples[:target_count]
            samples.extend([0] * (target_count - len(samples)))
            samples = _normalise(samples)
            target = OUTPUT / filename
            _write_wav(target, samples)
            records[filename] = _audio_record(target, voice=voice, text=text, samples=samples)

        utterances: dict[str, list[int]] = {}
        phrase_text: dict[str, str] = {}
        for name, (voice, text) in PHRASES.items():
            source = temporary_dir / f"{name}.mp3"
            decoded = temporary_dir / f"{name}.wav"
            await _synthesise(voice, text, source)
            _run_ffmpeg(source, decoded, tempo=1.15)
            utterances[name] = _normalise(_read_samples(decoded))
            phrase_text[name] = text

    starts: dict[str, int] = {}
    starts["A1"] = 0
    starts["B1"] = len(utterances["A1"])
    starts["A2"] = starts["B1"] + len(utterances["B1"])
    overlap_samples = SAMPLE_RATE * 8 // 10
    starts["B2"] = max(0, starts["A2"] + len(utterances["A2"]) - overlap_samples)
    starts["A3"] = max(
        starts["A2"] + len(utterances["A2"]),
        starts["B2"] + len(utterances["B2"]),
    )
    starts["B3"] = starts["A3"] + len(utterances["A3"])
    final_end = starts["B3"] + len(utterances["B3"])
    if final_end > SAMPLE_COUNT:
        raise ValueError(
            f"双人探测语音超出 10 秒：{final_end / SAMPLE_RATE:.2f} 秒，请缩短台词或提高语速"
        )

    mixed = [0] * SAMPLE_COUNT
    segments = []
    for name in sorted(starts, key=starts.__getitem__):
        voice, _ = PHRASES[name]
        start = starts[name]
        samples = utterances[name]
        for offset, sample in enumerate(samples):
            mixed[start + offset] += round(sample * 0.62)
        segments.append(
            {
                "speaker": "A" if voice == VOICE_A else "B",
                "text": phrase_text[name],
                "start_ms": round(start * 1000 / SAMPLE_RATE),
                "end_ms": round((start + len(samples)) * 1000 / SAMPLE_RATE),
                "overlap": name in {"A2", "B2"},
            }
        )
    mixed = _normalise(mixed)
    probe_path = OUTPUT / "probe_two_spk_10s.wav"
    _write_wav(probe_path, mixed)
    records[probe_path.name] = _audio_record(
        probe_path,
        voice=f"{VOICE_A} + {VOICE_B}",
        text="".join(segment["text"] for segment in segments),
        samples=mixed,
    )

    file_metadata = {
        **{
            filename: {"voice": voice, "text": text}
            for filename, (voice, text) in ENROLLMENTS.items()
        },
        "probe_two_spk_10s.wav": {
            "voice": f"{VOICE_A} + {VOICE_B}",
            "text": records["probe_two_spk_10s.wav"]["text"],
        },
    }
    wav_paths = [OUTPUT / filename for filename in file_metadata]
    audio_samples = {path.name: _read_samples(path) for path in wav_paths}
    safe_rms_targets = []
    for samples in audio_samples.values():
        rms = _linear_rms(samples)
        peak = max(abs(sample) for sample in samples)
        safe_rms_targets.append(min(TARGET_RMS, PEAK_LIMIT * rms / peak))
    shared_rms = min(safe_rms_targets)
    for path in wav_paths:
        samples = audio_samples[path.name]
        gain = shared_rms / _linear_rms(samples)
        adjusted = [round(sample * gain) for sample in samples]
        if max(abs(sample) for sample in adjusted) > PEAK_LIMIT:
            raise ValueError(f"响度归一造成削波：{path.name}")
        _write_wav(path, adjusted)
        info = file_metadata[path.name]
        records[path.name] = _audio_record(
            path, voice=info["voice"], text=info["text"], samples=adjusted
        )

    rms_values = [record["rms_dbfs"] for record in records.values()]
    if max(rms_values) - min(rms_values) > 2.0:
        raise ValueError(f"三段素材响度差异超过 2dB：{rms_values}")
    expected_durations = {
        "probe_spk_A.wav": 6000,
        "probe_spk_B.wav": 6000,
        "probe_two_spk_10s.wav": 10000,
    }
    if any(
        records[name]["duration_ms"] != expected for name, expected in expected_durations.items()
    ):
        raise ValueError("素材时长与设计要求不符")

    ref = {
        "source": "synthetic_tts",
        "sample_rate": SAMPLE_RATE,
        "text": records["probe_two_spk_10s.wav"]["text"],
        "segments": segments,
    }
    (OUTPUT / "probe_ref.json").write_text(
        json.dumps(ref, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "generated_at": "2026-11-05",
        "source": "Microsoft Edge 中文神经语音合成；经用户授权生成的合成测试音频",
        "format": "16kHz / 16bit / mono / PCM WAV",
        "files": list(records.values()),
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(generate())
