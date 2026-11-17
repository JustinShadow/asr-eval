"""3 设备 × 3 数据集矩阵工具的选样与计划校验测试。"""

import json
import shutil
import sys
import wave
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from bench_aishell_matrix import Sample  # noqa: E402
from bench_matrix import (  # noqa: E402
    DatasetSpec,
    _load_aishell3_samples,
    _load_kespeech_samples,
    _load_plan,
    _prepare_samples,
)


def _write_plan(path: Path, *, kind: str = "aishell1") -> Path:
    path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "name": "205",
                        "host": "127.0.0.1",
                        "port": 8701,
                        "model": "etsstt",
                        "mode": "burst",
                    },
                    {
                        "name": "502",
                        "host": "127.0.0.1",
                        "port": 8702,
                        "model": "qwen3-asr-1.7b",
                        "mode": "stream",
                    },
                ],
                "datasets": [
                    {
                        "name": "语料",
                        "kind": kind,
                        "root": "/data/root",
                        "subset": "test",
                        "limit": 2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_plan_parses_devices_and_datasets(tmp_path: Path) -> None:
    """计划 JSON 正确映射为 Target 与 DatasetSpec，并保留 burst/stream 模式。"""
    devices, datasets = _load_plan(_write_plan(tmp_path / "plan.json"))

    assert [device.name for device in devices] == ["205", "502"]
    assert devices[0].mode == "burst"
    assert devices[1].mode == "stream"
    assert datasets == [DatasetSpec("语料", "aishell1", Path("/data/root"), "test", 2)]


def test_load_plan_rejects_unknown_dataset_kind(tmp_path: Path) -> None:
    """未知数据集类型必须尽早失败，避免真机跑到一半才报错。"""
    with pytest.raises(ValueError, match="kind 不支持"):
        _load_plan(_write_plan(tmp_path / "plan.json", kind="unknown"))


def test_aishell3_loader_uses_even_tokens_and_original_audio_path(tmp_path: Path) -> None:
    """AISHELL-3 的“汉字 拼音”交替列只取汉字，音频仍指向原目录。"""
    root = tmp_path / "AISHELL-3"
    split = root / "test"
    audio_dir = split / "wav" / "SSB0693"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "SSB06930002.wav"
    audio.write_bytes(b"wav")
    (split / "content.txt").write_text(
        "SSB06930002.wav\t今 jin1 天 tian1 很 hen3 好 hao3\n", encoding="utf-8"
    )

    samples = _load_aishell3_samples(DatasetSpec("AISHELL-3", "aishell3", root, "test", 1))

    assert len(samples) == 1
    assert samples[0].item_key == "test-SSB06930002.wav"
    assert samples[0].reference == "今天很好"
    assert samples[0].audio_path == audio


def test_kespeech_loader_reads_dialect_pairs(tmp_path: Path) -> None:
    """KeSpeech 按方言目录读取同名 wav/txt 对，文本为单行纯文本。"""
    root = tmp_path / "kespeech"
    dialect = root / "Beijing"
    dialect.mkdir(parents=True)
    audio = dialect / "1000234_7188a851.wav"
    audio.write_bytes(b"wav")
    (dialect / "1000234_7188a851.txt").write_text("去年六月的饥荒\n", encoding="utf-8")

    samples = _load_kespeech_samples(DatasetSpec("KeSpeech", "kespeech", root, "all", 1))

    assert len(samples) == 1
    assert samples[0].item_key == "Beijing-1000234_7188a851"
    assert samples[0].reference == "去年六月的饥荒"
    assert samples[0].audio_path == audio


def test_prepare_samples_transcodes_44k_without_touching_source(tmp_path: Path) -> None:
    """AISHELL-3 原始 44.1k 音频只转临时 16k 缓存，源文件保持不变。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg 验证 44.1k 转码")
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 4410)
    before = source.read_bytes()
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    prepared = _prepare_samples([Sample("item-1", source, "参考")], work_dir)

    assert len(prepared) == 1
    assert prepared[0].audio_path != source
    with wave.open(str(prepared[0].audio_path), "rb") as handle:
        assert (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (
            16000,
            1,
            2,
        )
    assert source.read_bytes() == before


def test_loader_fails_when_requested_samples_are_missing(tmp_path: Path) -> None:
    """样本数不足时明确失败，而不是悄悄缩小矩阵分母。"""
    root = tmp_path / "kespeech"
    root.mkdir()
    with pytest.raises(ValueError, match="少于要求"):
        _load_kespeech_samples(DatasetSpec("KeSpeech", "kespeech", root, "all", 1))
